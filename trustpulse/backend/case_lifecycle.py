ACTIVE_CASE_STATUSES = (
    "OPEN",
    "UNDER_REVIEW",
    "NEEDS_CONTEXT",
    "ESCALATED",
    "REOPENED",
)

INACTIVE_CASE_STATUSES = (
    "RESOLVED",
    "FALSE_POSITIVE",
    "SUPPRESSED",
    "CLOSED",
)


def is_active_case_status(status: str) -> bool:
    return status in ACTIVE_CASE_STATUSES
