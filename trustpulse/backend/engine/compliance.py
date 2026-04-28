"""
Compliance health score, trust scores, peer comparison, minimum necessary.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import (
    Case, NormalizedEvent, IngestionRun,
    KnownPattern, UserTrustScore, ComplianceCalendarItem,
)


# ── Compliance Health Score ───────────────────────────────────────────────────

def compute_compliance_health_score(db: Session) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=30)

    # 1. Audit Review Rate (30%)
    total_cases = db.query(Case).filter(Case.created_at >= cutoff).count()
    reviewed = db.query(Case).filter(
        Case.created_at >= cutoff,
        Case.status.in_(["RESOLVED", "DISMISSED", "FALSE_POSITIVE"]),
    ).count()
    review_rate = (reviewed / total_cases * 100) if total_cases > 0 else 100.0

    # 2. Mean Time to Review (20%)
    resolved_cases = (
        db.query(Case)
        .filter(Case.created_at >= cutoff, Case.resolved_at.isnot(None))
        .all()
    )
    if resolved_cases:
        days_list = [(c.resolved_at - c.created_at).days for c in resolved_cases]
        mttr = float(np.mean(days_list))
        mttr_score = max(0.0, 100 - (mttr / 7) * 50)
    else:
        mttr_score = 100.0
        mttr = 0.0

    # 3. Open P0 Cases (25%) - any open P0 caps overall score at 40
    open_p0 = db.query(Case).filter(
        Case.status == "OPEN", Case.severity == "P0_CRITICAL"
    ).count()
    p0_score = 0.0 if open_p0 > 0 else 100.0

    # 4. Suppression Quality (15%)
    total_sup = db.query(KnownPattern).count()
    documented = (
        db.query(KnownPattern)
        .filter(KnownPattern.reason.isnot(None), KnownPattern.reason != "")
        .count()
    )
    sup_score = (documented / total_sup * 100) if total_sup > 0 else 100.0

    # 5. Log Completeness (10%)
    runs = db.query(IngestionRun).filter(IngestionRun.run_at >= cutoff).all()
    success_runs = sum(1 for r in runs if r.status == "SUCCESS")
    complete_score = (success_runs / len(runs) * 100) if runs else 100.0

    raw = (
        review_rate * 0.30
        + mttr_score * 0.20
        + p0_score * 0.25
        + sup_score * 0.15
        + complete_score * 0.10
    )
    score = round(min(raw, 40 if open_p0 > 0 else raw), 1)

    if score >= 90:
        grade, color = "A",  "green"
    elif score >= 80:
        grade, color = "B+", "green"
    elif score >= 70:
        grade, color = "B",  "yellow"
    elif score >= 60:
        grade, color = "C",  "yellow"
    elif score >= 50:
        grade, color = "D",  "orange"
    else:
        grade, color = "F",  "red"

    # Top issue
    issues = []
    if open_p0:
        issues.append(f"{open_p0} critical case(s) unresolved")
    stale = db.query(Case).filter(
        Case.status == "OPEN",
        Case.created_at <= datetime.utcnow() - timedelta(days=7),
    ).count()
    if stale:
        issues.append(f"{stale} case(s) pending review for >7 days")
    overdue_cal = db.query(ComplianceCalendarItem).filter(
        ComplianceCalendarItem.status == "OVERDUE"
    ).count()
    if overdue_cal:
        issues.append(f"{overdue_cal} HIPAA calendar item(s) overdue")
    top_issue = issues[0] if issues else "No critical issues"

    return {
        "score": score,
        "grade": grade,
        "color": color,
        "components": {
            "audit_review_rate": round(review_rate, 1),
            "mean_time_to_review_days": round(mttr, 1),
            "mean_time_to_review_score": round(mttr_score, 1),
            "open_p0_cases_score": p0_score,
            "suppression_quality": round(sup_score, 1),
            "log_completeness": round(complete_score, 1),
        },
        "top_issue": top_issue,
    }


# ── User Trust Score ──────────────────────────────────────────────────────────

def compute_user_trust_score(user_id: str, db: Session) -> float:
    cutoff = datetime.utcnow() - timedelta(days=90)
    cutoff_30 = datetime.utcnow() - timedelta(days=30)
    cutoff_60 = datetime.utcnow() - timedelta(days=60)

    events = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.user_id == user_id,
            NormalizedEvent.event_time >= cutoff,
            NormalizedEvent.risk_level.in_(["HIGH", "CRITICAL"]),
        )
        .all()
    )

    penalty = 0.0
    for ev in events:
        age = (datetime.utcnow() - ev.event_time).days
        weight = 1.0 if age < 30 else (0.5 if age < 60 else 0.25)
        penalty += (ev.risk_score / 100) * weight * 0.5

    confirmed = (
        db.query(Case)
        .filter(Case.user_id == user_id, Case.status == "ESCALATED")
        .count()
    ) * 10

    score = max(0.0, min(100.0, 75.0 - penalty - confirmed))
    return round(score, 1)


def save_trust_scores(db: Session) -> Dict[str, float]:
    user_ids = [r[0] for r in db.query(NormalizedEvent.user_id).distinct().all()]
    scores: Dict[str, float] = {}
    for uid in user_ids:
        score = compute_user_trust_score(uid, db)
        scores[uid] = score
        existing = db.get(UserTrustScore, uid)
        now = datetime.utcnow()
        if existing:
            history = existing.score_history or []
            history.append({"date": now.isoformat()[:10], "score": score})
            existing.trust_score = score
            existing.score_history = history[-90:]
            existing.last_computed = now
        else:
            db.add(UserTrustScore(
                user_id=uid,
                trust_score=score,
                score_history=[{"date": now.isoformat()[:10], "score": score}],
                last_computed=now,
            ))
    db.commit()
    return scores


# ── Peer Comparison ───────────────────────────────────────────────────────────

def compute_peer_comparison(user_id: str, db: Session) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=30)
    total_days = 30

    user_ev = db.query(NormalizedEvent).filter(NormalizedEvent.user_id == user_id).first()
    if not user_ev:
        return {}
    role = user_ev.user_role or "clinician"

    stats = (
        db.query(
            NormalizedEvent.user_id,
            func.count(NormalizedEvent.id).label("total"),
        )
        .filter(
            NormalizedEvent.event_time >= cutoff,
            NormalizedEvent.user_role == role,
        )
        .group_by(NormalizedEvent.user_id)
        .all()
    )

    user_total = next((s.total for s in stats if s.user_id == user_id), 0)
    peers = [s for s in stats if s.user_id != user_id]
    peer_avg_total = (sum(s.total for s in peers) / len(peers)) if peers else user_total

    user_daily = user_total / total_days
    peer_daily = peer_avg_total / total_days
    ratio = user_daily / peer_daily if peer_daily > 0 else 1.0

    # After-hours ratio
    user_ah = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == user_id,
        NormalizedEvent.event_time >= cutoff,
        (NormalizedEvent.hour_of_day < 7) | (NormalizedEvent.hour_of_day >= 19),
    ).count()
    user_ah_pct = (user_ah / user_total * 100) if user_total else 0

    peer_ah_total = sum(
        db.query(NormalizedEvent).filter(
            NormalizedEvent.user_id == s.user_id,
            NormalizedEvent.event_time >= cutoff,
            (NormalizedEvent.hour_of_day < 7) | (NormalizedEvent.hour_of_day >= 19),
        ).count()
        for s in peers[:5]  # cap to avoid N+1 slow-down in demo
    )
    peer_ah_pct = (peer_ah_total / max(sum(s.total for s in peers[:5]), 1) * 100) if peers else 0

    percentile = round(
        sum(1 for s in stats if s.total < user_total) / len(stats) * 100
    ) if stats else 50

    return {
        "user_daily_avg": round(user_daily, 1),
        "peer_daily_avg": round(peer_daily, 1),
        "ratio": round(ratio, 1),
        "percentile": percentile,
        "role": role,
        "peer_count": len(peers),
        "user_after_hours_pct": round(user_ah_pct, 1),
        "peer_after_hours_pct": round(peer_ah_pct, 1),
    }


# ── Minimum Necessary Report ──────────────────────────────────────────────────

def compute_minimum_necessary(db: Session) -> List[dict]:
    cutoff = datetime.utcnow() - timedelta(days=30)
    total_days = 30

    rows = (
        db.query(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            NormalizedEvent.user_role,
            NormalizedEvent.department,
            func.count(NormalizedEvent.id).label("total"),
        )
        .filter(NormalizedEvent.event_time >= cutoff)
        .group_by(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            NormalizedEvent.user_role,
            NormalizedEvent.department,
        )
        .all()
    )

    by_role: Dict[str, list] = defaultdict(list)
    for r in rows:
        by_role[r.user_role or "clinician"].append(r.total / total_days)

    role_avg = {role: float(np.mean(vals)) for role, vals in by_role.items()}

    result = []
    for r in rows:
        role = r.user_role or "clinician"
        user_daily = r.total / total_days
        peer_daily = role_avg.get(role, user_daily)
        ratio = user_daily / peer_daily if peer_daily > 0 else 1.0
        status = "ACTION" if ratio >= 3.0 else ("REVIEW" if ratio >= 2.0 else "NORMAL")
        result.append({
            "user_id": r.user_id,
            "user_name": r.user_name or r.user_id,
            "role": role,
            "department": r.department,
            "user_daily_avg": round(user_daily, 1),
            "peer_daily_avg": round(peer_daily, 1),
            "ratio": round(ratio, 1),
            "status": status,
        })

    return sorted(result, key=lambda x: x["ratio"], reverse=True)


# ── Compliance Calendar Seed ──────────────────────────────────────────────────

def seed_compliance_calendar(db: Session) -> None:
    if db.query(ComplianceCalendarItem).count() > 0:
        return
    items = [
        ComplianceCalendarItem(
            item_type="AUDIT_REVIEW",
            hipaa_provision="§164.308(a)(1)(ii)(D)",
            last_completed=datetime(2026, 4, 18),
            next_due=datetime(2026, 5, 18),
            status="ON_TRACK",
            notes="Monthly audit log review",
        ),
        ComplianceCalendarItem(
            item_type="RISK_ANALYSIS",
            hipaa_provision="§164.308(a)(1)",
            last_completed=datetime(2025, 9, 15),
            next_due=datetime(2026, 9, 15),
            status="ON_TRACK",
            notes="Annual security risk analysis",
        ),
        ComplianceCalendarItem(
            item_type="TRAINING",
            hipaa_provision="§164.308(a)(5)",
            last_completed=datetime(2025, 1, 20),
            next_due=datetime(2026, 1, 20),
            status="OVERDUE",
            notes="Annual workforce HIPAA training",
        ),
        ComplianceCalendarItem(
            item_type="BAA_REVIEW",
            hipaa_provision="§164.308(b)(1)",
            last_completed=None,
            next_due=datetime(2026, 12, 31),
            status="DUE_SOON",
            notes="Business Associate Agreement review - no date recorded",
        ),
    ]
    for item in items:
        db.add(item)
    db.commit()
