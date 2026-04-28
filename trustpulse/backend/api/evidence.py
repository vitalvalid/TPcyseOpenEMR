"""
Evidence export - requires authentication and export permission.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from db.models import Case, BreachAssessment, CaseAction, IngestionManifest, TrustPulseUser
from db.session import get_tp_session
from engine.case_engine import get_case_events
from governance.evidence import generate_evidence_html
from api.auth import require_permission
from api.cases import _record_action

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


@router.get("/{case_id}", response_class=HTMLResponse)
def export_case_evidence(
    case_id: str,
    http_req: Request,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("export")),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    events = get_case_events(case, db)
    assessment = (
        db.query(BreachAssessment)
        .filter(BreachAssessment.case_id == case_id)
        .order_by(BreachAssessment.completed_at.desc())
        .first()
    )

    # Find the exact manifests that ingested the events in this case
    manifest_ids = list({e.manifest_id for e in events if e.manifest_id})
    if manifest_ids:
        case_manifests = (
            db.query(IngestionManifest)
            .filter(IngestionManifest.id.in_(manifest_ids))
            .order_by(IngestionManifest.started_at.asc())
            .all()
        )
    else:
        # Fall back to most recent manifest for legacy events without manifest_id
        fallback = (
            db.query(IngestionManifest)
            .filter(IngestionManifest.status == "SUCCESS")
            .order_by(IngestionManifest.completed_at.desc())
            .first()
        )
        case_manifests = [fallback] if fallback else []

    case_actions = (
        db.query(CaseAction)
        .filter(CaseAction.case_id == case_id)
        .order_by(CaseAction.created_at.asc())
        .all()
    )

    html = generate_evidence_html(
        case          = case,
        events        = events,
        actions       = case_actions,
        assessment    = assessment,
        reviewer      = current.email,
        reviewer_role = current.role,
        manifests     = case_manifests,
        is_demo       = case.is_demo,
    )

    _record_action(
        db, case, current, "REPORT_EXPORTED", case.status,
        previous_status=case.status,
        notes=f"Evidence package exported by {current.email}",
        request=http_req,
    )
    db.commit()

    filename = f"trustpulse_evidence_{case_id[:8]}.html"
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
