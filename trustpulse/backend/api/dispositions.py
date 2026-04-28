from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.session import get_tp_session
from governance.store import create_disposition

router = APIRouter(prefix="/api/dispositions", tags=["dispositions"])


class DispositionRequest(BaseModel):
    event_id: int
    reviewer: str
    action: str
    notes: str = ""


@router.post("")
def submit_disposition(req: DispositionRequest, db: Session = Depends(get_tp_session)):
    try:
        disp = create_disposition(db, req.event_id, req.reviewer, req.action, req.notes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "disposition_id": disp.id,
        "event_id": disp.event_id,
        "action": disp.action,
        "updated_event_status": disp.action if disp.action != "FALSE_POSITIVE" else "DISMISSED",
    }
