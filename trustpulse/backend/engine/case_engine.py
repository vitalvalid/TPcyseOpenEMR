"""
Groups NormalizedEvents into Cases using 7-day window buckets per user+pattern.
Cases are the primary unit of work for the compliance officer.
"""
import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from db.models import NormalizedEvent, Case, KnownPattern

# ── Pattern mapping ───────────────────────────────────────────────────────────

RULE_TO_PATTERN = {
    "R-06": "CREDENTIAL_RISK",
    "R-09": "CREDENTIAL_RISK",
    "R-05": "INSIDER_SNOOPING",
    "R-07": "DATA_EXPORT",
    "R-02": "VOLUME_SPIKE",
    "R-08": "VOLUME_SPIKE",
    "R-04": "CROSS_DEPT",
    "R-01": "OFF_HOURS",
    "R-03": "OFF_HOURS",
    "R-10": "OFF_HOURS",
}

PATTERN_PRIORITY = {
    "CREDENTIAL_RISK": 6,
    "INSIDER_SNOOPING": 5,
    "DATA_EXPORT": 4,
    "VOLUME_SPIKE": 3,
    "CROSS_DEPT": 2,
    "OFF_HOURS": 1,
}

PATTERN_TITLES = {
    "INSIDER_SNOOPING": "Unusual Patient Access",
    "CREDENTIAL_RISK":  "Credential Risk Detected",
    "DATA_EXPORT":      "Suspicious Data Export",
    "VOLUME_SPIKE":     "Access Volume Spike",
    "CROSS_DEPT":       "Cross-Department Access",
    "OFF_HOURS":        "After-Hours Activity",
}

HIPAA_BY_PATTERN = {
    "INSIDER_SNOOPING": ["§164.312(b)", "§164.502(b)", "§164.308(a)(1)"],
    "CREDENTIAL_RISK":  ["§164.312(d)", "§164.308(a)(5)", "§164.312(b)"],
    "DATA_EXPORT":      ["§164.312(c)(1)", "§164.312(b)", "§164.308(a)(1)"],
    "VOLUME_SPIKE":     ["§164.502(b)", "§164.312(b)"],
    "CROSS_DEPT":       ["§164.502(b)", "§164.308(a)(3)"],
    "OFF_HOURS":        ["§164.308(a)(3)", "§164.312(b)"],
}

ACTION_BY_SEVERITY_PATTERN = {
    ("P0_CRITICAL", "INSIDER_SNOOPING"): "ESCALATED",
    ("P0_CRITICAL", "DATA_EXPORT"):      "ESCALATED",
    ("P0_CRITICAL", "CREDENTIAL_RISK"):  "ESCALATED",
    ("P0_CRITICAL", "VOLUME_SPIKE"):     "ESCALATED",
    ("P0_CRITICAL", "CROSS_DEPT"):       "ESCALATED",
    ("P0_CRITICAL", "OFF_HOURS"):        "ESCALATED",
    ("P1_HIGH", "CREDENTIAL_RISK"):      "FOLLOW_UP",
    ("P1_HIGH", "INSIDER_SNOOPING"):     "FOLLOW_UP",
    ("P1_HIGH", "DATA_EXPORT"):          "FOLLOW_UP",
    ("P1_HIGH", "VOLUME_SPIKE"):         "FOLLOW_UP",
    ("P1_HIGH", "CROSS_DEPT"):           "FOLLOW_UP",
    ("P1_HIGH", "OFF_HOURS"):            "REVIEWED",
    ("P2_MEDIUM", "CREDENTIAL_RISK"):    "FOLLOW_UP",
    ("P2_MEDIUM", "VOLUME_SPIKE"):       "REVIEWED",
    ("P2_MEDIUM", "INSIDER_SNOOPING"):   "FOLLOW_UP",
}


def _dominant_pattern(triggered_rules: list) -> str:
    if not triggered_rules:
        return "OFF_HOURS"
    patterns = [
        RULE_TO_PATTERN.get(r.get("rule_id", ""), "OFF_HOURS")
        for r in triggered_rules if r.get("fired")
    ]
    if not patterns:
        return "OFF_HOURS"
    return max(set(patterns), key=lambda p: PATTERN_PRIORITY.get(p, 0))


def _severity(score: float, breach_risk: bool) -> str:
    if breach_risk or score >= 80:
        return "P0_CRITICAL"
    if score >= 60:
        return "P1_HIGH"
    if score >= 30:
        return "P2_MEDIUM"
    return "P3_LOW"


def _breach_risk(pattern_type: str, score: float) -> bool:
    return pattern_type in ("INSIDER_SNOOPING", "DATA_EXPORT", "CREDENTIAL_RISK") and score >= 55


def _recommended_action(severity: str, pattern_type: str) -> str:
    key = (severity, pattern_type)
    if key in ACTION_BY_SEVERITY_PATTERN:
        return ACTION_BY_SEVERITY_PATTERN[key]
    if severity == "P3_LOW":
        return "REVIEWED"
    return "FOLLOW_UP"


def _stable_case_id(user_id: str, pattern_type: str, week_bucket: int) -> str:
    raw = f"{user_id}|{pattern_type}|{week_bucket}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def _is_suppressed(user_id: str, pattern_type: str, db: Session) -> bool:
    return (
        db.query(KnownPattern)
        .filter(
            KnownPattern.user_id == user_id,
            KnownPattern.pattern_type == pattern_type,
            KnownPattern.active == True,
            KnownPattern.expires_at > datetime.utcnow(),
        )
        .count()
        > 0
    )


# Week bucket anchor: week 0 = first week of 2026
_EPOCH = date(2026, 1, 1)


def generate_cases(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(days=30)
    events = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.event_time >= cutoff,
            NormalizedEvent.risk_score > 5,
        )
        .all()
    )

    # Group: (user_id, pattern_type, week_bucket) → list of events
    groups: Dict[Tuple, List[NormalizedEvent]] = defaultdict(list)
    for ev in events:
        if not ev.triggered_rules:
            continue
        pattern_type = _dominant_pattern(ev.triggered_rules)
        day_offset = (ev.event_time.date() - _EPOCH).days
        week_bucket = max(day_offset // 7, 0)
        groups[(ev.user_id, pattern_type, week_bucket)].append(ev)

    created = 0
    for (user_id, pattern_type, week_bucket), evs in groups.items():
        case_id = _stable_case_id(user_id, pattern_type, week_bucket)
        existing = db.get(Case, case_id)

        if existing and existing.status in ("RESOLVED",):
            continue

        max_score = max(e.risk_score for e in evs)
        br = _breach_risk(pattern_type, max_score)
        severity = _severity(max_score, br)

        suppressed = _is_suppressed(user_id, pattern_type, db)
        if suppressed:
            status = "SUPPRESSED"
        elif existing:
            status = existing.status
        else:
            status = "OPEN"

        user_name = next((e.user_name for e in evs if e.user_name), user_id)
        dates = [e.event_time for e in evs]
        title = f"{PATTERN_TITLES.get(pattern_type, 'Anomalous Activity')} - {user_name}"

        if existing:
            existing.event_count = len(evs)
            existing.date_start = min(dates)
            existing.date_end = max(dates)
            existing.risk_score = max_score
            existing.severity = severity
            existing.breach_risk = br
            existing.breach_deadline = (
                datetime.utcnow() + timedelta(days=60) if br else None
            )
            existing.recommended_action = _recommended_action(severity, pattern_type)
            existing.hipaa_provisions = HIPAA_BY_PATTERN.get(pattern_type, [])
            if existing.status not in ("SUPPRESSED", "RESOLVED", "DISMISSED"):
                existing.status = status
        else:
            db.add(
                Case(
                    case_id=case_id,
                    title=title,
                    severity=severity,
                    pattern_type=pattern_type,
                    user_id=user_id,
                    user_name=user_name,
                    event_count=len(evs),
                    date_start=min(dates),
                    date_end=max(dates),
                    risk_score=max_score,
                    recommended_action=_recommended_action(severity, pattern_type),
                    breach_risk=br,
                    breach_deadline=(
                        datetime.utcnow() + timedelta(days=60) if br else None
                    ),
                    status=status,
                    hipaa_provisions=HIPAA_BY_PATTERN.get(pattern_type, []),
                    created_at=datetime.utcnow(),
                )
            )
            created += 1

    db.commit()
    return created


def get_case_events(case: Case, db: Session) -> List[NormalizedEvent]:
    """Fetch all constituent NormalizedEvents for a case."""
    return (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.user_id == case.user_id,
            NormalizedEvent.event_time >= case.date_start,
            NormalizedEvent.event_time <= case.date_end,
            NormalizedEvent.risk_score > 5,
        )
        .order_by(NormalizedEvent.event_time.asc())
        .all()
    )
