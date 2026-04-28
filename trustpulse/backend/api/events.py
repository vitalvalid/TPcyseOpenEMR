from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_

from db.models import NormalizedEvent
from db.session import get_tp_session

router = APIRouter(prefix="/api/events", tags=["events"])


def event_to_dict(e: NormalizedEvent) -> dict:
    return {
        "id": e.id,
        "source_log_id": e.source_log_id,
        "ingested_at": e.ingested_at.isoformat() if e.ingested_at else None,
        "event_time": e.event_time.isoformat() if e.event_time else None,
        "user_id": e.user_id,
        "user_name": e.user_name,
        "user_role": e.user_role,
        "event_type": e.event_type,
        "patient_id": e.patient_id,
        "department": e.department,
        "ip_address": e.ip_address,
        "hour_of_day": e.hour_of_day,
        "day_of_week": e.day_of_week,
        "risk_score": e.risk_score,
        "risk_level": e.risk_level,
        "triggered_rules": e.triggered_rules or [],
        "status": e.status,
    }


@router.get("")
def list_events(
    status: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_tp_session),
):
    q = db.query(NormalizedEvent)

    if status and status != "ALL":
        q = q.filter(NormalizedEvent.status == status)
    if risk_level:
        q = q.filter(NormalizedEvent.risk_level == risk_level)
    if user_id:
        q = q.filter(NormalizedEvent.user_id == user_id)
    if start_date:
        q = q.filter(NormalizedEvent.event_time >= start_date)
    if end_date:
        q = q.filter(NormalizedEvent.event_time <= end_date)

    total = q.count()
    events = (
        q.order_by(NormalizedEvent.risk_score.desc(), NormalizedEvent.event_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "page": page, "page_size": page_size, "events": [event_to_dict(e) for e in events]}


@router.get("/{event_id}")
def get_event(event_id: int, db: Session = Depends(get_tp_session)):
    from fastapi import HTTPException
    from db.models import Disposition

    event = db.get(NormalizedEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    dispositions = (
        db.query(Disposition)
        .filter(Disposition.event_id == event_id)
        .order_by(Disposition.reviewed_at.desc())
        .all()
    )
    result = event_to_dict(event)
    result["dispositions"] = [
        {
            "id": d.id,
            "reviewer": d.reviewer,
            "action": d.action,
            "notes": d.notes,
            "reviewed_at": d.reviewed_at.isoformat() if d.reviewed_at else None,
        }
        for d in dispositions
    ]
    return result
