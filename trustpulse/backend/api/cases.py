"""
Case management API.
Reviewer identity comes from the authenticated user - never from the request body.
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from case_lifecycle import ACTIVE_CASE_STATUSES, INACTIVE_CASE_STATUSES
from db.models import (
    Case,
    CaseAction,
    BreachAssessment,
    CaseEvent,
    ContextRequest,
    IngestionManifest,
    KnownPattern,
    TrustPulseUser,
)
from db.session import get_tp_session
from engine.case_engine import get_case_events
from api.auth import get_current_user, require_permission
from governance.evidence import tokenize_patient_id
from api.admin import get_privacy_state, user_has_privacy_access, mask_dict, MASK
from patient_access_review import build_case_assessment
from review_reasons import SENSITIVE_REASON_ACTIONS, get_reason_for_action, get_reason_options_for_action

router = APIRouter(prefix="/api/cases", tags=["cases"])


# ── CaseAction hash chain ─────────────────────────────────────────────────────

def _get_last_action_hash(case_id: str, db: Session) -> str:
    last = (
        db.query(CaseAction.record_hash)
        .filter(CaseAction.case_id == case_id)
        .order_by(CaseAction.created_at.desc())
        .first()
    )
    return last[0] if (last and last[0]) else "0" * 64


def _compute_action_hash(fields: dict, previous_hash: str) -> str:
    canonical = json.dumps({**fields, "previous_hash": previous_hash}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _record_action(
    db: Session,
    case: Case,
    actor: TrustPulseUser,
    action: str,
    new_status: str,
    previous_status: str = "",
    reason_code: str = "",
    reason_label_snapshot: str = "",
    notes: str = "",
    request: Optional[Request] = None,
) -> CaseAction:
    previous_hash = _get_last_action_hash(case.case_id, db)
    now           = datetime.utcnow()

    fields = {
        "case_id":         case.case_id,
        "actor_email":     actor.email,
        "actor_role":      actor.role,
        "action":          action,
        "previous_status": previous_status,
        "new_status":      new_status,
        "reason_code":     reason_code,
        "reason_label_snapshot": reason_label_snapshot,
        "notes":           notes,
        "created_at":      now.isoformat(),
    }
    record_hash = _compute_action_hash(fields, previous_hash)

    ca = CaseAction(
        case_id         = case.case_id,
        actor_user_id   = str(actor.id),
        actor_email     = actor.email,
        actor_role      = actor.role,
        action          = action,
        previous_status = previous_status,
        new_status      = new_status,
        reason_code     = reason_code,
        reason_label_snapshot = reason_label_snapshot,
        notes           = notes,
        source_ip       = request.client.host if request and request.client else None,
        user_agent      = request.headers.get("user-agent") if request else None,
        created_at      = now,
        previous_hash   = previous_hash,
        record_hash     = record_hash,
    )
    db.add(ca)
    return ca


# ── Serialisation ─────────────────────────────────────────────────────────────

def case_to_dict(c: Case, db: Optional[Session] = None) -> dict:
    now     = datetime.utcnow()
    snoozed = bool(c.snoozed_until and c.snoozed_until > now)
    triage  = _triage_bucket(c, now)

    result = {
        "case_id":             c.case_id,
        "title":               c.title,
        "severity":            c.severity,
        "pattern_type":        c.pattern_type,
        "case_type":           getattr(c, "case_type", None) or "GENERIC_REVIEW",
        "user_id":             c.user_id,
        "user_name":           c.user_name,
        "event_count":         c.event_count,
        "date_start":          c.date_start.isoformat() if c.date_start else None,
        "date_end":            c.date_end.isoformat() if c.date_end else None,
        "risk_score":          c.risk_score,
        "recommended_action":  c.recommended_action,
        "breach_risk":         c.breach_risk,
        "breach_deadline":     c.breach_deadline.isoformat() if c.breach_deadline else None,
        "breach_days_remaining": (c.breach_deadline - now).days if c.breach_deadline else None,
        "status":              c.status,
        "hipaa_provisions":    c.hipaa_provisions or [],
        "snoozed":             snoozed,
        "snoozed_until":       c.snoozed_until.isoformat() if c.snoozed_until else None,
        "created_at":          c.created_at.isoformat() if c.created_at else None,
        "latest_event_time":   c.date_end.isoformat() if c.date_end else (c.created_at.isoformat() if c.created_at else None),
        "resolved_at":         c.resolved_at.isoformat() if c.resolved_at else None,
        "triage_bucket":       triage,
        "is_demo":             c.is_demo,
        "escalated_to_role":   c.escalated_to_role,
        "escalated_to_email":  c.escalated_to_email,
        "escalated_at":        c.escalated_at.isoformat() if c.escalated_at else None,
        "escalation_reason":   c.escalation_reason,
        "escalation_due_at":   c.escalation_due_at.isoformat() if c.escalation_due_at else None,
    }

    if db:
        actions = (
            db.query(CaseAction)
            .filter(CaseAction.case_id == c.case_id)
            .order_by(CaseAction.created_at.asc())
            .all()
        )
        result["action_history"] = [
            {
                "id":              a.id,
                "actor_email":     a.actor_email,
                "actor_role":      a.actor_role,
                "action":          a.action,
                "previous_status": a.previous_status,
                "new_status":      a.new_status,
                "reason_code":     a.reason_code,
                "reason_label_snapshot": a.reason_label_snapshot,
                "notes":           a.notes,
                "source_ip":       a.source_ip,
                "user_agent":      a.user_agent,
                "created_at":      a.created_at.isoformat(),
                "record_hash":     a.record_hash,
            }
            for a in actions
        ]

    return result


def _triage_bucket(c: Case, now: datetime) -> str:
    if c.severity == "P0_CRITICAL":
        return "TODAY"
    if c.date_end and c.date_end >= now - timedelta(days=7):
        return "THIS_WEEK"
    return "BACKLOG"


# ── List / detail ─────────────────────────────────────────────────────────────

@router.get("")
def list_cases(
    status: Optional[str]  = Query(None),
    status_group: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    triage_bucket: Optional[str] = Query(None),
    db: Session = Depends(get_tp_session),
    _user: TrustPulseUser = Depends(get_current_user),
):
    now = datetime.utcnow()
    q   = db.query(Case)
    if status and status != "ALL":
        q = q.filter(Case.status == status)
    elif status_group == "active":
        q = q.filter(Case.status.in_(ACTIVE_CASE_STATUSES))
    elif status_group == "closed":
        q = q.filter(Case.status.in_(INACTIVE_CASE_STATUSES))
    if severity:
        q = q.filter(Case.severity == severity)
    q = q.filter((Case.snoozed_until == None) | (Case.snoozed_until <= now))
    cases = q.order_by(Case.date_end.desc(), Case.created_at.desc(), Case.risk_score.desc()).all()
    if triage_bucket:
        cases = [c for c in cases if _triage_bucket(c, now) == triage_bucket]
    return {"total": len(cases), "cases": [case_to_dict(c) for c in cases]}


@router.get("/reason-codes")
def reason_codes(
    action: str,
    db: Session = Depends(get_tp_session),
    _user: TrustPulseUser = Depends(get_current_user),
):
    rows = get_reason_options_for_action(db, action)
    return {
        "action": action,
        "reason_codes": [
            {
                "code": r.code,
                "label": r.label,
                "category": r.category,
                "requires_note": bool(r.requires_note),
            }
            for r in rows
        ],
    }


@router.get("/{case_id}")
def get_case(
    case_id: str,
    db: Session = Depends(get_tp_session),
    _user: TrustPulseUser = Depends(get_current_user),
):
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")

    events     = get_case_events(c, db)
    assessment = (
        db.query(BreachAssessment)
        .filter(BreachAssessment.case_id == case_id)
        .order_by(BreachAssessment.completed_at.desc())
        .first()
    )

    privacy_on, privacy_fields = get_privacy_state(db)
    is_admin = _user.role == "TRUSTPULSE_ADMIN"
    do_mask  = privacy_on and not is_admin and not user_has_privacy_access(_user.email, db)

    case_event_map = {
        ce.normalized_event_id: ce
        for ce in (
            db.query(CaseEvent)
            .filter(CaseEvent.case_id == case_id)
            .all()
        )
    }
    manifest_ids = sorted({e.manifest_id for e in events if e.manifest_id})
    manifest_map = {
        m.id: m
        for m in (
            db.query(IngestionManifest)
            .filter(IngestionManifest.id.in_(manifest_ids))
            .all()
            if manifest_ids else []
        )
    }

    def _ev(e):
        case_event = case_event_map.get(e.id)
        manifest = manifest_map.get(e.manifest_id)
        ev = {
            "id":              e.id,
            "source_log_id":   e.source_log_id,
            "event_time":      e.event_time.isoformat(),
            "event_type":      e.event_type,
            "patient_token":   tokenize_patient_id(e.patient_id) if not (do_mask and "patient_id" in privacy_fields) else MASK,
            "manifest_id":     e.manifest_id,
            "ip_address":      e.ip_address,
            "user_name":       e.user_name,
            "user_id":         e.user_id,
            "source_username": e.user_id,
            "source_event_type": e.event_type,
            "department":      e.department,
            "risk_score":      e.risk_score,
            "triggered_rules": e.triggered_rules or [],
            "linked_reason":   case_event.linked_reason if case_event else None,
            "rule_id":         case_event.rule_id if case_event else None,
            "source_connector_name": manifest.connector_name if manifest else None,
            "source_name":     manifest.source_name if manifest else None,
            "source_system":   manifest.source_system if manifest else None,
            "source_manifest_hash": manifest.manifest_hash if manifest else None,
            "source_batch_sha256": manifest.source_batch_sha256 if manifest else None,
            "normalized_batch_sha256": manifest.normalized_batch_sha256 if manifest else None,
        }
        if do_mask:
            ev = mask_dict(ev, [f for f in privacy_fields if f != "patient_id"])
        return ev

    result = case_to_dict(c, db=db)
    result["events"] = [_ev(e) for e in events]
    result["demo_source_label"] = "OpenEMR-derived demo activity" if c.is_demo else None
    result["source_traceability"] = {
        "source_events_label": "Source events",
        "connector_names": sorted({m.connector_name for m in manifest_map.values() if m.connector_name}),
        "source_names": sorted({m.source_name for m in manifest_map.values() if m.source_name}),
        "manifest_ids": manifest_ids,
        "manifest_hashes": [m.manifest_hash for m in manifest_map.values() if m.manifest_hash],
        "normalized_batch_hashes": [m.normalized_batch_sha256 for m in manifest_map.values() if m.normalized_batch_sha256],
        "source_batch_hashes": [m.source_batch_sha256 for m in manifest_map.values() if m.source_batch_sha256],
    }
    result["linked_event_count"] = len(events)
    patient_access_assessment = build_case_assessment(c, events, db)
    fired_rules = {}
    for event in events:
        for rule in event.triggered_rules or []:
            if not (rule.get("fired") or rule.get("not_evaluated")):
                continue
            rule_id = rule.get("rule_id", "")
            if rule_id not in fired_rules:
                fired_rules[rule_id] = rule
    result["fired_rules"] = list(fired_rules.values())
    result["privacy_masked"] = do_mask
    result["privacy_fields"] = privacy_fields if do_mask else []
    result["patient_access_assessment"] = patient_access_assessment
    context_requests = (
        db.query(ContextRequest)
        .filter(ContextRequest.case_id == case_id)
        .order_by(ContextRequest.created_at.asc())
        .all()
    )
    result["context_requests"] = [
        {
            "id": row.id,
            "requested_by_user_id": row.requested_by_user_id,
            "requested_by_email": row.requested_by_email,
            "requested_from_role": row.requested_from_role,
            "requested_from_email": row.requested_from_email,
            "question": row.question,
            "due_at": row.due_at.isoformat() if row.due_at else None,
            "status": row.status,
            "response_text": row.response_text,
            "responded_by_email": row.responded_by_email,
            "responded_at": row.responded_at.isoformat() if row.responded_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "recommendation_provenance": getattr(row, "recommendation_provenance", None),
        }
        for row in context_requests
    ]
    if patient_access_assessment:
        result["unique_patient_token_count"] = patient_access_assessment.get("unique_patient_token_count", len({e["patient_token"] for e in result["events"] if e["patient_token"] != "-"}))
        result["event_type_breakdown"] = patient_access_assessment.get("event_type_breakdown", {})
        result["primary_triggers"] = patient_access_assessment.get("primary_triggers", [])
        result["missing_context"] = patient_access_assessment.get("missing_context", [])
        result["quick_review_reason"] = patient_access_assessment.get("quick_review_reason", patient_access_assessment.get("summary", ""))
        result["quick_review_checks"] = patient_access_assessment.get("quick_review_checks", [])
        result["things_to_confirm"] = patient_access_assessment.get("things_to_confirm")
        result["authentication_summary"] = patient_access_assessment.get("authentication_summary")
        result["modifiers"] = patient_access_assessment.get("modifiers", [])
        result["related_authentication_signals"] = patient_access_assessment.get("related_authentication_signals", [])
        result["primary_suggested_action"] = patient_access_assessment.get("primary_suggested_action")
        result["primary_suggested_reason_code"] = patient_access_assessment.get("primary_suggested_reason_code")
        result["alternate_suggested_action"] = patient_access_assessment.get("alternate_suggested_action")
        result["missing_information_summary"] = patient_access_assessment.get("missing_information_summary", [])
        result["technical_details"] = patient_access_assessment.get("technical_details", {})
        result["guidance_rationale"] = patient_access_assessment.get("guidance_rationale", "")
        result["case_review_checklist"] = patient_access_assessment.get("case_review_checklist", [])
        result["checklist_items"] = patient_access_assessment.get("checklist_items", [])
        result["checklist_rationale"] = patient_access_assessment.get("checklist_rationale", "")
        result["recommended_review_questions"] = patient_access_assessment.get("checklist_items", [])
        result["suggested_review_actions"] = patient_access_assessment.get("suggested_review_actions", [])
        result["suggested_reason_codes"] = patient_access_assessment.get("suggested_reason_codes", [])
        result["not_evaluated_rules"] = patient_access_assessment.get("not_evaluated_rules", [])
        result["context_request_recommendation"] = patient_access_assessment.get("context_request_recommendation")
    else:
        result["unique_patient_token_count"] = len({e["patient_token"] for e in result["events"] if e["patient_token"] != "-"})
        result["event_type_breakdown"] = {}
        result["primary_triggers"] = []
        result["missing_context"] = []
        result["quick_review_reason"] = ""
        result["quick_review_checks"] = []
        result["things_to_confirm"] = None
        result["authentication_summary"] = None
        result["modifiers"] = []
        result["related_authentication_signals"] = []
        result["primary_suggested_action"] = None
        result["primary_suggested_reason_code"] = None
        result["alternate_suggested_action"] = None
        result["missing_information_summary"] = []
        result["technical_details"] = {}
        result["guidance_rationale"] = ""
        result["case_review_checklist"] = []
        result["checklist_items"] = []
        result["checklist_rationale"] = ""
        result["recommended_review_questions"] = []
        result["suggested_review_actions"] = []
        result["suggested_reason_codes"] = []
        result["not_evaluated_rules"] = []
        result["context_request_recommendation"] = None
    result["breach_assessment"] = (
        {
            "q1_unauthorized":   assessment.q1_unauthorized,
            "q2_acquired":       assessment.q2_acquired,
            "q3_disclosed":      assessment.q3_disclosed,
            "factor1_score":     assessment.factor1_score,
            "factor2_score":     assessment.factor2_score,
            "factor4_mitigated": assessment.factor4_mitigated,
            "determination":     assessment.determination,
            "ocr_deadline":      assessment.ocr_deadline.isoformat() if assessment.ocr_deadline else None,
            "completed_by":      assessment.completed_by,
            "completed_at":      assessment.completed_at.isoformat() if assessment.completed_at else None,
            "notes":             assessment.notes,
        }
        if assessment else None
    )
    return result


# ── Review actions ────────────────────────────────────────────────────────────

class DispositionRequest(BaseModel):
    action:       str
    notes:        str = ""
    snooze_hours: int = 24
    reason:       str = ""
    reason_code:  str = ""
    expires_days: int = 90
    requested_from_role: str = ""
    requested_from_email: str = ""
    context_question: str = ""
    due_date: str = ""
    escalated_to_role: str = ""
    escalated_to_email: str = ""
    escalation_reason: str = ""
    escalation_due_date: str = ""
    # Recommendation provenance (optional — sent by UI when "Use this action" was clicked)
    suggested_intent: str = ""
    suggested_template_id: str = ""
    suggested_template_version: str = ""
    suggested_rationale_labels: list[str] = []
    generated_question_text: str = ""


class ContextResponseRequest(BaseModel):
    response_text: str
    responded_by_email: str = ""
    notes: str = ""


VALID_CASE_ACTIONS = {
    "REVIEW_STARTED",
    "REQUEST_CONTEXT",
    "ESCALATED",
    "RESOLVED_AUTHORIZED_ACCESS",
    "RESOLVED_NO_ISSUE",
    "FALSE_POSITIVE",
    "SUPPRESSED",
    "REOPENED",
    "COMMENT_ADDED",
    "EVIDENCE_EXPORTED",
}

RESOLVED_CASE_STATUSES = {"RESOLVED", "FALSE_POSITIVE", "SUPPRESSED"}


def _apply_case_action(
    case: Case,
    action: str,
    actor: TrustPulseUser,
    now: datetime,
    req: DispositionRequest,
    db: Session,
) -> tuple[str, str]:
    previous_status = case.status
    new_status = previous_status

    if action == "REVIEW_STARTED":
        new_status = "UNDER_REVIEW"
        case.status = new_status
        case.resolved_at = None
    elif action == "REQUEST_CONTEXT":
        new_status = "NEEDS_CONTEXT"
        case.status = new_status
        case.resolved_at = None
        case.recommended_action = "FOLLOW_UP"
    elif action == "ESCALATED":
        new_status = "ESCALATED"
        case.status = new_status
        case.resolved_at = None
        case.recommended_action = "FOLLOW_UP"
        case.escalated_at = now
        case.escalated_to_role = req.escalated_to_role.strip() or case.escalated_to_role
        case.escalated_to_email = req.escalated_to_email.strip() or None
        case.escalation_reason = req.escalation_reason.strip() or req.notes
        case.escalation_due_at = _parse_optional_datetime(req.escalation_due_date)
    elif action in ("RESOLVED_AUTHORIZED_ACCESS", "RESOLVED_NO_ISSUE"):
        new_status = "RESOLVED"
        case.status = new_status
        case.resolved_at = now
        case.recommended_action = "REVIEWED"
    elif action == "FALSE_POSITIVE":
        new_status = "FALSE_POSITIVE"
        case.status = new_status
        case.resolved_at = now
        case.recommended_action = "REVIEWED"
    elif action == "SUPPRESSED":
        if actor.role not in ("TRUSTPULSE_ADMIN", "SECURITY_ADMIN"):
            raise HTTPException(
                status_code=403,
                detail="Suppression requires TRUSTPULSE_ADMIN or SECURITY_ADMIN role",
            )
        new_status = "SUPPRESSED"
        case.status = new_status
        case.resolved_at = now
        case.recommended_action = "REVIEWED"
        case.suppression_reason = req.reason or req.notes
        case.suppression_expires = now + timedelta(days=req.expires_days)
        db.add(KnownPattern(
            user_id=case.user_id,
            pattern_type=case.pattern_type,
            approved_by=actor.email,
            approval_date=now,
            reason=req.reason or req.notes,
            expires_at=now + timedelta(days=req.expires_days),
            active=True,
        ))
    elif action == "REOPENED":
        new_status = "REOPENED"
        case.status = new_status
        case.resolved_at = None
        case.suppression_expires = None
        # Restore the original recommendation based on severity/pattern
        from engine.case_engine import _recommended_action, _recommended_auth_action
        if getattr(case, "case_type", None) == "AUTH_ANOMALY":
            case.recommended_action = _recommended_auth_action(case.severity, case.pattern_type)
        else:
            case.recommended_action = _recommended_action(case.severity, case.pattern_type)
    elif action in ("COMMENT_ADDED", "EVIDENCE_EXPORTED"):
        new_status = previous_status
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    return previous_status, new_status


@router.post("/{case_id}/disposition")
def case_disposition(
    case_id: str,
    req: DispositionRequest,
    http_req: Request,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("disposition")),
):
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    if req.action not in VALID_CASE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action: {req.action}")

    reason_code = req.reason_code.strip()
    reason = get_reason_for_action(db, req.action, reason_code or None)
    if req.action in SENSITIVE_REASON_ACTIONS and not reason_code:
        raise HTTPException(status_code=400, detail=f"Reason code required for '{req.action}'")
    if reason_code and not reason:
        raise HTTPException(status_code=400, detail=f"Invalid reason_code for '{req.action}'")
    if req.action in SENSITIVE_REASON_ACTIONS and not req.notes.strip():
        raise HTTPException(status_code=400, detail=f"Notes required for '{req.action}'")
    if req.action == "REQUEST_CONTEXT":
        if not req.requested_from_role.strip():
            raise HTTPException(status_code=400, detail="Requested-from role required for 'REQUEST_CONTEXT'")
        if not req.context_question.strip():
            raise HTTPException(status_code=400, detail="Context question required for 'REQUEST_CONTEXT'")
    if req.action == "ESCALATED":
        if not req.escalated_to_role.strip():
            raise HTTPException(status_code=400, detail="Escalated-to role required for 'ESCALATED'")
        if not req.escalation_reason.strip():
            raise HTTPException(status_code=400, detail="Escalation reason required for 'ESCALATED'")

    now        = datetime.utcnow()
    old_status, new_status = _apply_case_action(c, req.action, current, now, req, db)

    _record_action(
        db, c, current, req.action, new_status,
        previous_status=old_status,
        reason_code=reason.code if reason else reason_code,
        reason_label_snapshot=reason.label if reason else "",
        notes=req.notes, request=http_req,
    )
    if req.action == "REQUEST_CONTEXT":
        provenance: Optional[dict] = None
        if req.suggested_template_id:
            provenance = {
                "suggested_intent": req.suggested_intent,
                "suggested_template_id": req.suggested_template_id,
                "suggested_template_version": req.suggested_template_version,
                "suggested_rationale_labels": req.suggested_rationale_labels,
                "generated_question_text": req.generated_question_text,
                "final_submitted_question_text": req.context_question.strip(),
            }
        db.add(
            ContextRequest(
                case_id=c.case_id,
                requested_by_user_id=str(current.id),
                requested_by_email=current.email,
                requested_from_role=req.requested_from_role.strip(),
                requested_from_email=req.requested_from_email.strip() or None,
                question=req.context_question.strip(),
                due_at=_parse_optional_datetime(req.due_date),
                status="PENDING",
                created_at=now,
                recommendation_provenance=provenance,
            )
        )
    db.commit()
    return {"case_id": case_id, "new_status": c.status, "action": req.action,
            "reviewed_by": current.email}


def _parse_optional_datetime(raw: str) -> Optional[datetime]:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime value: {value}") from exc


@router.post("/{case_id}/context_requests/{context_request_id}/response")
def respond_to_context_request(
    case_id: str,
    context_request_id: int,
    req: ContextResponseRequest,
    http_req: Request,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("disposition")),
):
    case = db.get(Case, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    context_request = db.get(ContextRequest, context_request_id)
    if not context_request or context_request.case_id != case_id:
        raise HTTPException(status_code=404, detail="Context request not found")
    if not req.response_text.strip():
        raise HTTPException(status_code=400, detail="Response text is required")

    now = datetime.utcnow()
    context_request.status = "RESPONDED"
    context_request.response_text = req.response_text.strip()
    context_request.responded_by_email = req.responded_by_email.strip() or current.email
    context_request.responded_at = now

    note = req.notes.strip() or f"Context response added from {context_request.requested_from_role}"
    _record_action(
        db,
        case,
        current,
        "CONTEXT_RESPONSE_ADDED",
        case.status,
        previous_status=case.status,
        notes=note,
        request=http_req,
    )
    db.commit()
    return {"context_request_id": context_request_id, "status": context_request.status}


# ── Breach assessment ─────────────────────────────────────────────────────────

class BreachAssessmentRequest(BaseModel):
    q1_unauthorized:  str
    q2_acquired:      str
    q3_disclosed:     str
    factor1_score:    int = 3
    factor2_score:    int = 3
    factor4_mitigated: bool = False
    notes:            str = ""


@router.post("/{case_id}/breach_assessment")
def submit_breach_assessment(
    case_id: str,
    req: BreachAssessmentRequest,
    http_req: Request,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("breach_assessment")),
):
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")

    yes_count  = sum(1 for v in (req.q1_unauthorized, req.q2_acquired, req.q3_disclosed)
                     if v == "YES")
    factor_risk = req.factor1_score + req.factor2_score

    if req.q1_unauthorized == "NO":
        determination = "LOW_RISK"
    elif yes_count >= 2 and factor_risk >= 7 and not req.factor4_mitigated:
        determination = "BREACH"
    elif "UNCERTAIN" in (req.q1_unauthorized, req.q2_acquired, req.q3_disclosed) \
            or (yes_count >= 1 and factor_risk >= 5):
        determination = "HIGH_RISK"
    else:
        determination = "LOW_RISK"

    now          = datetime.utcnow()
    old_status   = c.status
    ocr_deadline = (
        (c.date_start + timedelta(days=60))
        if determination in ("HIGH_RISK", "BREACH") else None
    )

    assessment = BreachAssessment(
        case_id           = case_id,
        q1_unauthorized   = req.q1_unauthorized,
        q2_acquired       = req.q2_acquired,
        q3_disclosed      = req.q3_disclosed,
        factor1_score     = req.factor1_score,
        factor2_score     = req.factor2_score,
        factor4_mitigated = req.factor4_mitigated,
        determination     = determination,
        ocr_deadline      = ocr_deadline,
        completed_by      = current.email,
        completed_at      = now,
        notes             = req.notes,
    )
    db.add(assessment)
    c.breach_deadline = ocr_deadline
    if determination in ("HIGH_RISK", "BREACH"):
        c.status = "ESCALATED"

    _record_action(
        db, c, current, "BREACH_ASSESSMENT_COMPLETED", c.status,
        previous_status=old_status,
        reason_code=determination, reason_label_snapshot=determination, notes=req.notes, request=http_req,
    )
    db.commit()
    db.refresh(assessment)

    return {
        "determination":      determination,
        "ocr_deadline":       ocr_deadline.isoformat() if ocr_deadline else None,
        "days_remaining":     (ocr_deadline - now).days if ocr_deadline else None,
        "evidence_package_url": f"/api/evidence/{case_id}",
        "completed_by":       current.email,
    }


@router.post("/{case_id}/suppress_pattern")
def suppress_pattern(
    case_id: str,
    req: DispositionRequest,
    http_req: Request,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(require_permission("configure")),
):
    c = db.get(Case, case_id)
    if not c:
        raise HTTPException(status_code=404, detail="Case not found")
    now = datetime.utcnow()
    reason_code = req.reason_code.strip()
    reason = get_reason_for_action(db, "SUPPRESSED", reason_code or None)
    if not reason_code:
        raise HTTPException(status_code=400, detail="Reason code required for 'SUPPRESSED'")
    if not reason:
        raise HTTPException(status_code=400, detail="Invalid reason_code for 'SUPPRESSED'")
    if not req.notes.strip():
        raise HTTPException(status_code=400, detail="Notes required for 'SUPPRESSED'")
    old_status, new_status = _apply_case_action(c, "SUPPRESSED", current, now, req, db)
    _record_action(
        db,
        c,
        current,
        "SUPPRESSED",
        new_status,
        previous_status=old_status,
        reason_code=reason.code,
        reason_label_snapshot=reason.label,
        notes=req.notes,
        request=http_req,
    )
    db.commit()
    return {"suppressed": True, "expires_days": req.expires_days,
            "approved_by": current.email}
