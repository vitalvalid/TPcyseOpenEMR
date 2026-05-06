"""
Event type classification for TrustPulse case routing.
Separates authentication signals from patient-access events so each
can be handled by the appropriate case-generation path.
"""

PATIENT_ACCESS_EVENT = "PATIENT_ACCESS_EVENT"
AUTHENTICATION_EVENT = "AUTHENTICATION_EVENT"
SYSTEM_EVENT = "SYSTEM_EVENT"
UNKNOWN_EVENT = "UNKNOWN_EVENT"

AUTHENTICATION_EVENT_TYPES = frozenset({
    "failed_login",
    "login",
    "successful_login",
    "logout",
    "account_lockout",
    "password_failure",
    "authentication_failure",
    "authentication_success",
})

PATIENT_ACCESS_EVENT_TYPES = frozenset({
    "patient_access",
    "patient_view",
    "patient_record",
    "record_modify",
    "export",
    "download",
    "report_export",
})

SYSTEM_EVENT_TYPES = frozenset({
    "admin_action",
    "system_action",
    "system_audit",
    "admin_audit",
    "system",
    "admin",
    "background",
    "service",
})


def classify_event_category(event_type: str) -> str:
    et = (event_type or "").lower().strip()
    if et in AUTHENTICATION_EVENT_TYPES:
        return AUTHENTICATION_EVENT
    if et in PATIENT_ACCESS_EVENT_TYPES:
        return PATIENT_ACCESS_EVENT
    if et in SYSTEM_EVENT_TYPES:
        return SYSTEM_EVENT
    return UNKNOWN_EVENT


def classify_event(event_type: str) -> str:
    """Return the classification bucket for an event type string."""
    category = classify_event_category(event_type)
    if category == AUTHENTICATION_EVENT:
        return "authentication"
    if category == PATIENT_ACCESS_EVENT:
        return "patient_access"
    if category == SYSTEM_EVENT:
        return "system"
    return "other"


def is_auth_event(event_type: str) -> bool:
    return (event_type or "").lower().strip() in AUTHENTICATION_EVENT_TYPES


def is_patient_access_event(event_type: str, patient_id: object = None) -> bool:
    et = (event_type or "").lower().strip()
    if et in AUTHENTICATION_EVENT_TYPES:
        return False
    if et in PATIENT_ACCESS_EVENT_TYPES:
        return True
    # If patient_id is present and event_type isn't explicitly auth, treat as patient access
    return bool(patient_id)
