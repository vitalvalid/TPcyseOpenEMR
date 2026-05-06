"""
Evidence export - requires authentication and export permission.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import Case, BreachAssessment, CaseAction, ContextRequest, IngestionManifest, NormalizedEvent, TrustPulseUser
from db.session import get_tp_session
from engine.case_engine import get_case_events
from governance.evidence import generate_evidence_html
from api.auth import require_permission
from api.cases import _record_action
from api.system import history_maturity_label
from patient_access_review import build_case_assessment

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
    context_requests = (
        db.query(ContextRequest)
        .filter(ContextRequest.case_id == case_id)
        .order_by(ContextRequest.created_at.asc())
        .all()
    )
    patient_access_assessment = build_case_assessment(case, events, db)
    if patient_access_assessment != getattr(case, "assessment_json", None):
        case.assessment_json = patient_access_assessment

    # Enrich evidence package with history-based baseline maturity
    _oldest = db.query(func.min(NormalizedEvent.event_time)).scalar()
    _history_days = max((datetime.utcnow() - _oldest).days, 0) if _oldest else 0
    if patient_access_assessment and isinstance(patient_access_assessment, dict):
        _td = patient_access_assessment.setdefault("technical_details", {})
        _td["history_days_available"] = _history_days
        _td["history_maturity_label"] = history_maturity_label(_history_days)
        _td["baseline_basis"]         = "Configured policy threshold"

    html = generate_evidence_html(
        case          = case,
        events        = events,
        actions       = case_actions,
        context_requests = context_requests,
        assessment    = assessment,
        patient_access_assessment = patient_access_assessment,
        reviewer      = current.email,
        reviewer_role = current.role,
        manifests     = case_manifests,
        is_demo       = case.is_demo,
    )

    # The evidence package is generated from the current case state first, then the
    # export action is appended to the audit trail without mutating case status.
    _record_action(
        db, case, current, "EVIDENCE_EXPORTED", case.status,
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
