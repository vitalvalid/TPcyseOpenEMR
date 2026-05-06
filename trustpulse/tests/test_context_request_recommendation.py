"""
Tests for the deterministic context-request recommendation engine.
"""
import sys
from pathlib import Path

# Allow importing backend modules directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from context_request_recommendation import (
    TEMPLATE_REGISTRY,
    build_recommendation_context,
    render_recommendation,
    select_reason_code,
    select_intent,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ctx(
    user_role="Billing",
    pattern_type="VOLUME_SPIKE",
    selected_reason_code="NEED_BILLING_JUSTIFICATION",
    missing_context=None,
    primary_triggers=None,
    event_type_breakdown=None,
    linked_event_count=10,
    unique_patient_token_count=5,
):
    if missing_context is None:
        missing_context = [{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}]
    if primary_triggers is None:
        primary_triggers = [{"rule_id": "R-02", "rule_name": "Unusual patient volume"}]
    if event_type_breakdown is None:
        event_type_breakdown = {"patient_access": linked_event_count}

    return build_recommendation_context(
        case_id="test-case-123",
        case_type="PATIENT_ACCESS_REVIEW",
        user_display_name="Test User",
        user_username="billing_test",
        user_role=user_role,
        pattern_type=pattern_type,
        selected_action="REQUEST_CONTEXT",
        selected_reason_code=selected_reason_code,
        primary_triggers=primary_triggers,
        missing_context=missing_context,
        linked_event_count=linked_event_count,
        unique_patient_token_count=unique_patient_token_count,
        event_type_breakdown=event_type_breakdown,
    )


# ── A. Recommendation context builder ─────────────────────────────────────────

def test_context_builder_extracts_role():
    ctx = _ctx(user_role="Billing")
    assert ctx["user_role"] == "Billing"


def test_context_builder_extracts_pattern():
    ctx = _ctx(pattern_type="VOLUME_SPIKE")
    assert ctx["pattern_type"] == "VOLUME_SPIKE"


def test_context_builder_extracts_reason_code():
    ctx = _ctx(selected_reason_code="NEED_BILLING_JUSTIFICATION")
    assert ctx["selected_reason_code"] == "NEED_BILLING_JUSTIFICATION"


def test_context_builder_extracts_missing_context_keys():
    ctx = _ctx(missing_context=[{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}])
    assert "Appointment/care relationship" in ctx["missing_context_keys"]


def test_context_builder_extracts_counts():
    ctx = _ctx(linked_event_count=27, unique_patient_token_count=12)
    assert ctx["linked_event_count"] == 27
    assert ctx["unique_patient_token_count"] == 12


def test_context_builder_no_patient_ids():
    ctx = _ctx()
    ctx_str = str(ctx)
    assert "patient_id" not in ctx_str.lower()
    assert "raw_patient" not in ctx_str.lower()
    # More specifically: the context dict must not contain raw patient IDs
    for key in ("patient_id", "raw_patient", "mrn"):
        assert key not in ctx_str.lower()


# ── B. Intent selector ────────────────────────────────────────────────────────

def test_billing_volume_spike_selects_billing_intent():
    ctx = _ctx(user_role="Billing", selected_reason_code="NEED_BILLING_JUSTIFICATION")
    assert select_intent(ctx) == "REQUEST_BILLING_JUSTIFICATION"


def test_billing_role_without_reason_code_selects_billing_intent():
    ctx = _ctx(user_role="Billing", selected_reason_code="")
    assert select_intent(ctx) == "REQUEST_BILLING_JUSTIFICATION"


def test_physician_selects_treatment_intent():
    ctx = _ctx(user_role="Physician", selected_reason_code="NEED_PROVIDER_CONFIRMATION")
    assert select_intent(ctx) == "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT"


def test_doctor_selects_treatment_intent():
    ctx = _ctx(user_role="Doctor", selected_reason_code="")
    assert select_intent(ctx) == "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT"


def test_clinician_selects_treatment_intent():
    ctx = _ctx(user_role="Clinician", selected_reason_code="NEED_PROVIDER_CONFIRMATION")
    assert select_intent(ctx) == "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT"


def test_nurse_selects_shift_care_intent():
    ctx = _ctx(user_role="Nursing", selected_reason_code="NEED_PROVIDER_CONFIRMATION")
    assert select_intent(ctx) == "REQUEST_SHIFT_OR_CARE_CONTEXT"


def test_nurse_with_supervisor_review_selects_shift_care_intent():
    ctx = _ctx(user_role="Nurse", selected_reason_code="NEED_SUPERVISOR_REVIEW")
    assert select_intent(ctx) == "REQUEST_SHIFT_OR_CARE_CONTEXT"


def test_admin_role_selects_administrative_intent():
    ctx = _ctx(user_role="Administration", selected_reason_code="NEED_SUPERVISOR_REVIEW")
    assert select_intent(ctx) == "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT"


def test_front_desk_selects_administrative_intent():
    ctx = _ctx(user_role="Front desk reception", selected_reason_code="")
    assert select_intent(ctx) == "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT"


def test_credential_misuse_selects_account_activity_intent():
    ctx = _ctx(user_role="Unknown", selected_reason_code="POSSIBLE_CREDENTIAL_MISUSE")
    assert select_intent(ctx) == "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION"


def test_failed_login_event_selects_account_activity_intent():
    ctx = _ctx(
        user_role="Physician",
        selected_reason_code="",
        event_type_breakdown={"failed_login": 3, "patient_access": 5},
    )
    assert select_intent(ctx) == "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION"


def test_credential_risk_pattern_selects_account_activity_intent():
    ctx = _ctx(
        user_role="Unknown",
        pattern_type="CREDENTIAL_RISK",
        selected_reason_code="",
    )
    assert select_intent(ctx) == "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION"


def test_need_appointment_context_selects_appointment_intent():
    ctx = _ctx(
        user_role="UNKNOWN",
        selected_reason_code="NEED_APPOINTMENT_CONTEXT",
        missing_context=[{"context": "appointment", "status": "UNAVAILABLE"}],
    )
    assert select_intent(ctx) == "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT"


def test_missing_appointment_in_context_selects_appointment_intent():
    ctx = _ctx(
        user_role="UNKNOWN",
        selected_reason_code="",
        missing_context=[{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}],
    )
    assert select_intent(ctx) == "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT"


def test_fallback_selects_general_justification():
    ctx = _ctx(
        user_role="UNKNOWN",
        pattern_type="",
        selected_reason_code="",
        missing_context=[],
        primary_triggers=[],
    )
    assert select_intent(ctx) == "REQUEST_GENERAL_ACCESS_JUSTIFICATION"


# ── C. Template registry structure ────────────────────────────────────────────

def test_all_templates_have_required_fields():
    required = {
        "template_id", "template_version", "question_text",
        "suggested_note_text", "suggested_requested_from_role",
        "rationale_labels", "compatible_reason_codes",
    }
    for tid, template in TEMPLATE_REGISTRY.items():
        missing = required - template.keys()
        assert not missing, f"Template {tid!r} missing fields: {missing}"


# ── D. Template renderer output ───────────────────────────────────────────────

def test_render_returns_all_required_keys():
    ctx = _ctx()
    result = render_recommendation(ctx)
    for key in ("intent", "template_id", "template_version", "question_text",
                "suggested_note_text", "suggested_requested_from_role",
                "rationale_labels", "source_facts"):
        assert key in result, f"Missing key: {key!r}"


def test_render_billing_returns_correct_template():
    ctx = _ctx(user_role="Billing", selected_reason_code="NEED_BILLING_JUSTIFICATION")
    result = render_recommendation(ctx)
    assert result["template_id"] == "REQUEST_BILLING_JUSTIFICATION"
    assert result["template_version"] == "1.0"
    assert result["reason_code"] == "NEED_BILLING_JUSTIFICATION"
    assert "billing" in result["question_text"].lower()
    assert "Billing Supervisor" == result["suggested_requested_from_role"]
    assert "Billing role" in result["rationale_labels"]
    assert "Volume spike" in result["rationale_labels"]


def test_render_source_facts_has_no_patient_ids():
    ctx = _ctx()
    result = render_recommendation(ctx)
    facts_str = str(result["source_facts"])
    for forbidden in ("patient_id", "mrn", "raw_patient"):
        assert forbidden not in facts_str.lower()


def test_render_is_deterministic():
    ctx = _ctx()
    r1 = render_recommendation(ctx)
    r2 = render_recommendation(ctx)
    assert r1 == r2


def test_select_reason_code_is_deterministic_for_general_intent():
    ctx = _ctx(
        user_role="UNKNOWN",
        pattern_type="",
        selected_reason_code="",
        missing_context=[],
        primary_triggers=[],
    )
    intent = select_intent(ctx)
    assert intent == "REQUEST_GENERAL_ACCESS_JUSTIFICATION"
    assert select_reason_code(ctx, intent) == "NEED_SUPERVISOR_REVIEW"


def test_render_nursing_returns_nurse_manager_role():
    ctx = _ctx(user_role="Nursing", selected_reason_code="NEED_PROVIDER_CONFIRMATION")
    result = render_recommendation(ctx)
    assert result["suggested_requested_from_role"] == "Nurse Manager"


def test_render_physician_returns_provider_role():
    ctx = _ctx(user_role="Physician", selected_reason_code="NEED_PROVIDER_CONFIRMATION")
    result = render_recommendation(ctx)
    assert result["suggested_requested_from_role"] == "Provider / Physician"


def test_render_credential_returns_it_admin_role():
    ctx = _ctx(user_role="Unknown", selected_reason_code="POSSIBLE_CREDENTIAL_MISUSE")
    result = render_recommendation(ctx)
    assert result["suggested_requested_from_role"] == "IT / Security Admin"


# ── UI source check ────────────────────────────────────────────────────────────

def test_frontend_shows_suggested_because():
    html = Path(__file__).resolve().parents[1].joinpath("frontend", "index.html").read_text()
    assert "Suggested because:" in html
    assert "Suggested context question" in html
    assert "You can edit this before submitting." in html
