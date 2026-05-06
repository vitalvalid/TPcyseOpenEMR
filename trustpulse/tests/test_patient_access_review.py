import json
from datetime import datetime, timedelta

from api.cases import get_case
from db.models import Case, NormalizedEvent
from engine.case_engine import generate_cases
from patient_access_review import (
    CASE_TYPE_ACCOUNT_ACCESS_REVIEW,
    CASE_TYPE_PATIENT_ACCESS_REVIEW,
    build_case_assessment,
    build_things_to_confirm,
)


def _rule(rule_id: str, rule_name: str, description: str, *, severity="HIGH", confidence="LOW", score=20.0, fired=True):
    return [{
        "rule_id": rule_id,
        "rule_name": rule_name,
        "fired": fired,
        "score_contribution": score,
        "description": description,
        "severity": severity,
        "confidence": confidence,
    }]


def _event(
    db,
    *,
    source_log_id: int,
    user_id: str,
    user_name: str,
    user_role: str,
    event_time: datetime,
    patient_id: str,
    event_type: str = "patient_access",
    triggered_rules=None,
    ip_address="10.0.0.1",
    department="Demo Health Clinic",
):
    event = NormalizedEvent(
        source_log_id=source_log_id,
        event_time=event_time,
        user_id=user_id,
        user_name=user_name,
        user_role=user_role,
        event_type=event_type,
        patient_id=patient_id,
        department=department,
        ip_address=ip_address,
        hour_of_day=event_time.hour,
        day_of_week=event_time.weekday(),
        risk_score=45.0,
        risk_level="HIGH",
        triggered_rules=triggered_rules or [],
        status="PENDING",
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _assessment_for_checklist(
    *,
    case_type=CASE_TYPE_PATIENT_ACCESS_REVIEW,
    role="Billing",
    pattern_type="VOLUME_SPIKE",
    missing_context=None,
    primary_action=None,
    primary_triggers=None,
):
    if missing_context is None:
        missing_context = [{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}]
    if primary_action is None:
        primary_action = {"action": "REQUEST_CONTEXT", "reason_code": "NEED_BILLING_JUSTIFICATION"}
    if primary_triggers is None:
        primary_triggers = [{"rule_id": "R-08", "rule_name": "Access Volume Spike"}]
    case = Case(
        case_id="checklist-case",
        title="Checklist Case",
        severity="P1_HIGH",
        pattern_type=pattern_type,
        case_type=case_type,
        user_id="user_1",
        user_name="User One",
        event_count=3,
        risk_score=45.0,
        recommended_action="FOLLOW_UP",
        breach_risk=False,
        status="OPEN",
    )
    assessment = {
        "case_type": case_type,
        "subject_role": role,
        "pattern_type": pattern_type,
        "primary_triggers": primary_triggers,
        "missing_context": missing_context,
        "primary_suggested_action": primary_action,
        "primary_suggested_reason_code": primary_action.get("reason_code"),
    }
    return case, assessment


def test_volume_spike_patient_access_case_gets_patient_access_review_case_type(db, compliance_user):
    base_time = datetime.utcnow().replace(microsecond=0, second=0, minute=0) - timedelta(days=1)
    for i in range(3):
        _event(
            db,
            source_log_id=2000 + i,
            user_id="billing_ross",
            user_name="David Ross",
            user_role="Billing",
            event_time=base_time + timedelta(minutes=i),
            patient_id=f"PAT-{i:03d}",
            triggered_rules=_rule(
                "R-08",
                "Access Volume Spike",
                "Daily access count 27 exceeds threshold 20 (avg=0.0, σ=1.0, maturity=COLD_START)",
            ),
        )

    assert generate_cases(db) == 1
    case = db.query(Case).one()
    assert case.pattern_type == "VOLUME_SPIKE"
    assert case.case_type == CASE_TYPE_PATIENT_ACCESS_REVIEW

    detail = get_case(case.case_id, db=db, _user=compliance_user)
    assert detail["case_type"] == CASE_TYPE_PATIENT_ACCESS_REVIEW
    assert detail["quick_review_reason"]
    assert len(detail["quick_review_checks"]) <= 3
    assert detail["patient_access_assessment"]["linked_event_count"] == 3
    assert detail["patient_access_assessment"]["unique_patient_token_count"] == 3
    assert detail["event_type_breakdown"]["patient_access"] == 3
    assert "billing, payment, claims" in " ".join(detail["quick_review_checks"]).lower()
    assert detail["things_to_confirm"]["checklist_template_id"] == "BILLING_ACCESS_REVIEW"
    assert detail["primary_suggested_action"]["action"] == "ESCALATED" or detail["primary_suggested_action"]["action"] == "RESOLVED_AUTHORIZED_ACCESS" or detail["primary_suggested_action"]["action"] == "REQUEST_CONTEXT"
    assert detail["events"][0]["patient_token"].startswith("PT-")
    assert "patient_id" not in detail["events"][0]
    assert "PAT-000" not in json.dumps(detail["patient_access_assessment"])


def test_after_hours_patient_access_case_gets_patient_access_review_case_type(db):
    event_time = datetime.utcnow().replace(microsecond=0, minute=0, second=0) - timedelta(days=2)
    _event(
        db,
        source_log_id=3001,
        user_id="nurse_chen",
        user_name="Nurse Chen",
        user_role="Nurse",
        event_time=event_time.replace(hour=22),
        patient_id="PAT-201",
        triggered_rules=_rule(
            "R-01",
            "After-Hours Access",
            "Access at 22:00 - outside business hours (07:00–19:00)",
            confidence="HIGH",
        ),
    )

    assert generate_cases(db) == 1
    case = db.query(Case).one()
    assert case.pattern_type == "OFF_HOURS"
    assert case.case_type == CASE_TYPE_PATIENT_ACCESS_REVIEW


def test_assessment_includes_missing_appointment_and_ip_context(db):
    base_time = datetime.utcnow().replace(microsecond=0, second=0, minute=0) - timedelta(days=1)
    event = _event(
        db,
        source_log_id=4001,
        user_id="billing_ross",
        user_name="David Ross",
        user_role="Billing",
        event_time=base_time,
        patient_id="PAT-401",
        ip_address=None,
        triggered_rules=[
            {
                "rule_id": "R-05",
                "rule_name": "VIP/No-Appointment Access",
                "fired": False,
                "not_evaluated": True,
                "not_evaluated_reason": "Appointment context unavailable - openemr_postcalendar_events table not accessible.",
                "description": "VIP patient accessed without a corresponding appointment",
            },
            {
                "rule_id": "R-01",
                "rule_name": "After-Hours Access",
                "fired": True,
                "score_contribution": 20.0,
                "description": "Access at 22:00 - outside business hours (07:00–19:00)",
                "severity": "MEDIUM",
                "confidence": "HIGH",
            },
        ],
    )
    case = Case(
        case_id="review-case-1",
        title="After-Hours Activity - David Ross",
        severity="P1_HIGH",
        pattern_type="OFF_HOURS",
        case_type=CASE_TYPE_PATIENT_ACCESS_REVIEW,
        user_id=event.user_id,
        user_name=event.user_name,
        event_count=1,
        date_start=event.event_time,
        date_end=event.event_time,
        risk_score=45.0,
        recommended_action="FOLLOW_UP",
        breach_risk=False,
        status="OPEN",
        created_at=event.event_time,
    )
    db.add(case)
    db.commit()

    assessment = build_case_assessment(case, [event], db)
    contexts = {item["context"]: item for item in assessment["missing_context"]}
    assert "Appointment/care relationship" in contexts
    assert contexts["Appointment/care relationship"]["status"] == "UNAVAILABLE"
    assert "openemr_postcalendar_events" in contexts["Appointment/care relationship"]["reason"]
    assert "IP address" in contexts
    assert contexts["IP address"]["status"] == "UNAVAILABLE"
    assert any("Appointment/care relationship was not available" in item for item in assessment["missing_information_summary"])


def test_assessment_suggests_reason_coded_actions(db):
    event_time = datetime.utcnow().replace(microsecond=0, second=0, minute=0) - timedelta(days=1)
    event = _event(
        db,
        source_log_id=5001,
        user_id="billing_ross",
        user_name="David Ross",
        user_role="Billing",
        event_time=event_time,
        patient_id="PAT-501",
        ip_address=None,
        triggered_rules=_rule(
            "R-08",
            "Access Volume Spike",
            "Daily access count 25 exceeds threshold 20 (avg=0.0, σ=1.0, maturity=COLD_START)",
        ),
    )
    case = Case(
        case_id="review-case-2",
        title="Access Volume Spike - David Ross",
        severity="P1_HIGH",
        pattern_type="VOLUME_SPIKE",
        case_type=CASE_TYPE_PATIENT_ACCESS_REVIEW,
        user_id=event.user_id,
        user_name=event.user_name,
        event_count=1,
        date_start=event.event_time,
        date_end=event.event_time,
        risk_score=45.0,
        recommended_action="FOLLOW_UP",
        breach_risk=False,
        status="OPEN",
        created_at=event.event_time,
    )
    db.add(case)
    db.commit()

    assessment = build_case_assessment(case, [event], db)
    action_map = {item["action"]: item for item in assessment["suggested_review_actions"]}
    request_context = action_map["REQUEST_CONTEXT"]
    recommendation = assessment["context_request_recommendation"]
    assert action_map["RESOLVED_AUTHORIZED_ACCESS"]["reason_code"] == "BILLING_OR_PAYMENT_OPERATIONS"
    assert action_map["FALSE_POSITIVE"]["reason_code"] == "RULE_THRESHOLD_TOO_LOW"
    assert request_context["intent"] == "REQUEST_BILLING_JUSTIFICATION"
    assert request_context["template_id"] == "REQUEST_BILLING_JUSTIFICATION"
    assert request_context["reason_code"] == "NEED_BILLING_JUSTIFICATION"
    assert "Billing role" in request_context["rationale_labels"]
    assert recommendation["template_id"] == "REQUEST_BILLING_JUSTIFICATION"
    assert recommendation["reason_code"] == "NEED_BILLING_JUSTIFICATION"
    assert assessment["primary_suggested_reason_code"] == assessment["primary_suggested_action"]["reason_code"]
    assert len(assessment["quick_review_checks"]) <= 3
    assert "billing" in " ".join(assessment["quick_review_checks"]).lower()


def test_clinician_guidance_references_treatment_or_on_call_context(db):
    event_time = datetime.utcnow().replace(microsecond=0, second=0, minute=0) - timedelta(days=1)
    event = _event(
        db,
        source_log_id=6001,
        user_id="dr_nguyen",
        user_name="Dr Nguyen",
        user_role="Physician",
        event_time=event_time.replace(hour=23),
        patient_id="PAT-601",
        triggered_rules=_rule(
            "R-01",
            "After-Hours Access",
            "Access at 23:00 - outside business hours (07:00–19:00)",
            confidence="HIGH",
        ),
    )
    case = Case(
        case_id="review-case-3",
        title="After-Hours Activity - Dr Nguyen",
        severity="P1_HIGH",
        pattern_type="OFF_HOURS",
        case_type=CASE_TYPE_PATIENT_ACCESS_REVIEW,
        user_id=event.user_id,
        user_name=event.user_name,
        event_count=1,
        date_start=event.event_time,
        date_end=event.event_time,
        risk_score=45.0,
        recommended_action="FOLLOW_UP",
        breach_risk=False,
        status="OPEN",
        created_at=event.event_time,
    )
    db.add(case)
    db.commit()

    assessment = build_case_assessment(case, [event], db)
    checks = " ".join(assessment["quick_review_checks"]).lower()
    assert "treatment" in checks or "on-call" in checks or "care-coordination" in checks
    assert "suspicious" not in checks


def test_build_things_to_confirm_physician_case_gets_clinician_checklist():
    case, assessment = _assessment_for_checklist(role="Physician", primary_action={"action": "REQUEST_CONTEXT", "reason_code": "NEED_PROVIDER_CONFIRMATION"})
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "CLINICIAN_ACCESS_REVIEW"
    assert checklist["items"] == [
        "Treatment, on-call, or care-coordination reason",
        "Scheduled, admitted, or assigned patients",
        "Timing consistent with clinical workflow",
    ]


def test_build_things_to_confirm_billing_case_gets_billing_checklist():
    case, assessment = _assessment_for_checklist(role="Billing")
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "BILLING_ACCESS_REVIEW"
    assert checklist["items"][0] == "Billing, payment, claims, or insurance reason"


def test_build_things_to_confirm_nurse_case_gets_nurse_checklist():
    case, assessment = _assessment_for_checklist(role="Nurse", primary_action={"action": "REQUEST_CONTEXT", "reason_code": "NEED_SUPERVISOR_REVIEW"})
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "NURSE_ACCESS_REVIEW"
    assert checklist["items"][1] == "Triage, medication, documentation, or care-coordination reason"


def test_build_things_to_confirm_admin_case_gets_admin_checklist():
    case, assessment = _assessment_for_checklist(role="Front desk reception", primary_action={"action": "REQUEST_CONTEXT", "reason_code": "NEED_SUPERVISOR_REVIEW"})
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "ADMINISTRATIVE_ACCESS_REVIEW"
    assert checklist["items"][0] == "Scheduling, registration, insurance, or administrative reason"


def test_build_things_to_confirm_account_case_gets_account_checklist():
    case, assessment = _assessment_for_checklist(
        case_type=CASE_TYPE_ACCOUNT_ACCESS_REVIEW,
        role="IT / Security Admin",
        pattern_type="FAILED_LOGIN_BURST",
        missing_context=[],
        primary_action={"action": "REQUEST_CONTEXT", "reason_code": "POSSIBLE_CREDENTIAL_MISUSE"},
        primary_triggers=[],
    )
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "ACCOUNT_ACCESS_REVIEW"
    assert checklist["items"][2] == "Escalate to IT/security if not confirmed"


def test_build_things_to_confirm_fallback_works():
    case, assessment = _assessment_for_checklist(
        role="Unknown",
        missing_context=[],
        primary_action={"action": "REQUEST_CONTEXT", "reason_code": ""},
        primary_triggers=[],
    )
    checklist = build_things_to_confirm(case, assessment)
    assert checklist["checklist_template_id"] == "GENERAL_ACCESS_REVIEW"
    assert checklist["items"][0] == "Business, clinical, or operational reason"


def test_build_things_to_confirm_is_capped_at_three_and_deterministic():
    case, assessment = _assessment_for_checklist(role="Billing")
    first = build_things_to_confirm(case, assessment)
    second = build_things_to_confirm(case, assessment)
    assert len(first["items"]) == 3
    assert first == second


def test_build_things_to_confirm_contains_no_raw_patient_ids():
    case, assessment = _assessment_for_checklist(role="Physician")
    checklist = build_things_to_confirm(case, assessment)
    payload = json.dumps(checklist)
    assert "patient_id" not in payload.lower()
    assert "pat-" not in payload.lower()
