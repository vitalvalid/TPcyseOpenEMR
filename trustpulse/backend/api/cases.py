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

from db.models import Case, CaseAction, BreachAssessment, KnownPattern, TrustPulseUser
from db.session import get_tp_session
from engine.case_engine import get_case_events
from api.auth import get_current_user, require_permission
from governance.evidence import tokenize_patient_id
from api.admin import get_privacy_state, user_has_privacy_access, mask_dict, MASK

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
        "resolved_at":         c.resolved_at.isoformat() if c.resolved_at else None,
        "triage_bucket":       triage,
        "is_demo":             c.is_demo,
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
                "notes":           a.notes,
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
    severity: Optional[str] = Query(None),
    triage_bucket: Optional[str] = Query(None),
    db: Session = Depends(get_tp_session),
    _user: TrustPulseUser = Depends(get_current_user),
):
    now = datetime.utcnow()
    q   = db.query(Case)
    if status and status != "ALL":
        q = q.filter(Case.status == status)
    if severity:
        q = q.filter(Case.severity == severity)
    q = q.filter((Case.snoozed_until == None) | (Case.snoozed_until <= now))
    cases = q.order_by(Case.risk_score.desc(), Case.created_at.desc()).all()
    if triage_bucket:
        cases = [c for c in cases if _triage_bucket(c, now) == triage_bucket]
    return {"total": len(cases), "cases": [case_to_dict(c) for c in cases]}


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

    def _ev(e):
        ev = {
            "id":              e.id,
            "source_log_id":   e.source_log_id,
            "event_time":      e.event_time.isoformat(),
            "event_type":      e.event_type,
            "patient_token":   tokenize_patient_id(e.patient_id) if not (do_mask and "patient_id" in privacy_fields) else MASK,
            "ip_address":      e.ip_address,
            "user_name":       e.user_name,
            "user_id":         e.user_id,
            "department":      e.department,
            "risk_score":      e.risk_score,
            "triggered_rules": e.triggered_rules or [],
        }
        if do_mask:
            ev = mask_dict(ev, [f for f in privacy_fields if f != "patient_id"])
        return ev

    result = case_to_dict(c, db=db)
    result["events"] = [_ev(e) for e in events]
    result["privacy_masked"] = do_mask
    result["privacy_fields"] = privacy_fields if do_mask else []
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


# ── Disposition ───────────────────────────────────────────────────────────────

class DispositionRequest(BaseModel):
    action:       str
    notes:        str = ""
    snooze_hours: int = 24
    reason:       str = ""
    expires_days: int = 90


VALID_CASE_ACTIONS = {
    "REVIEWED", "ESCALATED", "DISMISSED", "FALSE_POSITIVE",
    "SNOOZED", "SUPPRESSED", "FOLLOW_UP",
    "BREACH_ASSESSMENT_STARTED", "BREACH_ASSESSMENT_COMPLETED",
    "REPORT_EXPORTED",
}


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

    _NOTES_REQUIRED = {"DISMISSED", "FALSE_POSITIVE", "SUPPRESSED", "ESCALATED", "FOLLOW_UP"}
    if req.action in _NOTES_REQUIRED and not (req.notes.strip() or req.reason.strip()):
        raise HTTPException(status_code=400,
                            detail=f"Notes or reason required for '{req.action}'")

    now        = datetime.utcnow()
    old_status = c.status
    new_status = c.status

    if req.action == "SNOOZED":
        c.snoozed_until = now + timedelta(hours=req.snooze_hours)
        new_status = c.status
    elif req.action == "SUPPRESSED":
        if current.role not in ("TRUSTPULSE_ADMIN", "SECURITY_ADMIN"):
            raise HTTPException(status_code=403,
                                detail="Suppression requires TRUSTPULSE_ADMIN or SECURITY_ADMIN role")
        new_status = "SUPPRESSED"
        c.status = "SUPPRESSED"
        c.suppression_reason  = req.reason or req.notes
        c.suppression_expires = now + timedelta(days=req.expires_days)
        db.add(KnownPattern(
            user_id      = c.user_id,
            pattern_type = c.pattern_type,
            approved_by  = current.email,
            approval_date = now,
            reason       = req.reason or req.notes,
            expires_at   = now + timedelta(days=req.expires_days),
            active       = True,
        ))
    elif req.action in ("REVIEWED", "DISMISSED", "FALSE_POSITIVE"):
        new_status = req.action
        c.status   = req.action
        c.resolved_at = now
    elif req.action == "FOLLOW_UP":
        new_status = "FOLLOW_UP"
        c.status   = "FOLLOW_UP"
        c.resolved_at = None
    elif req.action == "ESCALATED":
        new_status = "ESCALATED"
        c.status   = "ESCALATED"
        c.resolved_at = None

    _record_action(
        db, c, current, req.action, new_status,
        previous_status=old_status,
        reason_code=req.reason, notes=req.notes, request=http_req,
    )
    db.commit()
    return {"case_id": case_id, "new_status": c.status, "action": req.action,
            "reviewed_by": current.email}


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
        reason_code=determination, notes=req.notes, request=http_req,
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
    old_status = c.status
    db.add(KnownPattern(
        user_id      = c.user_id,
        pattern_type = c.pattern_type,
        approved_by  = current.email,
        approval_date = now,
        reason       = req.reason or req.notes,
        expires_at   = now + timedelta(days=req.expires_days),
        active       = True,
    ))
    c.status = "SUPPRESSED"
    c.suppression_reason = req.reason or req.notes
    _record_action(db, c, current, "SUPPRESSED", "SUPPRESSED",
                   previous_status=old_status,
                   reason_code=req.reason, notes=req.notes, request=http_req)
    db.commit()
    return {"suppressed": True, "expires_days": req.expires_days,
            "approved_by": current.email}
