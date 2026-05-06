from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from context_request_recommendation import (
    TEMPLATE_REGISTRY,
    build_recommendation_context,
    render_recommendation,
    select_reason_code,
)
from db.models import Case, NormalizedEvent
from engine.baseline import get_baseline_dict
from engine.event_classification import AUTHENTICATION_EVENT_TYPES
from governance.evidence import tokenize_patient_id
from review_reasons import get_reason_for_action


CASE_TYPE_PATIENT_ACCESS_REVIEW  = "PATIENT_ACCESS_REVIEW"
CASE_TYPE_AUTHENTICATION_REVIEW  = "AUTHENTICATION_REVIEW"
CASE_TYPE_ACCOUNT_ACCESS_REVIEW  = "ACCOUNT_ACCESS_REVIEW"
CASE_TYPE_ACCOUNT_MISUSE_REVIEW  = "ACCOUNT_MISUSE_REVIEW"
CASE_TYPE_GENERIC_REVIEW         = "GENERIC_REVIEW"

AUTH_CASE_TYPES = {
    CASE_TYPE_AUTHENTICATION_REVIEW,
    CASE_TYPE_ACCOUNT_ACCESS_REVIEW,
    CASE_TYPE_ACCOUNT_MISUSE_REVIEW,
}

PATIENT_ACCESS_PATTERNS = {
    "VOLUME_SPIKE",
    "OFF_HOURS",
    "CREDENTIAL_RISK",
    "CROSS_DEPT",
    "INSIDER_SNOOPING",
}

CHECKLIST_TEMPLATE_VERSION = "1.0"

THINGS_TO_CONFIRM_TEMPLATE_REGISTRY: dict[str, dict] = {
    "CLINICIAN_ACCESS_REVIEW": {
        "checklist_template_id": "CLINICIAN_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "Treatment, on-call, or care-coordination reason",
            "Scheduled, admitted, or assigned patients",
            "Timing consistent with clinical workflow",
        ],
    },
    "BILLING_ACCESS_REVIEW": {
        "checklist_template_id": "BILLING_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "Billing, payment, claims, or insurance reason",
            "Assigned billing work queue or follow-up list",
            "Supervisor confirmation if unclear",
        ],
    },
    "NURSE_ACCESS_REVIEW": {
        "checklist_template_id": "NURSE_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "Assigned shift, unit, or patient-care responsibility",
            "Triage, medication, documentation, or care-coordination reason",
            "Timing consistent with shift or clinical workflow",
        ],
    },
    "ADMINISTRATIVE_ACCESS_REVIEW": {
        "checklist_template_id": "ADMINISTRATIVE_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "Scheduling, registration, insurance, or administrative reason",
            "Whether clinical-detail access was necessary",
            "Supervisor confirmation if unclear",
        ],
    },
    "ACCOUNT_ACCESS_REVIEW": {
        "checklist_template_id": "ACCOUNT_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "User expected this login or access",
            "Device or work location was expected",
            "Escalate to IT/security if not confirmed",
        ],
    },
    "GENERAL_ACCESS_REVIEW": {
        "checklist_template_id": "GENERAL_ACCESS_REVIEW",
        "checklist_template_version": CHECKLIST_TEMPLATE_VERSION,
        "items": [
            "Business, clinical, or operational reason",
            "Whether the access was expected for this role",
            "Supervisor confirmation if unclear",
        ],
    },
}


def ensure_case_review_schema(engine) -> None:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if "cases" not in tables:
        return
    cols = {c["name"] for c in inspector.get_columns("cases")}
    with engine.begin() as conn:
        if "case_type" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN case_type VARCHAR(50)"))
        if "assessment_json" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN assessment_json JSON"))
        if "escalated_to_role" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN escalated_to_role VARCHAR(100)"))
        if "escalated_to_email" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN escalated_to_email VARCHAR(200)"))
        if "escalated_at" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN escalated_at DATETIME"))
        if "escalation_reason" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN escalation_reason TEXT"))
        if "escalation_due_at" not in cols:
            conn.execute(text("ALTER TABLE cases ADD COLUMN escalation_due_at DATETIME"))
        conn.execute(
            text(
                "UPDATE cases SET case_type = :default_type "
                "WHERE case_type IS NULL OR case_type = ''"
            ),
            {"default_type": CASE_TYPE_GENERIC_REVIEW},
        )
    if "context_requests" in tables:
        cr_cols = {c["name"] for c in inspector.get_columns("context_requests")}
        with engine.begin() as conn:
            if "recommendation_provenance" not in cr_cols:
                conn.execute(text("ALTER TABLE context_requests ADD COLUMN recommendation_provenance JSON"))


def infer_case_type(pattern_type: str, events: list[NormalizedEvent]) -> str:
    if pattern_type in PATIENT_ACCESS_PATTERNS and any(_is_patient_access_event(e) for e in events):
        return CASE_TYPE_PATIENT_ACCESS_REVIEW
    return CASE_TYPE_GENERIC_REVIEW


def build_case_assessment(case: Case, events: list[NormalizedEvent], db: Session) -> Optional[dict]:
    case_type = getattr(case, "case_type", None) or infer_case_type(case.pattern_type or "", events)
    if case_type == CASE_TYPE_PATIENT_ACCESS_REVIEW:
        return build_patient_access_assessment(case, events, db)
    if case_type in AUTH_CASE_TYPES:
        return build_auth_review_assessment(case, events, db)
    return None


def build_patient_access_assessment(case: Case, events: list[NormalizedEvent], db: Session) -> dict:
    baseline = get_baseline_dict(db, case.user_id) if case.user_id else {}
    event_type_breakdown = dict(Counter((e.event_type or "unknown") for e in events))
    patient_tokens = [tokenize_patient_id(e.patient_id) for e in events if e.patient_id]
    unique_tokens = sorted(set(patient_tokens))
    fired_rules = _collect_fired_rules(events)
    not_evaluated_rules = _collect_not_evaluated_rules(events)
    subject_role = _subject_role(events)
    subject_department = _subject_department(events)
    missing_context = _build_missing_context(events, not_evaluated_rules, baseline)
    primary_triggers = _build_primary_triggers(fired_rules, baseline, unique_tokens, len(events))
    supporting_factors = _build_supporting_factors(
        case=case,
        baseline=baseline,
        subject_role=subject_role,
        event_type_breakdown=event_type_breakdown,
        missing_context=missing_context,
    )
    suggested_review_actions, context_request_recommendation = _suggested_review_actions(
        case=case,
        db=db,
        subject_role=subject_role,
        missing_context=missing_context,
        primary_triggers=primary_triggers,
        event_type_breakdown=event_type_breakdown,
        unique_patient_token_count=len(unique_tokens),
    )
    primary_action = suggested_review_actions[0] if suggested_review_actions else None
    alternate_action = suggested_review_actions[1] if len(suggested_review_actions) > 1 else None
    missing_information_summary = _missing_information_summary(missing_context)
    quick_review_reason = _quick_review_reason(case, len(events), len(unique_tokens), subject_role, primary_triggers)
    guidance_rationale = _guidance_rationale(subject_role, case, primary_triggers, missing_context)
    things_to_confirm = build_things_to_confirm(
        case,
        {
            "case_type": CASE_TYPE_PATIENT_ACCESS_REVIEW,
            "subject_role": subject_role,
            "primary_triggers": primary_triggers,
            "missing_context": missing_context,
            "primary_suggested_action": primary_action,
            "primary_suggested_reason_code": primary_action.get("reason_code") if primary_action else None,
            "pattern_type": case.pattern_type or "",
        },
    )
    checklist_items = things_to_confirm["items"]
    checklist_sources = [
        {"item": item, "source": {"type": "template", "value": things_to_confirm["checklist_template_id"]}}
        for item in checklist_items
    ]
    checklist_rationale = (
        f"Checklist template {things_to_confirm['checklist_template_id']} selected from structured case facts."
    )

    return {
        "case_type": CASE_TYPE_PATIENT_ACCESS_REVIEW,
        "summary": _summary(case, len(events), len(unique_tokens), subject_role, baseline),
        "subject": {
            "display_name": case.user_name or next((e.user_name for e in events if e.user_name), case.user_id),
            "username": case.user_id,
            "role": subject_role,
            "department": subject_department,
        },
        "subject_user": case.user_name or next((e.user_name for e in events if e.user_name), case.user_id),
        "subject_username": case.user_id,
        "subject_role": subject_role,
        "subject_department": subject_department,
        "time_window_start": case.date_start.isoformat() if case.date_start else None,
        "time_window_end": case.date_end.isoformat() if case.date_end else None,
        "event_count": case.event_count or len(events),
        "linked_event_count": len(events),
        "patient_token_count": len(patient_tokens),
        "unique_patient_token_count": len(unique_tokens),
        "activity_summary": {
            "linked_event_count": len(events),
            "patient_token_count": len(patient_tokens),
            "unique_patient_token_count": len(unique_tokens),
            "event_type_breakdown": event_type_breakdown,
        },
        "event_type_breakdown": event_type_breakdown,
        "telemetry_status": _telemetry_status(events, missing_context),
        "baseline_maturity": baseline.get("maturity", "COLD_START"),
        "primary_triggers": primary_triggers,
        "supporting_factors": supporting_factors,
        "missing_context": missing_context,
        "not_evaluated_rules": not_evaluated_rules,
        "quick_review_reason": quick_review_reason,
        "quick_review_checks": checklist_items[:3],
        "things_to_confirm": things_to_confirm,
        "primary_suggested_action": primary_action,
        "primary_suggested_reason_code": primary_action.get("reason_code") if primary_action else None,
        "alternate_suggested_action": alternate_action,
        "missing_information_summary": missing_information_summary,
        "guidance_rationale": guidance_rationale,
        "technical_details": {
            "triggered_rules": primary_triggers,
            "baseline_maturity": baseline.get("maturity", "COLD_START"),
            "baseline_note": (
                "Cold-start baseline: thresholds are conservative and confidence is lower until more historical activity is available."
                if baseline.get("maturity", "COLD_START") == "COLD_START" else ""
            ),
            "missing_context_details": missing_context,
            "missing_source_details": [item.get("reason") for item in missing_context if item.get("reason")],
            "suggested_reason_codes": [
                {
                    "action": item.get("action"),
                    "reason_code": item.get("reason_code"),
                    "reason_label": item.get("reason_label"),
                }
                for item in suggested_review_actions
                if item.get("reason_code")
            ],
            "check_sources": checklist_sources[:3],
        },
        "case_review_checklist": checklist_items,
        "checklist_items": checklist_items,
        "checklist_rationale": checklist_rationale,
        "recommended_review_questions": checklist_items,
        "suggested_review_actions": suggested_review_actions,
        "suggested_reason_codes": [
            {
                "action": item.get("action"),
                "reason_code": item.get("reason_code"),
                "reason_label": item.get("reason_label"),
            }
            for item in suggested_review_actions
            if item.get("reason_code")
        ],
        "reviewer_guidance": _reviewer_guidance(baseline, missing_context),
        "evidence_limitations": _evidence_limitations(events, baseline, missing_context),
        "context_request_recommendation": context_request_recommendation,
        "related_authentication_signals": _build_related_auth_signals(case, events, db),
    }


def _is_patient_access_event(event: NormalizedEvent) -> bool:
    return (event.event_type or "") == "patient_access" or bool(event.patient_id)


def _subject_role(events: list[NormalizedEvent]) -> str:
    role = next((e.user_role for e in events if e.user_role), None)
    return role or "UNKNOWN"


def _subject_department(events: list[NormalizedEvent]) -> Optional[str]:
    departments = [e.department for e in events if e.department]
    if not departments:
        return None
    return Counter(departments).most_common(1)[0][0]


def _collect_fired_rules(events: list[NormalizedEvent]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for event in events:
        for rule in event.triggered_rules or []:
            if rule.get("not_evaluated") or not rule.get("fired"):
                continue
            rule_id = rule.get("rule_id") or ""
            current = grouped.get(rule_id)
            if current is None or rule.get("score_contribution", 0) > current.get("score_contribution", 0):
                grouped[rule_id] = dict(rule)
    return sorted(grouped.values(), key=lambda r: r.get("score_contribution", 0), reverse=True)


def _collect_not_evaluated_rules(events: list[NormalizedEvent]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for event in events:
        for rule in event.triggered_rules or []:
            if not rule.get("not_evaluated"):
                continue
            key = (rule.get("rule_id", ""), rule.get("not_evaluated_reason", "") or "Missing evaluation context.")
            row = grouped.setdefault(
                key,
                {
                    "rule_id": rule.get("rule_id", ""),
                    "rule_name": rule.get("rule_name", ""),
                    "reason": rule.get("not_evaluated_reason", "") or "Missing evaluation context.",
                    "affected_event_count": 0,
                },
            )
            row["affected_event_count"] += 1
    return sorted(grouped.values(), key=lambda r: (-r["affected_event_count"], r["rule_id"], r["reason"]))


def _build_missing_context(events: list[NormalizedEvent], not_evaluated_rules: list[dict], baseline: dict) -> list[dict]:
    missing: list[dict] = []
    seen: set[str] = set()
    for rule in not_evaluated_rules:
        reason = rule["reason"]
        context_name = _context_name_from_reason(reason)
        if context_name in seen:
            continue
        missing.append(
            {
                "context": context_name,
                "status": "UNAVAILABLE",
                "reason": reason,
                "effect_on_confidence": _effect_on_confidence(reason),
            }
        )
        seen.add(context_name)

    patient_access_events = [e for e in events if _is_patient_access_event(e)]
    if patient_access_events and all(not e.ip_address for e in patient_access_events) and "IP address" not in seen:
        missing.append(
            {
                "context": "IP address",
                "status": "UNAVAILABLE",
                "reason": "api_log unavailable or event predates API logging.",
                "effect_on_confidence": "TrustPulse cannot determine whether access originated from a known workstation or source network.",
            }
        )
    elif any(not e.ip_address for e in patient_access_events) and "IP address" not in seen:
        missing.append(
            {
                "context": "IP address",
                "status": "PARTIAL",
                "reason": "One or more linked patient-access events do not include source IP data.",
                "effect_on_confidence": "Source-workstation comparisons are incomplete for the case window.",
            }
        )

    if baseline.get("maturity", "COLD_START") == "COLD_START":
        seen.add("Baseline maturity")

    return missing


def _context_name_from_reason(reason: str) -> str:
    lower = reason.lower()
    if "appointment" in lower or "postcalendar" in lower:
        return "Appointment/care relationship"
    if "ip" in lower or "api_log" in lower:
        return "IP address"
    if "department" in lower:
        return "Department context"
    return "OpenEMR context"


def _effect_on_confidence(reason: str) -> str:
    lower = reason.lower()
    if "appointment" in lower or "postcalendar" in lower:
        return "TrustPulse cannot determine whether the accessed patients had appointment or care-relationship context on the event date."
    if "ip" in lower or "api_log" in lower:
        return "TrustPulse cannot compare the access source to known IP or workstation context."
    if "department" in lower:
        return "TrustPulse cannot fully evaluate whether the access aligned with department assignment."
    return "TrustPulse cannot fully evaluate appropriateness without this OpenEMR context."


def _build_primary_triggers(fired_rules: list[dict], baseline: dict, unique_tokens: list[str], linked_event_count: int) -> list[dict]:
    triggers = []
    for rule in fired_rules[:5]:
        supporting_fields = {}
        rule_id = rule.get("rule_id")
        if rule_id == "R-02":
            supporting_fields = {
                "unique_patients": len(unique_tokens),
                "threshold": _extract_threshold(rule.get("description", "")),
                "baseline_maturity": baseline.get("maturity", "COLD_START"),
            }
        elif rule_id == "R-08":
            supporting_fields = {
                "daily_access_count": linked_event_count,
                "threshold": _extract_threshold(rule.get("description", "")),
                "baseline_maturity": baseline.get("maturity", "COLD_START"),
            }
        elif rule_id == "R-06":
            supporting_fields = {"linked_failed_login_count": None}
        triggers.append(
            {
                "rule_id": rule.get("rule_id"),
                "rule_name": rule.get("rule_name"),
                "severity": rule.get("severity", "MEDIUM"),
                "confidence": rule.get("confidence", "LOW"),
                "explanation": rule.get("description"),
                "supporting_fields": supporting_fields,
            }
        )
    return triggers


def _extract_threshold(description: str) -> Optional[float]:
    if "threshold " not in description:
        return None
    fragment = description.split("threshold ", 1)[1]
    raw = fragment.split(" ", 1)[0].strip(",)")
    try:
        return float(raw)
    except ValueError:
        return None


def _build_supporting_factors(case: Case, baseline: dict, subject_role: str, event_type_breakdown: dict, missing_context: list[dict]) -> list[str]:
    factors = []
    role_lower = (subject_role or "").lower()
    if "bill" in role_lower:
        factors.append("Billing role may legitimately access multiple patient records for payment operations, claims follow-up, or account resolution.")
    elif any(word in role_lower for word in ("physician", "doctor", "nurse", "clinician", "provider")):
        factors.append("Clinical roles may access multiple records for treatment, rounds, care coordination, or on-call coverage.")
    elif any(word in role_lower for word in ("admin", "registr", "schedul")):
        factors.append("Administrative roles may access records for scheduling, registration, insurance, or front-desk operations.")
    else:
        factors.append("Role-specific operational context should be confirmed with the user's supervisor or clinic administrator.")

    if baseline.get("maturity", "COLD_START") == "COLD_START":
        factors.append("Cold-start baseline reduces confidence. Thresholds are conservative until more historical activity is available.")
    if event_type_breakdown.get("failed_login", 0) > 0:
        factors.append("Failed login events in the linked window may require account misuse review in addition to access appropriateness review.")
    if case.pattern_type == "OFF_HOURS":
        factors.append("After-hours activity should be reviewed against expected shift, on-call, or administrative coverage patterns.")
    if missing_context:
        factors.append("Unavailable OpenEMR context limits how confidently TrustPulse can explain why the patient records were accessed.")
    return factors


def build_things_to_confirm(case: Case, assessment: dict) -> dict:
    role = (assessment.get("subject_role") or assessment.get("subject", {}).get("role") or "").strip()
    role_lower = role.lower()
    case_type = (assessment.get("case_type") or getattr(case, "case_type", None) or CASE_TYPE_GENERIC_REVIEW).upper()
    pattern_type = (getattr(case, "pattern_type", None) or assessment.get("pattern_type") or "").upper()
    missing_context = assessment.get("missing_context") or []
    primary_triggers = assessment.get("primary_triggers") or []
    primary_action = assessment.get("primary_suggested_action") or {}
    recommended_action = primary_action.get("action") or ""
    recommended_reason_code = primary_action.get("reason_code") or assessment.get("primary_suggested_reason_code") or ""

    if case_type in AUTH_CASE_TYPES:
        template_id = "ACCOUNT_ACCESS_REVIEW"
    elif "bill" in role_lower or recommended_reason_code == "NEED_BILLING_JUSTIFICATION":
        template_id = "BILLING_ACCESS_REVIEW"
    elif "nurs" in role_lower:
        template_id = "NURSE_ACCESS_REVIEW"
    elif any(word in role_lower for word in ("physician", "doctor", "provider", "clinician")):
        template_id = "CLINICIAN_ACCESS_REVIEW"
    elif any(word in role_lower for word in ("admin", "administration", "front desk", "reception", "registr", "schedul")):
        template_id = "ADMINISTRATIVE_ACCESS_REVIEW"
    else:
        template_id = "GENERAL_ACCESS_REVIEW"

    template = THINGS_TO_CONFIRM_TEMPLATE_REGISTRY[template_id]
    return {
        "checklist_template_id": template["checklist_template_id"],
        "checklist_template_version": template["checklist_template_version"],
        "items": template["items"][:3],
        "source_facts": {
            "case_type": case_type,
            "role": role or "UNKNOWN",
            "pattern_type": pattern_type,
            "primary_triggers": [item.get("rule_id") for item in primary_triggers[:3] if item.get("rule_id")],
            "missing_context": [item.get("context") for item in missing_context[:3] if item.get("context")],
            "recommended_action": recommended_action,
            "recommended_reason_code": recommended_reason_code,
        },
    }


def _checklist_rationale(role: str, case: Case, primary_triggers: list[dict], missing_context: list[dict]) -> str:
    parts = []
    if role and role != "UNKNOWN":
        parts.append(f"{role} role")
    if case.pattern_type:
        parts.append(case.pattern_type.replace("_", " ").title())
    if primary_triggers:
        parts.append(", ".join(
            item.get("rule_name") or item.get("rule_id") or "trigger"
            for item in primary_triggers[:2]
        ))
    if missing_context:
        missing_labels = ", ".join(item.get("context") for item in missing_context[:2] if item.get("context"))
        if missing_labels:
            parts.append(f"missing {missing_labels.lower()}")
    return "Generated from: " + ", ".join(parts) if parts else "Generated from linked OpenEMR-derived evidence."


def _case_review_checklist(
    role: str,
    case: Case,
    primary_triggers: list[dict],
    missing_context: list[dict],
    event_type_breakdown: dict,
) -> tuple[list[str], list[dict]]:
    role_lower = (role or "").lower()
    items: list[tuple[str, dict]]
    if "bill" in role_lower:
        items = [
            ("Confirm whether the access was related to billing, payment, claims, or insurance operations.", {"type": "role", "value": "Billing"}),
            ("Confirm whether the accessed patient accounts were part of the user's assigned billing work queue or follow-up list.", {"type": "role", "value": "Billing"}),
            ("Confirm whether the access volume is expected for this billing role during the review window.", {"type": "pattern", "value": case.pattern_type}),
        ]
    elif any(word in role_lower for word in ("physician", "doctor", "nurse", "clinician", "provider")):
        items = [
            ("Confirm whether there was treatment, on-call, or care-coordination context for the accessed patients.", {"type": "role", "value": role}),
            ("Confirm whether the patients were scheduled, admitted, or assigned to the clinician during this time window.", {"type": "missing_context", "value": "Appointment/care relationship"}),
            ("Confirm whether access timing was consistent with clinical workflow, shift coverage, or on-call responsibilities.", {"type": "pattern", "value": case.pattern_type}),
        ]
    elif any(word in role_lower for word in ("admin", "registr", "schedul")):
        items = [
            ("Confirm whether access was related to scheduling, registration, insurance, or administrative operations.", {"type": "role", "value": role}),
            ("Confirm whether access to clinical detail was necessary for the administrative task being performed.", {"type": "role", "value": role}),
            ("Confirm whether the number of patient records accessed was consistent with the assigned front-desk or administrative queue.", {"type": "pattern", "value": case.pattern_type}),
        ]
    else:
        items = [
            ("Confirm whether the user was expected to access these patient records for treatment, payment, or operations.", {"type": "role", "value": role or "UNKNOWN"}),
            ("Confirm whether the access timing matched the user's assigned duties or expected workflow.", {"type": "pattern", "value": case.pattern_type}),
            ("Confirm whether supervisor or clinic context is required to explain the linked access pattern.", {"type": "missing_context", "value": "OpenEMR context"}),
        ]

    trigger_ids = {item.get("rule_id") for item in primary_triggers}
    if case.pattern_type == "OFF_HOURS" or "R-01" in trigger_ids or "R-03" in trigger_ids:
        items.append(("Confirm whether the access timing was expected for the user's shift, call coverage, or assigned work hours.", {"type": "rule", "value": "R-01/R-03"}))
    elif case.pattern_type == "VOLUME_SPIKE" or "R-02" in trigger_ids or "R-08" in trigger_ids:
        items.append(("Confirm whether the linked patient-access volume is expected for this role and workload.", {"type": "rule", "value": "R-02/R-08"}))
    if case.pattern_type == "CREDENTIAL_RISK":
        items.append(("Review the linked sign-in activity and confirm whether the patient access occurred under the expected user account.", {"type": "pattern", "value": "CREDENTIAL_RISK"}))

    if missing_context:
        context_labels = ", ".join(item.get("context") for item in missing_context[:2] if item.get("context"))
        items.append((f"If {context_labels.lower()} is unavailable, request supervisor or workflow confirmation before closing the case.", {"type": "missing_context", "value": context_labels}))

    if case.severity in ("P0_CRITICAL", "P1_HIGH") or case.breach_risk or event_type_breakdown.get("failed_login", 0) > 0:
        items.append(("If the access pattern cannot be justified from role, timing, and available context, escalate to privacy/compliance review.", {"type": "next_step", "value": "ESCALATED"}))

    # Keep the drawer compact and remove generic or duplicate wording.
    normalized = []
    seen = set()
    normalized_sources = []
    for item, source in items:
        stripped = item.strip()
        key = stripped.lower()
        if not stripped or key in seen:
            continue
        seen.add(key)
        normalized.append(stripped)
        normalized_sources.append({"item": stripped, "source": source})
    return normalized[:5], normalized_sources[:5]


def _suggested_review_actions(
    case: Case,
    db: Session,
    subject_role: str,
    missing_context: list[dict],
    primary_triggers: list[dict],
    event_type_breakdown: dict,
    unique_patient_token_count: int,
) -> tuple[list[dict], Optional[dict]]:
    """
    Returns (actions_list, context_request_recommendation).
    context_request_recommendation is None when no REQUEST_CONTEXT action is generated.
    """
    actions = []
    context_request_recommendation: Optional[dict] = None

    if missing_context:
        # Use the recommendation engine to select the right template
        rec_ctx = build_recommendation_context(
            case_id=case.case_id,
            case_type=getattr(case, "case_type", None) or "PATIENT_ACCESS_REVIEW",
            user_display_name=case.user_name or case.user_id,
            user_username=case.user_id,
            user_role=subject_role,
            pattern_type=case.pattern_type or "",
            selected_action="REQUEST_CONTEXT",
            selected_reason_code="",
            primary_triggers=primary_triggers,
            missing_context=missing_context,
            linked_event_count=case.event_count or 0,
            unique_patient_token_count=unique_patient_token_count,
            event_type_breakdown=event_type_breakdown,
        )
        recommendation = render_recommendation(rec_ctx)
        context_request_recommendation = recommendation

        reason_code = recommendation.get("reason_code") or select_reason_code(rec_ctx, recommendation["intent"])
        reason = get_reason_for_action(db, "REQUEST_CONTEXT", reason_code)
        if not reason:
            # fall back to first compatible code
            for code in TEMPLATE_REGISTRY[recommendation["template_id"]]["compatible_reason_codes"]:
                reason = get_reason_for_action(db, "REQUEST_CONTEXT", code)
                if reason:
                    reason_code = code
                    break

        actions.append(
            {
                "action": "REQUEST_CONTEXT",
                "intent": recommendation["intent"],
                "reason_code": reason_code if reason else None,
                "reason_label": reason.label if reason else None,
                "rationale": "Additional context is needed before the access pattern can be reviewed confidently.",
                # question_text → prefilled into the context_question form field
                "question_text": recommendation["question_text"],
                # suggested_note → prefilled into the notes field
                "suggested_note": recommendation["suggested_note_text"],
                # Recommendation metadata for UI / provenance
                "suggested_requested_from_role": recommendation["suggested_requested_from_role"],
                "template_id": recommendation["template_id"],
                "template_version": recommendation["template_version"],
                "rationale_labels": recommendation["rationale_labels"],
            }
        )

    if case.severity in ("P0_CRITICAL", "P1_HIGH") or case.breach_risk or any(t.get("rule_id") == "R-06" for t in primary_triggers):
        escalate_rc = "HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW" if case.pattern_type == "VOLUME_SPIKE" else "POSSIBLE_CREDENTIAL_MISUSE"
        reason = get_reason_for_action(db, "ESCALATED", escalate_rc)
        actions.append(
            {
                "action": "ESCALATED",
                "reason_code": escalate_rc if reason else None,
                "reason_label": reason.label if reason else None,
                "rationale": "Escalation may be appropriate when severity is high, context remains unresolved, or credential-risk signals are present.",
                "suggested_note": "Please review this access pattern with privacy/compliance oversight because the business reason could not be confirmed from available context.",
            }
        )

    resolved_reason_code = _resolved_reason_code(subject_role)
    resolved_reason = get_reason_for_action(db, "RESOLVED_AUTHORIZED_ACCESS", resolved_reason_code)
    actions.append(
        {
            "action": "RESOLVED_AUTHORIZED_ACCESS",
            "reason_code": resolved_reason_code if resolved_reason else None,
            "reason_label": resolved_reason.label if resolved_reason else None,
            "rationale": "Use when the reviewer confirms valid treatment, payment, or operations context for the linked patient-record access.",
            "suggested_note": "Access was reviewed and documented as consistent with expected treatment, payment, or operations activity.",
        }
    )

    false_positive_reason = get_reason_for_action(db, "FALSE_POSITIVE", "RULE_THRESHOLD_TOO_LOW")
    actions.append(
        {
            "action": "FALSE_POSITIVE",
            "reason_code": "RULE_THRESHOLD_TOO_LOW" if false_positive_reason else None,
            "reason_label": false_positive_reason.label if false_positive_reason else None,
            "rationale": "Use when the threshold, log mapping, or workflow pattern is determined to be non-actionable.",
            "suggested_note": "This case was reviewed and appears to reflect threshold sensitivity or expected workflow rather than a reviewable exception.",
        }
    )

    return actions, context_request_recommendation


def _resolved_reason_code(subject_role: str) -> str:
    role_lower = (subject_role or "").lower()
    if "bill" in role_lower:
        return "BILLING_OR_PAYMENT_OPERATIONS"
    if any(word in role_lower for word in ("physician", "doctor", "nurse", "clinician", "provider")):
        return "TREATMENT_RELATIONSHIP_CONFIRMED"
    return "ADMINISTRATIVE_OPERATIONS"


def _reviewer_guidance(baseline: dict, missing_context: list[dict]) -> list[str]:
    guidance = [
        "TrustPulse supports human review of patient-access appropriateness and does not determine whether access was inappropriate, a breach, or a HIPAA violation.",
        "Use linked OpenEMR-derived evidence to confirm treatment, payment, or operations context before dispositioning the case.",
    ]
    if baseline.get("maturity", "COLD_START") == "COLD_START":
        guidance.append("Cold-start baseline: thresholds are conservative and confidence is lower until more historical activity is available.")
    if missing_context:
        guidance.append("If key context is unavailable, prefer requesting context rather than inferring intent from telemetry alone.")
    return guidance


def _evidence_limitations(events: list[NormalizedEvent], baseline: dict, missing_context: list[dict]) -> list[str]:
    limitations = [
        "Assessment is derived from linked OpenEMR-derived audit events only.",
        "Patient identifiers remain tokenized in the assessment and export.",
    ]
    if baseline.get("maturity", "COLD_START") == "COLD_START":
        limitations.append("Behavioral thresholds are conservative during cold-start baseline maturity.")
    if any(not e.ip_address for e in events if _is_patient_access_event(e)):
        limitations.append("Source IP or workstation attribution is incomplete for one or more linked events.")
    if missing_context:
        limitations.extend(item["effect_on_confidence"] for item in missing_context if item.get("effect_on_confidence"))
    return limitations


def _telemetry_status(events: list[NormalizedEvent], missing_context: list[dict]) -> dict:
    patient_access_events = [e for e in events if _is_patient_access_event(e)]
    status = "HEALTHY" if events else "UNKNOWN"
    label = "Healthy" if events else "Unknown"
    description = (
        "TrustPulse successfully read and interpreted OpenEMR audit telemetry."
        if events else
        "Telemetry status could not be determined."
    )
    if missing_context:
        status = "COVERAGE_WARNING"
        label = "Coverage Warning"
        description = "Some OpenEMR context was unavailable for this review, which lowers confidence in the explanation of access."
    return {
        "status": status,
        "label": label,
        "description": description,
        "linked_openemr_evidence": "AVAILABLE" if events else "UNAVAILABLE",
        "patient_access_events_present": bool(patient_access_events),
        "ip_context": "AVAILABLE" if patient_access_events and all(e.ip_address for e in patient_access_events) else "PARTIAL" if any(e.ip_address for e in patient_access_events) else "UNAVAILABLE",
        "missing_context_count": len(missing_context),
    }


def _summary(case: Case, linked_event_count: int, unique_patient_count: int, subject_role: str, baseline: dict) -> str:
    role_label = (subject_role or "User").replace("_", " ").title()
    maturity = baseline.get("maturity", "COLD_START")
    if case.pattern_type == "VOLUME_SPIKE":
        return f"{role_label} user accessed {linked_event_count} linked records across {unique_patient_count} tokenized patients, exceeding current volume thresholds for review."
    if case.pattern_type == "OFF_HOURS":
        return f"{role_label} user accessed patient records during an after-hours window that may require review against expected workflow."
    if case.pattern_type == "CREDENTIAL_RISK":
        return f"{role_label} user showed patient-access activity near authentication risk signals, requiring human review of appropriateness and account use."
    return f"{role_label} user access pattern requires patient-access appropriateness review. Baseline maturity is {maturity}."


def _quick_review_reason(case: Case, linked_event_count: int, unique_patient_count: int, subject_role: str, primary_triggers: list[dict]) -> str:
    role_label = (subject_role or "User").replace("_", " ").title()
    first_trigger = primary_triggers[0] if primary_triggers else {}
    if case.pattern_type == "VOLUME_SPIKE":
        return f"{role_label} user accessed {linked_event_count} patient records, above the expected review threshold."
    if case.pattern_type == "OFF_HOURS":
        return f"{role_label} user accessed patient records outside the usual work-hour review window."
    if case.pattern_type == "CREDENTIAL_RISK":
        return f"{role_label} user accessed patient records near unusual sign-in activity and the case needs context."
    if first_trigger.get("rule_name"):
        return f"This case was opened because {first_trigger.get('rule_name').lower()} requires review."
    return f"{role_label} user access requires review."


def _guidance_rationale(subject_role: str, case: Case, primary_triggers: list[dict], missing_context: list[dict]) -> str:
    return _checklist_rationale(subject_role, case, primary_triggers, missing_context)


def _missing_information_summary(missing_context: list[dict]) -> list[str]:
    summary = []
    for item in missing_context[:3]:
        label = item.get("context") or "Context"
        reason = item.get("reason") or "Not available from accessible OpenEMR logs."
        summary.append(f"{label} was not available from the accessible OpenEMR logs. {reason}")
    return summary


def _build_related_auth_signals(case: Case, events: list[NormalizedEvent], db: Session) -> list[dict]:
    """Summarize authentication events near the case's time window (±1 hr) for the same user."""
    if not events:
        return []
    times = [e.event_time for e in events if e.event_time]
    if not times:
        return []
    window_start = min(times) - timedelta(hours=1)
    window_end   = max(times) + timedelta(hours=1)
    auth_evs = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.user_id == case.user_id,
            NormalizedEvent.event_type.in_(list(AUTHENTICATION_EVENT_TYPES)),
            NormalizedEvent.event_time >= window_start,
            NormalizedEvent.event_time <= window_end,
        )
        .order_by(NormalizedEvent.event_time.asc())
        .all()
    )
    grouped: dict[str, list[NormalizedEvent]] = {}
    for event in auth_evs:
        grouped.setdefault(event.event_type or "authentication", []).append(event)
    summaries = []
    for event_type, rows in grouped.items():
        summaries.append(
            {
                "event_type": event_type,
                "count": len(rows),
                "time_window_start": rows[0].event_time.isoformat() if rows[0].event_time else None,
                "time_window_end": rows[-1].event_time.isoformat() if rows[-1].event_time else None,
                "explanation": f"{len(rows)} {event_type.replace('_', ' ')} attempt{'s' if len(rows) != 1 else ''} occurred near this patient-access review window.",
            }
        )
    return summaries


# ── Authentication case assessment ────────────────────────────────────────────

def build_auth_review_assessment(case: Case, events: list[NormalizedEvent], db: Session) -> dict:
    """Returns an assessment dict for authentication-review case types."""
    subject_role = _subject_role(events)
    event_type_breakdown = dict(Counter((e.event_type or "unknown") for e in events))
    failed_events = [e for e in events if (e.event_type or "").lower() == "failed_login"]
    suggested_actions = _suggested_auth_actions(case, db)
    primary_action  = suggested_actions[0] if suggested_actions else None
    alternate_action = suggested_actions[1] if len(suggested_actions) > 1 else None
    things_to_confirm = build_things_to_confirm(
        case,
        {
            "case_type": case.case_type or CASE_TYPE_AUTHENTICATION_REVIEW,
            "subject_role": subject_role,
            "primary_triggers": [],
            "missing_context": [],
            "primary_suggested_action": primary_action,
            "primary_suggested_reason_code": primary_action.get("reason_code") if primary_action else None,
            "pattern_type": case.pattern_type or "",
        },
    )
    checklist_items = things_to_confirm["items"]
    modifiers = _auth_modifiers(case, failed_events)

    successful_after_failures = case.pattern_type in ("SUCCESSFUL_LOGIN_AFTER_FAILURES", "PATIENT_ACCESS_AFTER_AUTH_FAILURES")
    patient_access_after_login = case.pattern_type == "PATIENT_ACCESS_AFTER_AUTH_FAILURES"
    return {
        "case_type": case.case_type or CASE_TYPE_AUTHENTICATION_REVIEW,
        "summary": _auth_summary(case, event_type_breakdown, failed_events),
        "subject": {
            "display_name": case.user_name or next((e.user_name for e in events if e.user_name), case.user_id),
            "username": case.user_id,
            "role": subject_role,
            "department": _subject_department(events),
        },
        "subject_user": case.user_name or next((e.user_name for e in events if e.user_name), case.user_id),
        "subject_username": case.user_id,
        "subject_role": subject_role,
        "subject_department": _subject_department(events),
        "time_window_start": case.date_start.isoformat() if case.date_start else None,
        "time_window_end":   case.date_end.isoformat()   if case.date_end   else None,
        "event_count": case.event_count or len(events),
        "linked_event_count": len(events),
        "linked_event_sequence": [(e.event_type or "unknown") for e in sorted(events, key=lambda e: e.event_time or datetime.min)],
        "unique_patient_token_count": 0,
        "event_type_breakdown": event_type_breakdown,
        "failed_login_count": len(failed_events),
        "authentication_summary": {
            "failed_login_count": len(failed_events),
            "successful_login_after_failures": successful_after_failures,
            "patient_access_after_login": patient_access_after_login,
            "window_minutes": (
                15 if patient_access_after_login else
                10 if successful_after_failures or case.pattern_type == "FAILED_LOGIN_BURST" else
                5
            ),
            "review_focus": (
                "Failed logins followed by patient-record access"
                if patient_access_after_login else
                "Failed logins followed by a successful login"
                if successful_after_failures else
                "Repeated failed login attempts"
            ),
        },
        "quick_review_reason": _quick_auth_review_reason(case, failed_events),
        "quick_review_checks": checklist_items[:3],
        "modifiers": modifiers,
        "things_to_confirm": things_to_confirm,
        "primary_suggested_action": primary_action,
        "primary_suggested_reason_code": primary_action.get("reason_code") if primary_action else None,
        "alternate_suggested_action": alternate_action,
        "suggested_review_actions": suggested_actions,
        "suggested_reason_codes": [
            {"action": a.get("action"), "reason_code": a.get("reason_code"), "reason_label": a.get("reason_label")}
            for a in suggested_actions if a.get("reason_code")
        ],
        "missing_context": [],
        "missing_information_summary": [],
        "guidance_rationale": "Generated from structured authentication and account-access case facts.",
        "reviewer_guidance": [
            "TrustPulse supports human review of authentication activity and does not automatically determine whether credential misuse occurred.",
            "Review linked authentication events with the user's supervisor or IT/Security team for context.",
        ],
        "evidence_limitations": [
            "Assessment is derived from authentication event logs in OpenEMR only.",
            "Patient-access context is assessed separately in patient-access review cases.",
        ],
        "context_request_recommendation": None,
        "related_authentication_signals": [
            {
                "event_type": event_type,
                "count": len(rows),
                "time_window_start": rows[0].event_time.isoformat() if rows[0].event_time else None,
                "time_window_end": rows[-1].event_time.isoformat() if rows[-1].event_time else None,
                "explanation": f"{len(rows)} {event_type.replace('_', ' ')} event{'s' if len(rows) != 1 else ''} were linked for this authentication review window.",
            }
            for event_type, rows in {
                et: [e for e in sorted(events, key=lambda e: e.event_time or datetime.min) if (e.event_type or "") == et]
                for et in {e.event_type or "authentication" for e in events}
            }.items()
            if rows
        ],
        "technical_details": {
            "triggered_rules": [],
            "baseline_maturity": "N/A",
            "baseline_note": "",
            "missing_context_details": [],
            "missing_source_details": [],
            "check_sources": [],
        },
    }


def _auth_summary(case: Case, event_type_breakdown: dict, failed_events: list[NormalizedEvent]) -> str:
    n = event_type_breakdown.get("failed_login", 0)
    user = case.user_name or case.user_id
    after_hours = _failed_events_after_hours(failed_events)
    if case.pattern_type == "PATIENT_ACCESS_AFTER_AUTH_FAILURES":
        return f"{user} had {n} failed login attempt{'s' if n!=1 else ''} followed by patient record access, requiring authentication and access review."
    if case.pattern_type == "SUCCESSFUL_LOGIN_AFTER_FAILURES":
        return f"{user} successfully authenticated after {n} failed login attempt{'s' if n!=1 else ''}, which may indicate credential testing or account compromise."
    if case.pattern_type == "FAILED_LOGIN_BURST":
        if after_hours:
            return f"{user} had {n} failed login attempt{'s' if n!=1 else ''} in a short after-hours window, requiring account security review."
        return f"{user} had {n} failed login attempt{'s' if n!=1 else ''} in a short window, requiring account security review."
    if case.pattern_type == "AFTER_HOURS_LOGIN_ATTEMPT":
        return f"{user} had failed login attempt{'s' if n!=1 else ''} outside business hours, which requires review for expected after-hours access patterns."
    if after_hours:
        return f"{user} had {n} failed login attempt{'s' if n!=1 else ''} outside business hours, requiring authentication review."
    return f"{user} had {n} failed login attempt{'s' if n!=1 else ''} exceeding the review threshold."


def _quick_auth_review_reason(case: Case, failed_events: list) -> str:
    after_hours = _failed_events_after_hours(failed_events)
    if case.pattern_type == "PATIENT_ACCESS_AFTER_AUTH_FAILURES":
        return "Failed login attempts were followed by patient-record access."
    if case.pattern_type == "SUCCESSFUL_LOGIN_AFTER_FAILURES":
        return "Failed login attempts were followed by a successful login."
    if case.pattern_type == "FAILED_LOGIN_BURST":
        if after_hours:
            return "This account had repeated failed login attempts outside normal business hours."
        return "This account had repeated failed login attempts during the review window."
    if case.pattern_type == "AFTER_HOURS_LOGIN_ATTEMPT":
        return "This account had repeated failed login attempts outside normal business hours."
    if after_hours:
        return "This account had repeated failed login attempts outside normal business hours."
    return "This account had repeated failed login attempts during the review window."


def _failed_events_after_hours(failed_events: list[NormalizedEvent]) -> bool:
    business_start, business_end = 7, 19
    return any(
        (event.hour_of_day is not None) and ((event.hour_of_day or 0) < business_start or (event.hour_of_day or 0) >= business_end)
        for event in failed_events
    )


def _auth_modifiers(case: Case, failed_events: list[NormalizedEvent]) -> list[dict]:
    after_hours = _failed_events_after_hours(failed_events)
    modifiers = []
    if after_hours and case.pattern_type in ("FAILED_LOGIN_ACTIVITY", "FAILED_LOGIN_BURST", "SUCCESSFUL_LOGIN_AFTER_FAILURES", "PATIENT_ACCESS_AFTER_AUTH_FAILURES"):
        modifiers.append({"key": "after_hours", "label": "After-hours"})
    elif case.pattern_type == "AFTER_HOURS_LOGIN_ATTEMPT":
        modifiers.append({"key": "after_hours", "label": "After-hours"})
    return modifiers


def _auth_review_checklist(case: Case) -> tuple[list[str], list[dict]]:
    if case.pattern_type == "PATIENT_ACCESS_AFTER_AUTH_FAILURES":
        raw = [
            ("Confirm whether the user recognizes the login activity.", {"type": "pattern", "value": "PATIENT_ACCESS_AFTER_AUTH_FAILURES"}),
            ("Confirm whether the patient access matches expected work.", {"type": "pattern", "value": "auth"}),
            ("Escalate to IT/security if the activity is not recognized.", {"type": "next_step", "value": "ESCALATED"}),
        ]
    elif case.pattern_type == "SUCCESSFUL_LOGIN_AFTER_FAILURES":
        raw = [
            ("Confirm whether the user recognizes the login attempts.", {"type": "pattern", "value": "SUCCESSFUL_LOGIN_AFTER_FAILURES"}),
            ("Confirm whether password reset or account support was needed.", {"type": "pattern", "value": "auth"}),
            ("Confirm whether the device or work location was expected.", {"type": "next_step", "value": "PASSWORD_RESET_REQUIRED"}),
        ]
    elif case.pattern_type == "FAILED_LOGIN_BURST":
        raw = [
            ("Confirm whether the user recognizes the login attempts.", {"type": "pattern", "value": "FAILED_LOGIN_BURST"}),
            ("Confirm whether password reset or account support was needed.", {"type": "pattern", "value": "auth"}),
            ("Confirm whether the device or work location was expected.", {"type": "next_step", "value": "ESCALATED"}),
        ]
    elif case.pattern_type == "AFTER_HOURS_LOGIN_ATTEMPT":
        raw = [
            ("Confirm whether the user recognizes the login attempts.", {"type": "pattern", "value": "AFTER_HOURS_LOGIN_ATTEMPT"}),
            ("Confirm whether password reset or account support was needed.", {"type": "pattern", "value": "auth"}),
            ("Confirm whether the device or work location was expected.", {"type": "rule", "value": "R-09"}),
        ]
    else:
        raw = [
            ("Confirm whether the user recognizes the login attempts.", {"type": "pattern", "value": "FAILED_LOGIN_ACTIVITY"}),
            ("Confirm whether password reset or account support was needed.", {"type": "pattern", "value": "auth"}),
            ("Confirm whether the device or work location was expected.", {"type": "next_step", "value": "ESCALATED"}),
        ]
    items, sources, seen = [], [], set()
    for text, source in raw:
        key = text.strip().lower()
        if key not in seen:
            seen.add(key)
            items.append(text.strip())
            sources.append({"item": text.strip(), "source": source})
    return items, sources


def _suggested_auth_actions(case: Case, db: Session) -> list[dict]:
    actions = []

    if case.pattern_type in ("PATIENT_ACCESS_AFTER_AUTH_FAILURES", "SUCCESSFUL_LOGIN_AFTER_FAILURES"):
        esc_rc = "ACCOUNT_SHARING_SUSPECTED"
        esc_r  = get_reason_for_action(db, "ESCALATED", esc_rc)
        if not esc_r:
            esc_rc = "POSSIBLE_CREDENTIAL_MISUSE"
            esc_r  = get_reason_for_action(db, "ESCALATED", esc_rc)
        actions.append({
            "action": "ESCALATED",
            "reason_code": esc_rc if esc_r else None,
            "reason_label": esc_r.label if esc_r else None,
            "rationale": "Escalation is recommended when patient access follows authentication failures or when credential misuse is possible.",
            "suggested_note": "Authentication event pattern requires IT/Security review. Please confirm whether the account access was authorized.",
        })

    template = TEMPLATE_REGISTRY.get("REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION", {})
    req_rc   = "USER_CONFIRMED_ACTIVITY"
    req_r    = get_reason_for_action(db, "REQUEST_CONTEXT", req_rc)
    if not req_r:
        req_rc = "KNOWN_REMOTE_ACCESS"
        req_r  = get_reason_for_action(db, "REQUEST_CONTEXT", req_rc)
    if not req_r:
        req_rc = "PASSWORD_RESET_REQUIRED"
        req_r  = get_reason_for_action(db, "REQUEST_CONTEXT", req_rc)
    actions.append({
        "action": "REQUEST_CONTEXT",
        "intent": "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION",
        "reason_code": req_rc if req_r else None,
        "reason_label": req_r.label if req_r else None,
        "action_label": "Request account confirmation",
        "rationale": "Request confirmation from the user or supervisor before dispositioning this authentication case.",
        "suggested_note": template.get("suggested_note_text", ""),
        "question_text": template.get("question_text", ""),
        "suggested_requested_from_role": template.get("suggested_requested_from_role", "IT / Security Admin"),
        "template_id": "REQUEST_ACCOUNT_ACTIVITY_CONFIRMATION",
        "template_version": template.get("template_version", "1.0"),
        "rationale_labels": template.get("rationale_labels", []),
    })

    res_rc = "USER_CONFIRMED_ACTIVITY"
    res_r  = get_reason_for_action(db, "RESOLVED_NO_ISSUE", res_rc)
    if not res_r:
        res_rc = "KNOWN_WORKFLOW_PATTERN"
        res_r  = get_reason_for_action(db, "RESOLVED_NO_ISSUE", res_rc)
    actions.append({
        "action": "RESOLVED_NO_ISSUE",
        "reason_code": res_rc if res_r else None,
        "reason_label": res_r.label if res_r else None,
        "rationale": "Use when the authentication activity is confirmed as expected behavior for the account holder.",
        "suggested_note": "Authentication activity was reviewed and confirmed as expected user behavior.",
    })

    return actions
