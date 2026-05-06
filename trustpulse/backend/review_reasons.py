from datetime import datetime

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from db.models import ReviewReason


REVIEW_REASON_DEFINITIONS = [
    {
        "code": "BILLING_OR_PAYMENT_OPERATIONS",
        "label": "Billing or payment operations confirmed",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS"],
        "requires_note": True,
    },
    {
        "code": "TREATMENT_RELATIONSHIP_CONFIRMED",
        "label": "Treatment relationship confirmed",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS"],
        "requires_note": True,
    },
    {
        "code": "SCHEDULED_APPOINTMENT_CONTEXT",
        "label": "Scheduled appointment context verified",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS"],
        "requires_note": True,
    },
    {
        "code": "CARE_COORDINATION",
        "label": "Care coordination or referral-related access",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS"],
        "requires_note": True,
    },
    {
        "code": "EMERGENCY_OR_ON_CALL_ACCESS",
        "label": "Emergency or on-call access",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS"],
        "requires_note": True,
    },
    {
        "code": "ADMINISTRATIVE_OPERATIONS",
        "label": "Administrative operations confirmed",
        "category": "resolved",
        "applies_to_actions": ["RESOLVED_AUTHORIZED_ACCESS", "RESOLVED_NO_ISSUE"],
        "requires_note": True,
    },
    {
        "code": "NEED_BILLING_JUSTIFICATION",
        "label": "Need billing/payment justification",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "NEED_PROVIDER_CONFIRMATION",
        "label": "Need provider confirmation",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "NEED_APPOINTMENT_CONTEXT",
        "label": "Need appointment or scheduling context",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "NEED_SUPERVISOR_REVIEW",
        "label": "Need supervisor review",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "INSUFFICIENT_LOG_CONTEXT",
        "label": "Available OpenEMR logs do not provide enough context",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "POSSIBLE_INAPPROPRIATE_ACCESS",
        "label": "Possible inappropriate patient access",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "POSSIBLE_CREDENTIAL_MISUSE",
        "label": "Possible credential misuse or account sharing",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW",
        "label": "High-volume access requires privacy review",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "REPEATED_ACCESS_PATTERN",
        "label": "Repeated access pattern requires escalation",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "POSSIBLE_BREACH_RISK",
        "label": "Possible breach-risk condition for privacy review",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "KNOWN_WORKFLOW_PATTERN",
        "label": "Known operational workflow pattern",
        "category": "false_positive",
        "applies_to_actions": ["RESOLVED_NO_ISSUE", "FALSE_POSITIVE", "SUPPRESSED"],
        "requires_note": True,
    },
    {
        "code": "RULE_THRESHOLD_TOO_LOW",
        "label": "Rule threshold too sensitive",
        "category": "false_positive",
        "applies_to_actions": ["FALSE_POSITIVE", "SUPPRESSED"],
        "requires_note": True,
    },
    {
        "code": "TEST_OR_DEMO_ACTIVITY",
        "label": "Test or demo activity",
        "category": "false_positive",
        "applies_to_actions": ["RESOLVED_NO_ISSUE", "FALSE_POSITIVE"],
        "requires_note": True,
    },
    {
        "code": "SYSTEM_GENERATED_ACTIVITY",
        "label": "System or admin-generated activity",
        "category": "false_positive",
        "applies_to_actions": ["FALSE_POSITIVE", "SUPPRESSED"],
        "requires_note": True,
    },
    {
        "code": "LOG_MAPPING_ERROR",
        "label": "Log mapping or normalization issue",
        "category": "false_positive",
        "applies_to_actions": ["FALSE_POSITIVE"],
        "requires_note": True,
    },
    # ── Authentication review reason codes ──────────────────────────────────
    {
        "code": "USER_CONFIRMED_ACTIVITY",
        "label": "User confirmed their own login activity",
        "category": "resolved",
        "applies_to_actions": ["REQUEST_CONTEXT", "RESOLVED_AUTHORIZED_ACCESS", "RESOLVED_NO_ISSUE"],
        "requires_note": True,
    },
    {
        "code": "PASSWORD_RESET_REQUIRED",
        "label": "Password reset required as remediation",
        "category": "needs_context",
        "applies_to_actions": ["REQUEST_CONTEXT"],
        "requires_note": True,
    },
    {
        "code": "KNOWN_REMOTE_ACCESS",
        "label": "Known remote or VPN access pattern confirmed",
        "category": "resolved",
        "applies_to_actions": ["REQUEST_CONTEXT", "RESOLVED_AUTHORIZED_ACCESS", "RESOLVED_NO_ISSUE"],
        "requires_note": True,
    },
    {
        "code": "ACCOUNT_SHARING_SUSPECTED",
        "label": "Possible account sharing or shared credential use",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
    {
        "code": "ESCALATED_TO_SECURITY",
        "label": "Escalated to IT/Security for investigation",
        "category": "escalation",
        "applies_to_actions": ["ESCALATED"],
        "requires_note": True,
    },
]


SENSITIVE_REASON_ACTIONS = {
    "REQUEST_CONTEXT",
    "ESCALATED",
    "RESOLVED_AUTHORIZED_ACCESS",
    "RESOLVED_NO_ISSUE",
    "FALSE_POSITIVE",
    "SUPPRESSED",
    "REOPENED",
}


def ensure_review_reason_schema(engine) -> None:
    inspector = inspect(engine)
    if "case_actions" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("case_actions")}
    with engine.begin() as conn:
        if "reason_code" not in cols:
            conn.execute(text("ALTER TABLE case_actions ADD COLUMN reason_code VARCHAR(50)"))
        if "reason_label_snapshot" not in cols:
            conn.execute(text("ALTER TABLE case_actions ADD COLUMN reason_label_snapshot VARCHAR(200)"))


def seed_review_reasons(db: Session) -> None:
    existing = {r.code: r for r in db.query(ReviewReason).all()}
    changed = False
    for item in REVIEW_REASON_DEFINITIONS:
        row = existing.get(item["code"])
        if row:
            if (
                row.label != item["label"]
                or row.category != item["category"]
                or (row.applies_to_actions or []) != item["applies_to_actions"]
                or bool(row.requires_note) != bool(item["requires_note"])
                or not row.is_active
            ):
                row.label = item["label"]
                row.category = item["category"]
                row.applies_to_actions = item["applies_to_actions"]
                row.requires_note = item["requires_note"]
                row.is_active = True
                changed = True
            continue
        db.add(ReviewReason(created_at=datetime.utcnow(), is_active=True, **item))
        changed = True
    if changed:
        db.commit()


def get_reason_options_for_action(db: Session, action: str) -> list[ReviewReason]:
    query = db.query(ReviewReason).filter(ReviewReason.is_active == True)
    rows = query.order_by(ReviewReason.category.asc(), ReviewReason.label.asc()).all()
    if action == "REOPENED":
        return rows
    return [r for r in rows if action in (r.applies_to_actions or [])]


def get_reason_for_action(db: Session, action: str, code: str | None) -> ReviewReason | None:
    if not code:
        return None
    candidates = get_reason_options_for_action(db, action)
    return next((r for r in candidates if r.code == code), None)
