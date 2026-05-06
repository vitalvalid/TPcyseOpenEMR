"""
Deterministic, template-based context-request recommendation engine.

No LLM calls. No random text. No patient IDs.
Same structured case facts → same intent → same template → same output.

Usage:
    ctx = build_recommendation_context(...)
    result = render_recommendation(ctx)
"""
from __future__ import annotations

TEMPLATE_VERSION = "1.0"

# ── Template Registry ──────────────────────────────────────────────────────────

TEMPLATE_REGISTRY: dict[str, dict] = {
    "REQUEST_BILLING_JUSTIFICATION": {
        "template_id": "REQUEST_BILLING_JUSTIFICATION",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether this access was related to billing, payment, claims, "
            "insurance follow-up, or other approved healthcare operations for the patient "
            "records linked to this case."
        ),
        "suggested_note_text": (
            "Requesting billing/payment justification before closing this patient-access review."
        ),
        "suggested_requested_from_role": "Billing Supervisor",
        "rationale_labels": [
            "Billing role",
            "Patient access volume",
            "Missing work-queue or appointment context",
        ],
        "compatible_reason_codes": ["NEED_BILLING_JUSTIFICATION"],
    },
    "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT": {
        "template_id": "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether this access was related to treatment, on-call coverage, "
            "urgent follow-up, or care coordination."
        ),
        "suggested_note_text": (
            "Requesting treatment, on-call, or care-coordination context before closing "
            "this patient-access review."
        ),
        "suggested_requested_from_role": "Provider / Physician",
        "rationale_labels": [
            "Clinical role",
            "Patient access review",
            "Missing treatment or on-call context",
        ],
        "compatible_reason_codes": ["NEED_PROVIDER_CONFIRMATION", "NEED_APPOINTMENT_CONTEXT"],
    },
    "REQUEST_SHIFT_OR_CARE_CONTEXT": {
        "template_id": "REQUEST_SHIFT_OR_CARE_CONTEXT",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether this access was related to the user's assigned shift, "
            "patient care duties, triage, medication workflow, or care coordination."
        ),
        "suggested_note_text": (
            "Requesting shift or care-duty context before closing this patient-access review."
        ),
        "suggested_requested_from_role": "Nurse Manager",
        "rationale_labels": [
            "Nursing role",
            "Patient access review",
            "Missing shift or care-duty context",
        ],
        "compatible_reason_codes": ["NEED_PROVIDER_CONFIRMATION", "NEED_SUPERVISOR_REVIEW"],
    },
    "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT": {
        "template_id": "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether this access was related to scheduling, registration, "
            "insurance verification, or administrative operations."
        ),
        "suggested_note_text": (
            "Requesting administrative justification before closing this patient-access review."
        ),
        "suggested_requested_from_role": "Clinic Administrator",
        "rationale_labels": [
            "Administrative role",
            "Patient access review",
            "Missing administrative context",
        ],
        "compatible_reason_codes": ["NEED_SUPERVISOR_REVIEW"],
    },
    "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION": {
        "template_id": "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether this login and patient-record access were expected for "
            "this user, device, and work location."
        ),
        "suggested_note_text": (
            "Requesting account-activity confirmation before closing this review."
        ),
        "suggested_requested_from_role": "IT / Security Admin",
        "rationale_labels": [
            "Account activity signal",
            "Patient access after login",
            "Missing source/device context",
        ],
        "compatible_reason_codes": ["POSSIBLE_CREDENTIAL_MISUSE"],
    },
    "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT": {
        "template_id": "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm whether appointment, work-queue, or care-context information "
            "supports this access."
        ),
        "suggested_note_text": (
            "Requesting appointment, work-queue, or care-context confirmation before closing "
            "this review."
        ),
        "suggested_requested_from_role": "Clinic Administrator",
        "rationale_labels": [
            "Missing appointment context",
            "Missing work-queue context",
        ],
        "compatible_reason_codes": ["NEED_APPOINTMENT_CONTEXT", "INSUFFICIENT_LOG_CONTEXT"],
    },
    "REQUEST_GENERAL_ACCESS_JUSTIFICATION": {
        "template_id": "REQUEST_GENERAL_ACCESS_JUSTIFICATION",
        "template_version": TEMPLATE_VERSION,
        "question_text": (
            "Please confirm the business, clinical, or operational reason for the "
            "patient-record access linked to this case."
        ),
        "suggested_note_text": (
            "Requesting business, clinical, or operational justification before closing "
            "this review."
        ),
        "suggested_requested_from_role": "Clinic Administrator",
        "rationale_labels": [
            "Patient access review",
            "Insufficient context",
        ],
        "compatible_reason_codes": ["INSUFFICIENT_LOG_CONTEXT", "NEED_SUPERVISOR_REVIEW"],
    },
}

# Credential-risk rule IDs that trigger account-activity intent
_CREDENTIAL_RISK_RULE_IDS = {"R-06", "R-09"}


def _missing_context_keys(missing_context: list[dict]) -> list[str]:
    return [m.get("context", "") for m in missing_context]


def _trigger_rule_ids(primary_triggers: list[dict]) -> list[str]:
    return [t.get("rule_id", "") for t in primary_triggers]


# ── Recommendation Context Builder ─────────────────────────────────────────────

def build_recommendation_context(
    *,
    case_id: str,
    case_type: str,
    user_display_name: str,
    user_username: str,
    user_role: str,
    pattern_type: str,
    selected_action: str,
    selected_reason_code: str,
    primary_triggers: list[dict],
    missing_context: list[dict],
    linked_event_count: int,
    unique_patient_token_count: int,
    event_type_breakdown: dict,
) -> dict:
    """
    Build a structured, patient-ID-free recommendation context dict.
    This is the canonical fact set that drives intent selection and template rendering.
    """
    missing_keys = _missing_context_keys(missing_context)
    trigger_rule_ids = _trigger_rule_ids(primary_triggers)

    return {
        "case_id": case_id,
        "case_type": case_type,
        "user_display_name": user_display_name,
        "user_username": user_username,
        "user_role": user_role,
        "pattern_type": pattern_type,
        "selected_action": selected_action,
        "selected_reason_code": selected_reason_code,
        "primary_triggers": primary_triggers,
        "missing_context": missing_context,
        "missing_context_keys": missing_keys,
        "trigger_rule_ids": trigger_rule_ids,
        "linked_event_count": linked_event_count,
        "unique_patient_token_count": unique_patient_token_count,
        "event_type_breakdown": event_type_breakdown,
    }


# ── Intent Selector ────────────────────────────────────────────────────────────

def select_intent(ctx: dict) -> str:
    """
    Deterministic intent selection. Same inputs always produce the same intent.

    Precedence order (highest to lowest):
    1. Credential risk / account activity
    2. Billing role
    3. Physician / provider (non-nursing clinical)
    4. Nursing role
    5. Administrative role
    6. Missing appointment / work-queue context
    7. Fallback (general)
    """
    reason_code = (ctx.get("selected_reason_code") or "").upper()
    role_lower = (ctx.get("user_role") or "").lower()
    pattern_type = (ctx.get("pattern_type") or "").upper()
    trigger_ids = set(ctx.get("trigger_rule_ids", []))
    missing_keys_lower = " ".join(k.lower() for k in ctx.get("missing_context_keys", []))
    event_breakdown = ctx.get("event_type_breakdown") or {}

    # 1. Credential risk / account activity
    if (
        reason_code == "POSSIBLE_CREDENTIAL_MISUSE"
        or pattern_type == "CREDENTIAL_RISK"
        or (trigger_ids & _CREDENTIAL_RISK_RULE_IDS)
        or event_breakdown.get("failed_login", 0) > 0
    ):
        return "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION"

    # 2. Billing
    if "bill" in role_lower or reason_code == "NEED_BILLING_JUSTIFICATION":
        return "REQUEST_BILLING_JUSTIFICATION"

    # 3. Physician / provider (not nursing)
    _clinical_words = ("physician", "doctor", "clinician", "provider")
    if any(w in role_lower for w in _clinical_words) and "nurs" not in role_lower:
        return "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT"

    # 4. Nursing
    if "nurs" in role_lower:
        return "REQUEST_SHIFT_OR_CARE_CONTEXT"

    # 5. Administrative / front-desk / reception
    _admin_words = ("admin", "registr", "schedul", "front desk", "reception")
    if any(w in role_lower for w in _admin_words):
        return "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT"

    # 6. Missing appointment / work-queue context
    _appt_words = ("appointment", "work queue", "care relationship", "scheduling")
    if (
        reason_code in ("NEED_APPOINTMENT_CONTEXT", "INSUFFICIENT_LOG_CONTEXT")
        or any(w in missing_keys_lower for w in _appt_words)
    ):
        return "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT"

    # 7. Fallback
    return "REQUEST_GENERAL_ACCESS_JUSTIFICATION"


# ── Template Renderer ──────────────────────────────────────────────────────────

def render_recommendation(ctx: dict) -> dict:
    """
    Select intent, look up template, and return a fully-structured recommendation object.

    The returned dict is safe to serialize to JSON and expose to the UI.
    It never contains raw patient IDs.
    """
    intent = select_intent(ctx)
    template = TEMPLATE_REGISTRY[intent]
    selected_reason_code = select_reason_code(ctx, intent)
    rationale_labels = derive_rationale_labels(ctx, intent, template)

    source_facts = {
        "case_type": ctx.get("case_type"),
        "user_role": ctx.get("user_role"),
        "pattern_type": ctx.get("pattern_type"),
        "selected_reason_code": ctx.get("selected_reason_code"),
        "primary_triggers": ctx.get("trigger_rule_ids"),
        "missing_context": ctx.get("missing_context_keys"),
        "linked_event_count": ctx.get("linked_event_count"),
        "unique_patient_token_count": ctx.get("unique_patient_token_count"),
    }

    return {
        "intent": intent,
        "template_id": template["template_id"],
        "template_version": template["template_version"],
        "reason_code": selected_reason_code,
        "question_text": template["question_text"],
        "suggested_note_text": template["suggested_note_text"],
        "suggested_requested_from_role": template["suggested_requested_from_role"],
        "rationale_labels": rationale_labels,
        "source_facts": source_facts,
    }


def select_reason_code(ctx: dict, intent: str) -> str:
    template = TEMPLATE_REGISTRY[intent]
    compatible = template.get("compatible_reason_codes") or []
    reason_code = (ctx.get("selected_reason_code") or "").upper()
    missing_keys_lower = " ".join(k.lower() for k in ctx.get("missing_context_keys", []))

    if reason_code in compatible:
        return reason_code

    if intent == "REQUEST_APPOINTMENT_OR_WORK_QUEUE_CONTEXT":
        if "appointment" in missing_keys_lower or "schedul" in missing_keys_lower:
            return "NEED_APPOINTMENT_CONTEXT"
        return "INSUFFICIENT_LOG_CONTEXT"

    if intent == "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT":
        if "appointment" in missing_keys_lower or "care relationship" in missing_keys_lower:
            return "NEED_APPOINTMENT_CONTEXT"
        return "NEED_PROVIDER_CONFIRMATION"

    if intent == "REQUEST_SHIFT_OR_CARE_CONTEXT":
        if reason_code == "NEED_SUPERVISOR_REVIEW":
            return reason_code
        return "NEED_PROVIDER_CONFIRMATION"

    if intent == "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT":
        return "NEED_SUPERVISOR_REVIEW"

    if intent == "REQUEST_GENERAL_ACCESS_JUSTIFICATION":
        if ctx.get("missing_context_keys"):
            return "INSUFFICIENT_LOG_CONTEXT"
        return "NEED_SUPERVISOR_REVIEW"

    if compatible:
        return compatible[0]
    return ""


def derive_rationale_labels(ctx: dict, intent: str, template: dict) -> list[str]:
    labels: list[str] = []
    role_lower = (ctx.get("user_role") or "").lower()
    pattern_type = (ctx.get("pattern_type") or "").upper()
    missing_keys = ctx.get("missing_context_keys") or []
    reason_code = select_reason_code(ctx, intent)

    if intent == "REQUEST_BILLING_JUSTIFICATION" or "bill" in role_lower:
        labels.append("Billing role")
    elif intent == "REQUEST_SHIFT_OR_CARE_CONTEXT" or "nurs" in role_lower:
        labels.append("Nursing role")
    elif intent == "REQUEST_TREATMENT_OR_ON_CALL_CONTEXT":
        labels.append("Clinical role")
    elif intent == "REQUEST_ADMINISTRATIVE_ACCESS_CONTEXT":
        labels.append("Administrative role")
    elif intent == "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION":
        labels.append("Account activity signal")
    else:
        labels.append("Patient access review")

    if pattern_type == "VOLUME_SPIKE":
        labels.append("Volume spike")
    elif pattern_type == "OFF_HOURS":
        labels.append("Off-hours activity")
    elif pattern_type == "CREDENTIAL_RISK":
        labels.append("Credential-risk pattern")

    if reason_code == "NEED_BILLING_JUSTIFICATION":
        labels.append("Need billing/payment justification")
    elif reason_code == "NEED_PROVIDER_CONFIRMATION":
        labels.append("Need provider confirmation")
    elif reason_code == "NEED_APPOINTMENT_CONTEXT":
        labels.append("Need appointment or scheduling context")
    elif reason_code == "NEED_SUPERVISOR_REVIEW":
        labels.append("Need supervisor review")
    elif reason_code == "INSUFFICIENT_LOG_CONTEXT":
        labels.append("Insufficient log context")
    elif reason_code == "POSSIBLE_CREDENTIAL_MISUSE":
        labels.append("Possible credential misuse")

    for key in missing_keys:
        lower = key.lower()
        if "appointment" in lower and "Missing appointment context" not in labels:
            labels.append("Missing appointment context")
        elif "work queue" in lower and "Missing work-queue context" not in labels:
            labels.append("Missing work-queue context")
        elif "ip address" in lower and "Missing source/device context" not in labels:
            labels.append("Missing source/device context")
        elif "care relationship" in lower and "Missing care-relationship context" not in labels:
            labels.append("Missing care-relationship context")

    for fallback in template.get("rationale_labels", []):
        if fallback not in labels:
            labels.append(fallback)

    return labels[:5]
