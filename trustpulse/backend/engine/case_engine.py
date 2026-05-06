"""
Groups NormalizedEvents into Cases using 7-day window buckets per user+pattern.
Cases are the primary unit of work for the compliance officer.
"""
import hashlib
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional

from sqlalchemy.orm import Session

from db.models import NormalizedEvent, Case, CaseEvent, KnownPattern
from engine.event_classification import AUTHENTICATION_EVENT_TYPES, PATIENT_ACCESS_EVENT_TYPES, is_auth_event
from patient_access_review import build_case_assessment, infer_case_type

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
    "OFF_HOURS":        "After-Hours Patient Access",
}

HIPAA_BY_PATTERN = {
    "INSIDER_SNOOPING": ["§164.312(b)", "§164.502(b)", "§164.308(a)(1)"],
    "CREDENTIAL_RISK":  ["§164.312(d)", "§164.308(a)(5)", "§164.312(b)"],
    "DATA_EXPORT":      ["§164.312(c)(1)", "§164.312(b)", "§164.308(a)(1)"],
    "VOLUME_SPIKE":     ["§164.502(b)", "§164.312(b)"],
    "CROSS_DEPT":       ["§164.502(b)", "§164.308(a)(3)"],
    "OFF_HOURS":        ["§164.308(a)(3)", "§164.312(b)"],
}

# ── Authentication pattern mapping ────────────────────────────────────────────

FAILED_LOGIN_REVIEW_THRESHOLD  = 3
FAILED_LOGIN_BURST_THRESHOLD   = 5
FAILED_LOGIN_REVIEW_WINDOW_MIN = 5
FAILED_LOGIN_BURST_WINDOW_MIN  = 10
FAILED_LOGIN_CLUSTER_GAP_MINUTES = 3
SUCCESS_AFTER_FAILURE_WINDOW_MINUTES = 10
PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES = 15
PATIENT_ACCESS_AFTER_LOGIN_WINDOW_MINUTES = PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES

FAILED_LOGIN_EVENT_TYPES = {"failed_login", "password_failure", "authentication_failure"}
SUCCESSFUL_LOGIN_EVENT_TYPES = {"login", "successful_login", "authentication_success"}

AUTH_PATTERN_TITLES = {
    "FAILED_LOGIN_ACTIVITY":              "Failed Login Activity",
    "FAILED_LOGIN_BURST":                 "Failed Login Burst",
    "AFTER_HOURS_LOGIN_ATTEMPT":          "After-Hours Login Attempt",
    "SUCCESSFUL_LOGIN_AFTER_FAILURES":    "Account Access Review",
    "PATIENT_ACCESS_AFTER_AUTH_FAILURES": "Account Misuse Review",
}

AUTH_PATTERN_SCORES = {
    "FAILED_LOGIN_ACTIVITY":              38.0,
    "AFTER_HOURS_LOGIN_ATTEMPT":          38.0,
    "FAILED_LOGIN_BURST":                 62.0,
    "SUCCESSFUL_LOGIN_AFTER_FAILURES":    68.0,
    "PATIENT_ACCESS_AFTER_AUTH_FAILURES": 75.0,
}

AUTH_CASE_TYPES = {
    "FAILED_LOGIN_ACTIVITY":             "AUTHENTICATION_REVIEW",
    "FAILED_LOGIN_BURST":                "AUTHENTICATION_REVIEW",
    "AFTER_HOURS_LOGIN_ATTEMPT":         "AUTHENTICATION_REVIEW",
    "SUCCESSFUL_LOGIN_AFTER_FAILURES":   "ACCOUNT_ACCESS_REVIEW",
    "PATIENT_ACCESS_AFTER_AUTH_FAILURES":"ACCOUNT_MISUSE_REVIEW",
}

AUTH_HIPAA = {
    "FAILED_LOGIN_ACTIVITY":              ["§164.312(d)", "§164.308(a)(5)"],
    "FAILED_LOGIN_BURST":                 ["§164.312(d)", "§164.308(a)(5)", "§164.312(b)"],
    "AFTER_HOURS_LOGIN_ATTEMPT":          ["§164.312(d)", "§164.308(a)(3)"],
    "SUCCESSFUL_LOGIN_AFTER_FAILURES":    ["§164.312(d)", "§164.308(a)(5)", "§164.312(b)"],
    "PATIENT_ACCESS_AFTER_AUTH_FAILURES": ["§164.312(d)", "§164.308(a)(5)", "§164.312(b)", "§164.502(b)"],
}

AUTH_PATTERN_SEVERITY = {
    "FAILED_LOGIN_ACTIVITY": "P2_MEDIUM",
    "AFTER_HOURS_LOGIN_ATTEMPT": "P2_MEDIUM",
    "FAILED_LOGIN_BURST": "P1_HIGH",
    "SUCCESSFUL_LOGIN_AFTER_FAILURES": "P1_HIGH",
    "PATIENT_ACCESS_AFTER_AUTH_FAILURES": "P1_HIGH",
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


def _stable_auth_case_id(user_id: str, cluster_start: datetime) -> str:
    raw = f"{user_id}|AUTH_CLUSTER|{cluster_start.replace(second=0, microsecond=0).isoformat()}"
    return str(uuid.UUID(hashlib.md5(raw.encode()).hexdigest()))


def _select_link_rule(event: NormalizedEvent, pattern_type: str) -> Tuple[str, Optional[str]]:
    fired_rules = [r for r in (event.triggered_rules or []) if r.get("fired")]
    matching = [r for r in fired_rules if RULE_TO_PATTERN.get(r.get("rule_id", "")) == pattern_type]
    selected = max(
        matching or fired_rules,
        key=lambda r: r.get("score_contribution", 0),
        default=None,
    )
    if not selected:
        return pattern_type, None
    return selected.get("rule_name") or pattern_type, selected.get("rule_id")


def _sync_case_event_links(
    case_id: str,
    evs: List[NormalizedEvent],
    pattern_type: str,
    db: Session,
    *,
    replace_existing: bool = False,
) -> None:
    target_ids = {ev.id for ev in evs}
    if replace_existing:
        stale_query = db.query(CaseEvent).filter(CaseEvent.case_id == case_id)
        if target_ids:
            stale_query = stale_query.filter(~CaseEvent.normalized_event_id.in_(target_ids))
        stale_query.delete(synchronize_session=False)
    existing_ids = {
        row[0]
        for row in (
            db.query(CaseEvent.normalized_event_id)
            .filter(CaseEvent.case_id == case_id)
            .all()
        )
    }
    for ev in evs:
        if ev.id in existing_ids:
            continue
        linked_reason, rule_id = _select_link_rule(ev, pattern_type)
        db.add(
            CaseEvent(
                case_id=case_id,
                normalized_event_id=ev.id,
                linked_reason=linked_reason,
                rule_id=rule_id,
                created_at=datetime.utcnow(),
            )
        )


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
    # Exclude authentication events — handled separately by generate_auth_cases
    events = [e for e in events if not is_auth_event(e.event_type or "")]

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

        if existing and existing.status in ("RESOLVED", "FALSE_POSITIVE"):
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
        case_type = infer_case_type(pattern_type, evs)

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
            existing.case_type = case_type
            if existing.status not in ("SUPPRESSED", "RESOLVED", "FALSE_POSITIVE"):
                existing.status = status
            existing.assessment_json = build_case_assessment(existing, evs, db)
        else:
            case = Case(
                case_id=case_id,
                title=title,
                severity=severity,
                pattern_type=pattern_type,
                case_type=case_type,
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
            case.assessment_json = build_case_assessment(case, evs, db)
            db.add(case)
            created += 1

        _sync_case_event_links(case_id, evs, pattern_type, db)

    db.commit()
    return created


def get_case_events(case: Case, db: Session) -> List[NormalizedEvent]:
    """Fetch all constituent NormalizedEvents for a case via exact CaseEvent linkage."""
    return (
        db.query(NormalizedEvent)
        .join(CaseEvent, CaseEvent.normalized_event_id == NormalizedEvent.id)
        .filter(CaseEvent.case_id == case.case_id)
        .order_by(NormalizedEvent.event_time.asc())
        .all()
    )


# ── Authentication case generation ────────────────────────────────────────────

def _threshold_in_window(events: list, threshold: int, window_minutes: int) -> bool:
    """Return True if at least `threshold` events fall within any `window_minutes` sliding window."""
    times = sorted(e.event_time for e in events if e.event_time)
    if len(times) < threshold:
        return False
    window = timedelta(minutes=window_minutes)
    for i in range(len(times) - threshold + 1):
        if times[i + threshold - 1] - times[i] <= window:
            return True
    return False


def _classify_auth_pattern(
    auth_events: list,
    patient_access_events: list,
) -> Optional[str]:
    """
    Return the dominant auth pattern for a user's auth events, or None if thresholds aren't met.
    Precedence (highest first): PATIENT_ACCESS_AFTER_AUTH_FAILURES → SUCCESSFUL_LOGIN_AFTER_FAILURES
    → FAILED_LOGIN_BURST → AFTER_HOURS_LOGIN_ATTEMPT → FAILED_LOGIN_ACTIVITY.
    """
    failed = [
        e for e in auth_events
        if (e.event_type or "").lower() in FAILED_LOGIN_EVENT_TYPES
    ]
    for cluster in reversed(_cluster_failed_logins(failed)):
        pattern_type, _ = _auth_case_pattern_and_events_for_cluster(
            auth_events,
            cluster,
            patient_access_events,
        )
        if pattern_type:
            return pattern_type
    return None


def _cluster_failed_logins(
    failed_events: list[NormalizedEvent],
    *,
    cluster_gap_minutes: int = FAILED_LOGIN_CLUSTER_GAP_MINUTES,
) -> list[list[NormalizedEvent]]:
    failed = sorted([e for e in failed_events if e.event_time], key=lambda e: e.event_time)
    if not failed:
        return []
    gap = timedelta(minutes=cluster_gap_minutes)
    clusters: list[list[NormalizedEvent]] = [[failed[0]]]
    for event in failed[1:]:
        previous = clusters[-1][-1]
        if event.event_time - previous.event_time <= gap:
            clusters[-1].append(event)
        else:
            clusters.append([event])
    return clusters


def _cluster_meets_threshold(
    cluster: list[NormalizedEvent],
    threshold: int,
    window_minutes: int,
) -> bool:
    return _threshold_in_window(cluster, threshold, window_minutes)


def _success_events_after_cluster(
    auth_events: list[NormalizedEvent],
    failed_cluster: list[NormalizedEvent],
) -> list[NormalizedEvent]:
    if not failed_cluster:
        return []
    cluster_end = failed_cluster[-1].event_time
    window_end = cluster_end + timedelta(minutes=SUCCESS_AFTER_FAILURE_WINDOW_MINUTES)
    successes = [
        e for e in auth_events
        if (e.event_type or "").lower() in SUCCESSFUL_LOGIN_EVENT_TYPES
        and e.event_time
        and cluster_end <= e.event_time <= window_end
    ]
    return sorted(successes, key=lambda e: e.event_time)[:1]


def _patient_access_after_anchor(
    patient_access_events: list[NormalizedEvent],
    anchor_time: datetime,
) -> list[NormalizedEvent]:
    if not anchor_time:
        return []
    window_end = anchor_time + timedelta(minutes=PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES)
    matched = [
        event for event in patient_access_events
        if event.event_time and anchor_time <= event.event_time <= window_end
    ]
    return sorted(matched, key=lambda e: e.event_time)


def _auth_case_pattern_and_events_for_cluster(
    auth_events: list[NormalizedEvent],
    failed_cluster: list[NormalizedEvent],
    patient_access_events: list[NormalizedEvent],
) -> tuple[Optional[str], list[NormalizedEvent]]:
    if not failed_cluster:
        return None, []

    if _cluster_meets_threshold(failed_cluster, FAILED_LOGIN_BURST_THRESHOLD, FAILED_LOGIN_BURST_WINDOW_MIN):
        base_pattern = "FAILED_LOGIN_BURST"
    elif _cluster_meets_threshold(failed_cluster, FAILED_LOGIN_REVIEW_THRESHOLD, FAILED_LOGIN_REVIEW_WINDOW_MIN):
        base_pattern = "FAILED_LOGIN_ACTIVITY"
    else:
        return None, []

    success_events = _success_events_after_cluster(auth_events, failed_cluster)
    if not success_events:
        return base_pattern, failed_cluster

    patient_access_events = _patient_access_after_anchor(
        patient_access_events,
        success_events[-1].event_time,
    )

    if patient_access_events:
        linked = sorted(
            {e.id: e for e in [*failed_cluster, *success_events, *patient_access_events]}.values(),
            key=lambda e: e.event_time or datetime.min,
        )
        return "PATIENT_ACCESS_AFTER_AUTH_FAILURES", linked
    linked = sorted(
        {e.id: e for e in [*failed_cluster, *success_events]}.values(),
        key=lambda e: e.event_time or datetime.min,
    )
    return "SUCCESSFUL_LOGIN_AFTER_FAILURES", linked


def _recommended_auth_action(severity: str, pattern_type: str) -> str:
    if severity in ("P0_CRITICAL", "P1_HIGH"):
        return "FOLLOW_UP"
    return "REVIEWED"


def _auth_severity(pattern_type: str) -> str:
    return AUTH_PATTERN_SEVERITY.get(pattern_type, "P2_MEDIUM")


def _successful_login_after_failures(
    auth_events: list[NormalizedEvent],
    failed_events: list[NormalizedEvent],
) -> list[NormalizedEvent]:
    successes = [
        e for e in auth_events
        if (e.event_type or "").lower() in ("login", "successful_login", "authentication_success")
    ]
    if not failed_events or not successes:
        return []
    window = timedelta(minutes=SUCCESS_AFTER_FAILURE_WINDOW_MINUTES)
    matched = []
    for success in successes:
        if not success.event_time:
            continue
        if any(
            failed.event_time
            and failed.event_time < success.event_time
            and success.event_time - failed.event_time <= window
            for failed in failed_events
        ):
            matched.append(success)
    return matched


def _patient_access_after_success(
    patient_access_events: list[NormalizedEvent],
    success_events: list[NormalizedEvent],
) -> list[NormalizedEvent]:
    if not patient_access_events or not success_events:
        return []
    window = timedelta(minutes=PATIENT_ACCESS_AFTER_LOGIN_WINDOW_MINUTES)
    matched = []
    for access in patient_access_events:
        if not access.event_time:
            continue
        if any(
            success.event_time
            and success.event_time <= access.event_time
            and access.event_time - success.event_time <= window
            for success in success_events
        ):
            matched.append(access)
    return matched


def generate_auth_cases(db: Session) -> int:
    """
    Generate cases for authentication-signal patterns (failed logins, credential events).
    Only creates cases when failed-login thresholds are met; single failures are ignored.
    """
    cutoff = datetime.utcnow() - timedelta(days=30)

    auth_events = (
        db.query(NormalizedEvent)
        .filter(
            NormalizedEvent.event_time >= cutoff,
            NormalizedEvent.event_type.in_(list(AUTHENTICATION_EVENT_TYPES)),
        )
        .all()
    )

    by_user: Dict[str, List[NormalizedEvent]] = defaultdict(list)
    for ev in auth_events:
        by_user[ev.user_id].append(ev)

    created = 0
    for user_id, evs in by_user.items():
        failed_events = [
            e for e in evs
            if (e.event_type or "").lower() in FAILED_LOGIN_EVENT_TYPES
        ]
        failed_clusters = _cluster_failed_logins(failed_events)
        if not failed_clusters:
            continue

        event_times = [e.event_time for e in evs if e.event_time]
        window_start = min(event_times) - timedelta(minutes=SUCCESS_AFTER_FAILURE_WINDOW_MINUTES)
        window_end   = max(event_times) + timedelta(minutes=PATIENT_ACCESS_AFTER_LOGIN_WINDOW_MINUTES)
        nearby_patient_access_events = (
            db.query(NormalizedEvent)
            .filter(
                NormalizedEvent.user_id == user_id,
                NormalizedEvent.event_time >= window_start,
                NormalizedEvent.event_time <= window_end,
            )
            .all()
        )
        nearby_patient_access_events = [
            e for e in nearby_patient_access_events
            if (e.event_type or "").lower() in PATIENT_ACCESS_EVENT_TYPES
        ]
        for failed_cluster in failed_clusters:
            pattern_type, linked_events = _auth_case_pattern_and_events_for_cluster(
                evs,
                failed_cluster,
                nearby_patient_access_events,
            )
            if not pattern_type:
                continue

            cluster_start = min(e.event_time for e in failed_cluster if e.event_time)
            case_id  = _stable_auth_case_id(user_id, cluster_start)
            existing = db.get(Case, case_id)

            if existing and existing.status in ("RESOLVED", "FALSE_POSITIVE"):
                continue

            score    = AUTH_PATTERN_SCORES[pattern_type]
            severity = _auth_severity(pattern_type)

            suppressed = _is_suppressed(user_id, pattern_type, db)
            status = "SUPPRESSED" if suppressed else (existing.status if existing else "OPEN")

            user_name = next((e.user_name for e in evs if e.user_name), user_id)
            dates     = [e.event_time for e in linked_events if e.event_time]
            title     = f"{AUTH_PATTERN_TITLES.get(pattern_type, 'Authentication Activity')} - {user_name}"
            case_type = AUTH_CASE_TYPES[pattern_type]

            if existing:
                existing.title            = title
                existing.event_count      = len(linked_events)
                existing.date_start       = min(dates)
                existing.date_end         = max(dates)
                existing.risk_score       = score
                existing.severity         = severity
                existing.breach_risk      = False
                existing.breach_deadline  = None
                existing.recommended_action = _recommended_auth_action(severity, pattern_type)
                existing.hipaa_provisions = AUTH_HIPAA.get(pattern_type, [])
                existing.case_type        = case_type
                existing.pattern_type     = pattern_type
                if existing.status not in ("SUPPRESSED", "RESOLVED", "FALSE_POSITIVE"):
                    existing.status = status
                existing.assessment_json  = build_case_assessment(existing, linked_events, db)
            else:
                case = Case(
                    case_id=case_id,
                    title=title,
                    severity=severity,
                    pattern_type=pattern_type,
                    case_type=case_type,
                    user_id=user_id,
                    user_name=user_name,
                    event_count=len(linked_events),
                    date_start=min(dates),
                    date_end=max(dates),
                    risk_score=score,
                    recommended_action=_recommended_auth_action(severity, pattern_type),
                    breach_risk=False,
                    breach_deadline=None,
                    status=status,
                    hipaa_provisions=AUTH_HIPAA.get(pattern_type, []),
                    created_at=datetime.utcnow(),
                )
                case.assessment_json = build_case_assessment(case, linked_events, db)
                db.add(case)
                created += 1

            _sync_case_event_links(case_id, linked_events, pattern_type, db, replace_existing=True)

    db.commit()
    return created
