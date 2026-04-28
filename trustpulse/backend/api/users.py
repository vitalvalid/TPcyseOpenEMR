from datetime import datetime, timedelta
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, text

from db.models import NormalizedEvent, UserBaseline, UserTrustScore, Case
from db.session import get_tp_session, get_openemr_engine
from engine.compliance import compute_peer_comparison, compute_user_trust_score
from ingestion.connectors.openemr_real import ROLE_MAP, SKIP_USERNAMES

router = APIRouter(prefix="/api/users", tags=["users"])

TRUST_SHIELD = {
    "green":  (90, "✓"),
    "yellow": (70, "~"),
    "orange": (50, "⚠"),
    "red":    (0,  "✗"),
}


def _shield(score: float) -> dict:
    if score >= 90:
        return {"color": "green", "symbol": "✓"}
    if score >= 70:
        return {"color": "yellow", "symbol": "~"}
    if score >= 50:
        return {"color": "orange", "symbol": "⚠"}
    return {"color": "red", "symbol": "✗"}


def _openemr_roster() -> dict:
    """Return {username: {user_name, role, department}} for all active OpenEMR staff."""
    engine = get_openemr_engine()
    if engine is None:
        return {}
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT username, fname, lname, specialty, facility "
                "FROM users WHERE active = 1"
            )).fetchall()
        roster = {}
        for username, fname, lname, specialty, facility in rows:
            if not username or username in SKIP_USERNAMES:
                continue
            name = f"{fname or ''} {lname or ''}".strip() or username
            role = ROLE_MAP.get(specialty or "", "staff")
            dept = facility or specialty or None
            roster[username] = {"user_name": name, "role": role, "department": dept}
        return roster
    except Exception:
        return {}


@router.get("")
def list_users(db: Session = Depends(get_tp_session)):
    # Base roster from OpenEMR (all active accounts, even with no events yet)
    roster = _openemr_roster()

    # Event-based stats: group by user_id
    rows = (
        db.query(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            NormalizedEvent.user_role,
            NormalizedEvent.department,
            func.max(NormalizedEvent.event_time).label("last_active"),
            func.count(NormalizedEvent.id).label("dept_count"),
        )
        .group_by(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            NormalizedEvent.user_role,
            NormalizedEvent.department,
        )
        .all()
    )

    # Collapse duplicate user_ids — keep highest dept_count row per user
    by_user: dict = {}
    for r in rows:
        uid = r.user_id
        if uid not in by_user or r.dept_count > by_user[uid].dept_count:
            by_user[uid] = r

    # Merge: start from roster, overlay event data where it exists
    seen = set()
    result = []

    for uid, r in by_user.items():
        seen.add(uid)
        oemr = roster.get(uid, {})
        total_events = (
            db.query(func.count(NormalizedEvent.id))
            .filter(NormalizedEvent.user_id == uid)
            .scalar()
        )
        ts = db.get(UserTrustScore, uid)
        score = ts.trust_score if ts else compute_user_trust_score(uid, db)
        open_cases = db.query(Case).filter(
            Case.user_id == uid, Case.status == "OPEN"
        ).count()
        result.append({
            "user_id": uid,
            "user_name": oemr.get("user_name") or r.user_name or uid,
            "role": oemr.get("role") or r.user_role,
            "department": oemr.get("department") or r.department,
            "last_active": r.last_active.isoformat() if r.last_active else None,
            "total_events": total_events,
            "trust_score": score,
            "trust_shield": _shield(score),
            "open_cases": open_cases,
        })

    # Add OpenEMR users who have no events yet
    for uid, info in roster.items():
        if uid in seen:
            continue
        result.append({
            "user_id": uid,
            "user_name": info["user_name"],
            "role": info["role"],
            "department": info["department"],
            "last_active": None,
            "total_events": 0,
            "trust_score": 100.0,
            "trust_shield": _shield(100.0),
            "open_cases": 0,
        })

    return sorted(result, key=lambda x: x["trust_score"])


@router.get("/{user_id}/timeline")
def user_timeline(user_id: str, db: Session = Depends(get_tp_session)):
    events = (
        db.query(NormalizedEvent)
        .filter(NormalizedEvent.user_id == user_id)
        .order_by(NormalizedEvent.event_time.asc())
        .all()
    )

    roster_info = _openemr_roster().get(user_id)
    if not events and not roster_info:
        raise HTTPException(status_code=404, detail="No events found for user")

    if not events:
        # User exists in OpenEMR but has no ingested activity yet
        return {
            "user_id": user_id,
            "user_name": roster_info["user_name"],
            "role": roster_info["role"],
            "department": roster_info["department"],
            "trust_score": 100.0,
            "trust_shield": _shield(100.0),
            "trust_score_history": [],
            "peer_comparison": {},
            "open_cases": [],
            "daily_access_counts": [],
            "risk_events": [],
            "baseline": None,
            "summary": "No activity has been ingested for this user yet.",
        }

    baseline = db.get(UserBaseline, user_id)
    ts = db.get(UserTrustScore, user_id)
    trust_score = ts.trust_score if ts else compute_user_trust_score(user_id, db)
    peer = compute_peer_comparison(user_id, db)
    open_cases = (
        db.query(Case).filter(Case.user_id == user_id, Case.status == "OPEN").all()
    )

    # Daily stats
    daily: dict = defaultdict(lambda: {"count": 0, "max_risk": 0.0})
    for e in events:
        day = e.event_time.date().isoformat()
        daily[day]["count"] += 1
        daily[day]["max_risk"] = max(daily[day]["max_risk"], e.risk_score)

    daily_access_counts = [
        {"date": d, "count": v["count"], "risk_score": round(v["max_risk"], 1)}
        for d, v in sorted(daily.items())
    ]

    risk_events = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.user_id == user_id,
            NormalizedEvent.risk_level.in_(["HIGH", "CRITICAL"]),
        )
        .order_by(NormalizedEvent.risk_score.desc())
        .limit(10)
        .all()
    )

    anomalous_count = sum(1 for e in events if e.risk_level in ("HIGH", "CRITICAL"))

    # Trust score history
    ts_history = ts.score_history if ts else []

    return {
        "user_id": user_id,
        "user_name": events[0].user_name,
        "role": events[0].user_role,
        "department": events[0].department,
        "trust_score": trust_score,
        "trust_shield": _shield(trust_score),
        "trust_score_history": ts_history[-30:],
        "peer_comparison": peer,
        "open_cases": [
            {
                "case_id": c.case_id,
                "title": c.title,
                "severity": c.severity,
                "risk_score": c.risk_score,
            }
            for c in open_cases
        ],
        "daily_access_counts": daily_access_counts,
        "risk_events": [
            {
                "id": e.id,
                "event_time": e.event_time.isoformat(),
                "event_type": e.event_type,
                "risk_score": e.risk_score,
                "risk_level": e.risk_level,
                "patient_id": e.patient_id,
                "status": e.status,
            }
            for e in risk_events
        ],
        "baseline": {
            "avg_daily_accesses": baseline.avg_daily_accesses if baseline else 0,
            "typical_hours_start": baseline.typical_hours_start if baseline else 8,
            "typical_hours_end": baseline.typical_hours_end if baseline else 18,
            "avg_unique_patients": baseline.avg_unique_patients if baseline else 0,
        } if baseline else None,
        "summary": f"This user shows {anomalous_count} anomalous access pattern(s) in the current dataset.",
    }


@router.get("/{user_id}/peer_comparison")
def peer_comparison(user_id: str, db: Session = Depends(get_tp_session)):
    return compute_peer_comparison(user_id, db)
