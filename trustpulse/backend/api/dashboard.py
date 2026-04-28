"""
GET /api/dashboard - single call for the home view.
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.models import Case, NormalizedEvent, ComplianceCalendarItem
from db.session import get_tp_session
from engine.compliance import (
    compute_compliance_health_score,
    compute_minimum_necessary,
    seed_compliance_calendar,
)
from api.auth import get_current_user, TrustPulseUser

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

CLINIC_NAME = os.environ.get("CLINIC_NAME", "Riverside Family Health Clinic")


@router.get("")
def dashboard(
    db: Session = Depends(get_tp_session),
    _current: TrustPulseUser = Depends(get_current_user),
):
    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)
    week_ago_2 = now - timedelta(days=14)

    # Compliance health
    health = compute_compliance_health_score(db)

    # Seed calendar if not done yet
    seed_compliance_calendar(db)

    # Today's cases (P0 + top-2 P1, max 5 total)
    open_cases = (
        db.query(Case)
        .filter(
            Case.status == "OPEN",
            (Case.snoozed_until == None) | (Case.snoozed_until <= now),
        )
        .order_by(Case.risk_score.desc())
        .all()
    )
    p0 = [c for c in open_cases if c.severity == "P0_CRITICAL"]
    p1 = [c for c in open_cases if c.severity == "P1_HIGH"][:2]
    today_cases_raw = (p0 + p1)[:5]

    def _brief(c: Case) -> dict:
        return {
            "case_id": c.case_id,
            "title": c.title,
            "severity": c.severity,
            "pattern_type": c.pattern_type,
            "user_id": c.user_id,
            "user_name": c.user_name,
            "event_count": c.event_count,
            "risk_score": c.risk_score,
            "breach_risk": c.breach_risk,
            "breach_days_remaining": (c.breach_deadline - now).days if c.breach_deadline else None,
            "recommended_action": c.recommended_action,
            "status": c.status,
        }

    today_cases = [_brief(c) for c in today_cases_raw]
    remaining_p1 = max(0, len(p1) - 2)
    remaining_p2 = db.query(Case).filter(Case.status == "OPEN", Case.severity == "P2_MEDIUM").count()

    # All clear
    all_clear = len(p0) == 0 and len(p1) == 0

    # Compliance streak (days since last P0/P1 opened)
    last_high = (
        db.query(Case)
        .filter(Case.severity.in_(["P0_CRITICAL", "P1_HIGH"]))
        .order_by(Case.created_at.desc())
        .first()
    )
    streak_days = (now - last_high.created_at).days if last_high else 0

    # Weekly digest
    this_week_opened = db.query(Case).filter(Case.created_at >= week_ago).count()
    this_week_resolved = db.query(Case).filter(
        Case.resolved_at.isnot(None), Case.resolved_at >= week_ago
    ).count()
    this_week_users = (
        db.query(NormalizedEvent.user_id)
        .filter(
            NormalizedEvent.event_time >= week_ago,
            NormalizedEvent.risk_level.in_(["HIGH", "CRITICAL"]),
        )
        .distinct()
        .count()
    )

    prev_health = health["score"] - 6  # dummy trend; production would store snapshots
    trend = f"+{health['score'] - prev_health:.0f} from last week" if health["score"] >= prev_health else f"{health['score'] - prev_health:.0f} from last week"

    # Breach countdowns
    breach_cases = (
        db.query(Case)
        .filter(Case.breach_deadline.isnot(None), Case.status.in_(["OPEN", "ESCALATED"]))
        .order_by(Case.breach_deadline.asc())
        .limit(5)
        .all()
    )
    countdowns = [
        {
            "case_id": c.case_id,
            "title": c.title,
            "deadline": c.breach_deadline.isoformat(),
            "days_remaining": (c.breach_deadline - now).days,
        }
        for c in breach_cases
    ]

    # Minimum necessary top 3 outliers
    mn_all = compute_minimum_necessary(db)
    mn_outliers = [r for r in mn_all if r["status"] in ("ACTION", "REVIEW")][:3]

    # Calendar alerts (OVERDUE or DUE_SOON)
    cal_alerts = (
        db.query(ComplianceCalendarItem)
        .filter(ComplianceCalendarItem.status.in_(["OVERDUE", "DUE_SOON"]))
        .all()
    )

    return {
        "clinic_name": CLINIC_NAME,
        "compliance_health": health,
        "today_cases": today_cases,
        "remaining_p1": remaining_p1,
        "remaining_p2": remaining_p2,
        "all_clear": all_clear,
        "streak_days": streak_days,
        "weekly_digest": {
            "opened": this_week_opened,
            "resolved": this_week_resolved,
            "flagged_users": this_week_users,
            "health_score": health["score"],
            "health_trend": trend,
        },
        "breach_countdowns": countdowns,
        "minimum_necessary_outliers": mn_outliers,
        "calendar_alerts": [
            {
                "id": i.id,
                "item_type": i.item_type,
                "status": i.status,
                "next_due": i.next_due.isoformat() if i.next_due else None,
                "days_until_due": (i.next_due - now).days if i.next_due else None,
            }
            for i in cal_alerts
        ],
    }
