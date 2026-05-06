"""
Legacy export router - redirects event-level requests to the case-level evidence API.
The canonical evidence export lives at GET /api/evidence/{case_id}.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from db.models import NormalizedEvent, Case, CaseEvent
from db.session import get_tp_session
from api.auth import require_permission, TrustPulseUser

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/evidence/{event_id}")
def export_evidence_by_event(
    event_id: int,
    db: Session = Depends(get_tp_session),
    _current: TrustPulseUser = Depends(require_permission("export")),
):
    """Find the case that contains this event and redirect to the case evidence report."""
    event = db.get(NormalizedEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    case = (
        db.query(Case)
        .join(CaseEvent, CaseEvent.case_id == Case.case_id)
        .filter(CaseEvent.normalized_event_id == event.id)
        .order_by(Case.risk_score.desc())
        .first()
    )
    if not case:
        raise HTTPException(
            status_code=404,
            detail="No linked case found for this event. Use GET /api/evidence/{case_id} directly.",
        )
    return RedirectResponse(url=f"/api/evidence/{case.case_id}", status_code=307)
