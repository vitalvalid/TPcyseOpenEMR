from datetime import datetime, timedelta, timezone
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.models import NormalizedEvent, IngestionRun
from db.session import get_tp_session

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/summary")
def summary(db: Session = Depends(get_tp_session)):
    today = datetime.utcnow().date()
    today_start = datetime(today.year, today.month, today.day)

    total_today = (
        db.query(NormalizedEvent)
        .filter(NormalizedEvent.event_time >= today_start)
        .count()
    )
    pending = db.query(NormalizedEvent).filter(NormalizedEvent.status == "PENDING").count()
    cutoff_30d = datetime.utcnow() - timedelta(days=30)
    high_critical = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.event_time >= cutoff_30d,
            NormalizedEvent.risk_level.in_(["HIGH", "CRITICAL"]),
        )
        .count()
    )

    # Top risky users (last 30 days)
    cutoff = datetime.utcnow() - timedelta(days=30)
    rows = (
        db.query(
            NormalizedEvent.user_id,
            NormalizedEvent.user_name,
            func.avg(NormalizedEvent.risk_score).label("avg_risk"),
            func.count(NormalizedEvent.id).label("event_count"),
        )
        .filter(NormalizedEvent.event_time >= cutoff)
        .group_by(NormalizedEvent.user_id, NormalizedEvent.user_name)
        .order_by(func.avg(NormalizedEvent.risk_score).desc())
        .limit(10)
        .all()
    )
    top_risky = [
        {"user_id": r.user_id, "user_name": r.user_name,
         "avg_risk_score": round(r.avg_risk, 1), "event_count": r.event_count}
        for r in rows
    ]

    # Risk level breakdown
    level_rows = (
        db.query(NormalizedEvent.risk_level, func.count(NormalizedEvent.id))
        .group_by(NormalizedEvent.risk_level)
        .all()
    )
    by_level = {"LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0}
    for level, cnt in level_rows:
        if level in by_level:
            by_level[level] = cnt

    # Events by day (last 30 days)
    day_rows = (
        db.query(NormalizedEvent)
        .filter(NormalizedEvent.event_time >= cutoff)
        .all()
    )
    daily: dict = defaultdict(lambda: {"count": 0, "total_score": 0.0})
    for e in day_rows:
        day = e.event_time.date().isoformat()
        daily[day]["count"] += 1
        daily[day]["total_score"] += e.risk_score
    events_by_day = [
        {
            "date": d,
            "count": v["count"],
            "avg_score": round(v["total_score"] / v["count"], 1) if v["count"] else 0,
        }
        for d, v in sorted(daily.items())
    ]

    # Flagged users (HIGH/CRITICAL this week)
    week_start = datetime.utcnow() - timedelta(days=7)
    flagged_users = (
        db.query(NormalizedEvent.user_id)
        .filter(
            NormalizedEvent.event_time >= week_start,
            NormalizedEvent.risk_level.in_(["HIGH", "CRITICAL"]),
        )
        .distinct()
        .count()
    )

    last_run = (
        db.query(IngestionRun)
        .order_by(IngestionRun.run_at.desc())
        .first()
    )

    # Compliance score: 100 minus avg risk of pending events (capped 0–100)
    pending_avg = (
        db.query(func.avg(NormalizedEvent.risk_score))
        .filter(NormalizedEvent.status == "PENDING")
        .scalar()
    ) or 0.0
    compliance_score = max(0, round(100 - pending_avg, 1))

    return {
        "total_events_today": total_today,
        "pending_review": pending,
        "high_critical_count": high_critical,
        "flagged_users_this_week": flagged_users,
        "compliance_score": compliance_score,
        "top_risky_users": top_risky,
        "events_by_risk_level": by_level,
        "events_by_day": events_by_day,
        "last_ingestion_run": {
            "id": last_run.id,
            "run_at": last_run.run_at.isoformat(),
            "events_ingested": last_run.events_ingested,
            "events_scored": last_run.events_scored,
            "highest_risk": last_run.highest_risk,
            "status": last_run.status,
        } if last_run else None,
    }
