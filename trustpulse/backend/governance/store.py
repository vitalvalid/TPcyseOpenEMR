from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session
from db.models import Disposition, NormalizedEvent


VALID_ACTIONS = {"REVIEWED", "ESCALATED", "DISMISSED", "FALSE_POSITIVE"}


def create_disposition(
    db: Session,
    event_id: int,
    reviewer: str,
    action: str,
    notes: str = "",
) -> Disposition:
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action '{action}'. Must be one of {VALID_ACTIONS}")

    disp = Disposition(
        event_id=event_id,
        reviewer=reviewer,
        action=action,
        notes=notes,
        reviewed_at=datetime.utcnow(),
    )
    db.add(disp)

    # Update event status
    event = db.get(NormalizedEvent, event_id)
    if event:
        event.status = action if action != "FALSE_POSITIVE" else "DISMISSED"

    db.commit()
    db.refresh(disp)
    return disp


def get_dispositions_for_event(db: Session, event_id: int):
    return (
        db.query(Disposition)
        .filter(Disposition.event_id == event_id)
        .order_by(Disposition.reviewed_at.desc())
        .all()
    )
