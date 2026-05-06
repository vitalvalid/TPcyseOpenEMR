"""
Tests for authentication event classification and threshold-based auth case generation.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "engine"))

from engine.event_classification import (
    AUTHENTICATION_EVENT_TYPES,
    PATIENT_ACCESS_EVENT_TYPES,
    classify_event,
    is_auth_event,
    is_patient_access_event,
)
from engine.case_engine import (
    AUTH_CASE_TYPES,
    AUTH_PATTERN_SCORES,
    FAILED_LOGIN_CLUSTER_GAP_MINUTES,
    FAILED_LOGIN_REVIEW_THRESHOLD,
    FAILED_LOGIN_BURST_THRESHOLD,
    FAILED_LOGIN_REVIEW_WINDOW_MIN,
    FAILED_LOGIN_BURST_WINDOW_MIN,
    SUCCESS_AFTER_FAILURE_WINDOW_MINUTES,
    PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES,
    _classify_auth_pattern,
    _threshold_in_window,
    generate_auth_cases,
    generate_cases,
)
from api.cases import get_case, list_cases
from api.events import debug_auth_events
from db.models import Case, NormalizedEvent
from review_reasons import get_reason_for_action


# ── A. Event classification ────────────────────────────────────────────────────

def test_failed_login_is_auth_event():
    assert is_auth_event("failed_login") is True


def test_patient_access_is_not_auth_event():
    assert is_auth_event("patient_access") is False


def test_classify_failed_login_returns_authentication():
    assert classify_event("failed_login") == "authentication"


def test_classify_patient_access_returns_patient_access():
    assert classify_event("patient_access") == "patient_access"


def test_classify_admin_action_returns_system():
    assert classify_event("admin_action") == "system"


def test_classify_unknown_returns_other():
    assert classify_event("unknown_event_xyz") == "other"


def test_is_patient_access_event_blocks_auth_types():
    for et in AUTHENTICATION_EVENT_TYPES:
        assert is_patient_access_event(et) is False, f"{et} should not be patient_access"


def test_is_patient_access_event_with_patient_id_and_auth_type():
    # Auth event type always wins, even if patient_id is present
    assert is_patient_access_event("failed_login", patient_id="pt-123") is False


def test_all_auth_types_are_classified():
    for et in AUTHENTICATION_EVENT_TYPES:
        assert classify_event(et) == "authentication", f"{et} not classified as auth"


# ── B. Threshold window detection ─────────────────────────────────────────────

def _make_events(times):
    evs = []
    for t in times:
        e = MagicMock()
        e.event_time = t
        e.event_type = "failed_login"
        e.hour_of_day = t.hour
        e.user_name = None
        evs.append(e)
    return evs


def test_threshold_in_window_exact_threshold():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0)
    events = _make_events([base + timedelta(minutes=i) for i in range(3)])
    assert _threshold_in_window(events, 3, 15) is True


def test_threshold_in_window_below_threshold():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0)
    events = _make_events([base + timedelta(minutes=i*10) for i in range(2)])
    assert _threshold_in_window(events, 3, 15) is False


def test_threshold_in_window_spread_too_wide():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0)
    events = _make_events([base + timedelta(minutes=i*10) for i in range(3)])
    # 0, 10, 20 min — spread is 20 min which is > 15 min window
    assert _threshold_in_window(events, 3, 15) is False


def test_threshold_in_window_three_events_outside_five_minutes():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0)
    events = _make_events([base, base + timedelta(minutes=2), base + timedelta(minutes=6)])
    assert _threshold_in_window(events, 3, 5) is False


# ── C. Auth pattern classification ────────────────────────────────────────────

def _make_failed_events(n, base=None, spacing_minutes=2, hour=10):
    base = base or datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=hour)
    evs = []
    for i in range(n):
        e = MagicMock()
        e.event_time = base + timedelta(minutes=i * spacing_minutes)
        e.event_type = "failed_login"
        e.hour_of_day = hour
        e.user_name = None
        evs.append(e)
    return evs


def test_single_failure_produces_no_pattern():
    events = _make_failed_events(1)
    assert _classify_auth_pattern(events, []) is None


def test_two_failures_produce_no_pattern():
    events = _make_failed_events(2)
    assert _classify_auth_pattern(events, []) is None


def test_three_failures_in_window_produce_activity_pattern():
    events = _make_failed_events(3)
    result = _classify_auth_pattern(events, [])
    assert result == "FAILED_LOGIN_ACTIVITY"


def test_five_failures_in_burst_window_produce_burst_pattern():
    events = _make_failed_events(5, spacing_minutes=2)
    result = _classify_auth_pattern(events, [])
    assert result == "FAILED_LOGIN_BURST"


def test_patient_access_after_failures_wins_over_burst():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    events = _make_failed_events(3, base=base, spacing_minutes=2)
    success = MagicMock()
    success.event_time = base + timedelta(minutes=10)
    success.event_type = "successful_login"
    success.hour_of_day = 10
    success.user_name = None
    patient_access = MagicMock()
    patient_access.event_time = success.event_time + timedelta(minutes=14)
    patient_access.event_type = "patient_access"
    patient_access.hour_of_day = 10
    patient_access.user_name = None
    result = _classify_auth_pattern(events + [success], [patient_access])
    assert result == "PATIENT_ACCESS_AFTER_AUTH_FAILURES"


def test_successful_login_after_threshold_failures_detected():
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    failed = _make_failed_events(3, base=base)
    success = MagicMock()
    success.event_time = base + timedelta(minutes=9)
    success.event_type = "successful_login"
    success.hour_of_day = 10
    success.user_name = None
    result = _classify_auth_pattern(failed + [success], [])
    assert result == "SUCCESSFUL_LOGIN_AFTER_FAILURES"


def test_after_hours_failure_detected():
    # hour=2 AM — after hours
    events = _make_failed_events(3, hour=2)
    result = _classify_auth_pattern(events, [])
    assert result == "FAILED_LOGIN_ACTIVITY"


def _db_event(
    db,
    *,
    source_log_id: int,
    user_id: str,
    event_type: str,
    event_time: datetime,
    patient_id: str | None = None,
    user_name: str = "Dr Patel",
    user_role: str = "Physician",
    triggered_rules=None,
):
    row = NormalizedEvent(
        source_log_id=source_log_id,
        event_time=event_time,
        user_id=user_id,
        user_name=user_name,
        user_role=user_role,
        event_type=event_type,
        patient_id=patient_id,
        department="Clinic",
        ip_address="10.0.0.1",
        hour_of_day=event_time.hour,
        day_of_week=event_time.weekday(),
        risk_score=20.0,
        risk_level="LOW",
        triggered_rules=triggered_rules or [],
        status="PENDING",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_single_failed_login_does_not_create_patient_access_review_or_high_priority_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=2)
    _db_event(db, source_log_id=1, user_id="dr_patel", event_type="failed_login", event_time=base)
    assert generate_cases(db) == 0
    assert generate_auth_cases(db) == 0
    assert db.query(Case).count() == 0


def test_after_hours_patient_access_still_creates_after_hours_patient_access_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=22)
    _db_event(
        db,
        source_log_id=2,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base,
        patient_id="PAT-001",
        triggered_rules=[{
            "rule_id": "R-01",
            "rule_name": "After-Hours Access",
            "fired": True,
            "score_contribution": 20.0,
            "description": "Access outside business hours",
            "severity": "MEDIUM",
            "confidence": "HIGH",
        }],
    )
    assert generate_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "PATIENT_ACCESS_REVIEW"
    assert case.title == "After-Hours Patient Access - Dr Patel"


def test_three_failed_logins_create_authentication_review_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=10 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.title == "Failed Login Activity - Dr Patel"
    assert case.severity == "P2_MEDIUM"
    assert case.event_count == 3


def test_four_after_hours_failed_logins_create_failed_login_activity_title_not_after_hours_attempt(db, compliance_user):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=2)
    for idx in range(4):
        _db_event(
            db,
            source_log_id=14 + idx,
            user_id="admin_hayes",
            user_name="Susan Hayes",
            user_role="Administration",
            event_type="failed_login",
            event_time=base + timedelta(minutes=idx),
        )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.title == "Failed Login Activity - Susan Hayes"
    detail = get_case(case.case_id, db=db, _user=compliance_user)
    assert detail["quick_review_reason"] == "This account had repeated failed login attempts outside normal business hours."
    assert detail["pattern_type"] == "FAILED_LOGIN_ACTIVITY"
    assert detail["modifiers"] == [{"key": "after_hours", "label": "After-hours"}]
    assert detail["primary_suggested_action"]["action"] == "REQUEST_CONTEXT"
    assert detail["primary_suggested_action"]["action_label"] == "Request account confirmation"
    assert detail["primary_suggested_action"]["reason_code"] == "USER_CONFIRMED_ACTIVITY"


def test_three_failed_logins_spread_outside_five_minutes_do_not_create_one_review_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx, minute in enumerate((0, 2, 6)):
        _db_event(db, source_log_id=13 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=minute))
    assert generate_auth_cases(db) == 0
    assert db.query(Case).count() == 0


def test_five_failed_logins_create_failed_login_burst_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(5):
        _db_event(db, source_log_id=20 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.pattern_type == "FAILED_LOGIN_BURST"
    assert case.severity == "P1_HIGH"
    assert case.event_count == 5


def test_five_failed_logins_spread_outside_ten_minutes_do_not_create_burst(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx, minute in enumerate((0, 2, 4, 6, 11)):
        _db_event(db, source_log_id=25 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=minute))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.event_count == 4


def test_failed_logins_followed_by_success_create_account_access_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=30 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=34, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=SUCCESS_AFTER_FAILURE_WINDOW_MINUTES - 1))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_ACCESS_REVIEW"
    assert case.pattern_type == "SUCCESSFUL_LOGIN_AFTER_FAILURES"
    assert case.title == "Account Access Review - Dr Patel"
    assert case.event_count == 4
    assert case.assessment_json["quick_review_reason"] == "Failed login attempts were followed by a successful login."
    assert "billing" not in " ".join(case.assessment_json["quick_review_checks"]).lower()
    assert "treatment" not in " ".join(case.assessment_json["quick_review_checks"]).lower()


def test_failed_logins_success_and_patient_access_create_account_misuse_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=40 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=50, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=SUCCESS_AFTER_FAILURE_WINDOW_MINUTES - 1))
    _db_event(
        db,
        source_log_id=51,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base + timedelta(minutes=(SUCCESS_AFTER_FAILURE_WINDOW_MINUTES - 1) + PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES - 1),
        patient_id="PAT-200",
    )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_MISUSE_REVIEW"
    assert case.pattern_type == "PATIENT_ACCESS_AFTER_AUTH_FAILURES"
    assert case.title == "Account Misuse Review - Dr Patel"
    assert case.event_count == 5
    assert case.assessment_json["authentication_summary"]["patient_access_after_login"] is True
    assert case.assessment_json["authentication_summary"]["review_focus"] == "Failed logins followed by patient-record access"


def test_two_failed_logins_do_not_create_medium_or_high_auth_case(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(2):
        _db_event(db, source_log_id=35 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    assert generate_auth_cases(db) == 0
    assert db.query(Case).count() == 0


def test_successful_login_outside_ten_minutes_from_cluster_end_is_not_linked(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=310 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    _db_event(
        db,
        source_log_id=320,
        user_id="dr_patel",
        event_type="successful_login",
        event_time=base + timedelta(minutes=4 + SUCCESS_AFTER_FAILURE_WINDOW_MINUTES + 1),
    )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.event_count == 3


def test_patient_access_without_success_within_15_minutes_does_not_create_strong_account_misuse_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=330 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    _db_event(
        db,
        source_log_id=340,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base + timedelta(minutes=PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES - 1),
        patient_id="PAT-330",
    )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.event_count == 3


def test_patient_access_outside_15_minutes_is_not_linked(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=350 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))
    _db_event(
        db,
        source_log_id=360,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base + timedelta(minutes=PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES + 1),
        patient_id="PAT-350",
    )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.event_count == 3


def test_patient_access_case_can_show_related_authentication_signals(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=22)
    _db_event(db, source_log_id=50, user_id="dr_patel", event_type="failed_login", event_time=base - timedelta(minutes=20))
    _db_event(db, source_log_id=51, user_id="dr_patel", event_type="failed_login", event_time=base - timedelta(minutes=10))
    _db_event(
        db,
        source_log_id=52,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base,
        patient_id="PAT-300",
        triggered_rules=[{
            "rule_id": "R-01",
            "rule_name": "After-Hours Access",
            "fired": True,
            "score_contribution": 20.0,
            "description": "Access outside business hours",
            "severity": "MEDIUM",
            "confidence": "HIGH",
        }],
    )
    assert generate_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "PATIENT_ACCESS_REVIEW"
    assessment = case.assessment_json
    assert assessment["related_authentication_signals"][0]["event_type"] == "failed_login"
    assert assessment["related_authentication_signals"][0]["count"] == 2


def test_auth_case_returned_in_list_can_be_fetched_in_detail(db, compliance_user):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=60 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx * 2))

    assert generate_auth_cases(db) == 1
    payload = list_cases(status=None, status_group=None, severity=None, triage_bucket=None, db=db, _user=compliance_user)
    auth_row = next(r for r in payload["cases"] if r["case_type"] == "AUTHENTICATION_REVIEW")

    detail = get_case(auth_row["case_id"], db=db, _user=compliance_user)
    assert detail["case_id"] == auth_row["case_id"]
    assert detail["case_type"] == "AUTHENTICATION_REVIEW"


def test_auth_debug_endpoint_filters_dr_patel_without_cross_assigning_to_david_ross(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    _db_event(db, source_log_id=70, user_id="dr_patel", user_name="Priya Patel", event_type="failed_login", event_time=base)
    _db_event(db, source_log_id=71, user_id="dross", user_name="David Ross", event_type="failed_login", event_time=base + timedelta(minutes=1))

    payload = debug_auth_events(username="dr_patel", db=db)

    assert payload["count"] == 1
    assert payload["events"][0]["source_username"] == "dr_patel"
    assert payload["events"][0]["user_name"] == "Priya Patel"


def test_old_failed_login_cluster_is_not_merged_with_new_two_login_failures(db):
    old_base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=21) - timedelta(days=14)
    for idx in range(4):
        _db_event(
            db,
            source_log_id=200 + idx,
            user_id="nurse_chen",
            user_name="Linda Chen",
            user_role="Nursing",
            event_type="failed_login",
            event_time=old_base + timedelta(minutes=idx),
        )

    assert generate_auth_cases(db) == 1
    first_case = db.query(Case).filter(Case.user_id == "nurse_chen", Case.case_type == "AUTHENTICATION_REVIEW").one()
    assert first_case.event_count == 4

    new_base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=23)
    for idx in range(2):
        _db_event(
            db,
            source_log_id=210 + idx,
            user_id="nurse_chen",
            user_name="Linda Chen",
            user_role="Nursing",
            event_type="failed_login",
            event_time=new_base + timedelta(minutes=idx),
        )

    assert generate_auth_cases(db) == 0
    auth_cases = (
        db.query(Case)
        .filter(Case.user_id == "nurse_chen", Case.case_type == "AUTHENTICATION_REVIEW")
        .all()
    )
    assert len(auth_cases) == 1
    assert auth_cases[0].event_count == 4
    assert auth_cases[0].date_start.date() == old_base.date()


def test_failed_logins_separated_by_cluster_gap_create_separate_clusters(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    first_cluster = [0, 2, 4]
    second_cluster = [FAILED_LOGIN_CLUSTER_GAP_MINUTES + 10, FAILED_LOGIN_CLUSTER_GAP_MINUTES + 12, FAILED_LOGIN_CLUSTER_GAP_MINUTES + 14]
    for idx, minute in enumerate(first_cluster + second_cluster):
        _db_event(
            db,
            source_log_id=400 + idx,
            user_id="dr_patel",
            event_type="failed_login",
            event_time=base + timedelta(minutes=minute),
        )

    assert generate_auth_cases(db) == 2
    cases = db.query(Case).order_by(Case.date_start.asc()).all()
    assert len(cases) == 2
    assert [case.event_count for case in cases] == [3, 3]
    assert cases[0].date_end < cases[1].date_start


def test_successful_login_at_nine_minutes_links(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=500 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=510, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=9))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_ACCESS_REVIEW"
    assert case.event_count == 4


def test_successful_login_at_eleven_minutes_does_not_link(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=520 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=530, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=13))
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.event_count == 3


def test_patient_access_at_fourteen_minutes_after_correlated_success_links(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=540 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=550, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=9))
    _db_event(db, source_log_id=551, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=23), patient_id="PAT-540")
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_MISUSE_REVIEW"
    assert case.event_count == 5


def test_patient_access_at_sixteen_minutes_after_correlated_success_does_not_link(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=560 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=570, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=9))
    _db_event(db, source_log_id=571, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=25), patient_id="PAT-560")
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_ACCESS_REVIEW"
    assert case.event_count == 4


def test_one_failed_login_plus_patient_access_does_not_create_account_misuse_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    _db_event(db, source_log_id=580, user_id="dr_patel", event_type="failed_login", event_time=base)
    _db_event(db, source_log_id=581, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=10), patient_id="PAT-580")
    assert generate_auth_cases(db) == 0


def test_two_failed_logins_plus_patient_access_does_not_create_account_misuse_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    _db_event(db, source_log_id=590, user_id="dr_patel", event_type="failed_login", event_time=base)
    _db_event(db, source_log_id=591, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=2))
    _db_event(db, source_log_id=592, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=10), patient_id="PAT-590")
    assert generate_auth_cases(db) == 0


def test_user_confirmed_activity_remains_available_as_resolution_reason(db):
    reason = get_reason_for_action(db, "RESOLVED_NO_ISSUE", "USER_CONFIRMED_ACTIVITY")
    assert reason is not None
    assert reason.label == "User confirmed their own login activity"


def test_three_failed_logins_without_success_plus_patient_access_stays_authentication_review(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=600 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(
        db,
        source_log_id=610,
        user_id="dr_patel",
        event_type="patient_access",
        event_time=base + timedelta(minutes=14),
        patient_id="PAT-600",
    )
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.event_count == 3


def test_three_failed_logins_success_outside_ten_minutes_and_patient_access_do_not_create_misuse(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=620 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=630, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=13))
    _db_event(db, source_log_id=631, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=20), patient_id="PAT-620")
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "AUTHENTICATION_REVIEW"
    assert case.pattern_type == "FAILED_LOGIN_ACTIVITY"
    assert case.event_count == 3


def test_three_failed_logins_success_within_ten_minutes_patient_access_outside_fifteen_does_not_create_misuse(db):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=10)
    for idx in range(3):
        _db_event(db, source_log_id=640 + idx, user_id="dr_patel", event_type="failed_login", event_time=base + timedelta(minutes=idx))
    _db_event(db, source_log_id=650, user_id="dr_patel", event_type="successful_login", event_time=base + timedelta(minutes=9))
    _db_event(db, source_log_id=651, user_id="dr_patel", event_type="patient_access", event_time=base + timedelta(minutes=26), patient_id="PAT-640")
    assert generate_auth_cases(db) == 1
    case = db.query(Case).one()
    assert case.case_type == "ACCOUNT_ACCESS_REVIEW"
    assert case.pattern_type == "SUCCESSFUL_LOGIN_AFTER_FAILURES"
    assert case.event_count == 4


def test_case_window_reflects_only_linked_cluster_events(db):
    old_base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=21) - timedelta(days=14)
    for idx in range(4):
        _db_event(
            db,
            source_log_id=660 + idx,
            user_id="nurse_chen",
            user_name="Linda Chen",
            user_role="Nursing",
            event_type="failed_login",
            event_time=old_base + timedelta(minutes=idx),
        )
    new_base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=23)
    for idx in range(3):
        _db_event(
            db,
            source_log_id=670 + idx,
            user_id="nurse_chen",
            user_name="Linda Chen",
            user_role="Nursing",
            event_type="failed_login",
            event_time=new_base + timedelta(minutes=idx),
        )

    assert generate_auth_cases(db) == 2
    latest_case = (
        db.query(Case)
        .filter(Case.user_id == "nurse_chen")
        .order_by(Case.date_start.desc())
        .first()
    )
    assert latest_case.date_start == new_base
    assert latest_case.date_end == new_base + timedelta(minutes=2)
    assert latest_case.event_count == 3


# ── D. Auth pattern metadata ───────────────────────────────────────────────────

def test_all_auth_patterns_have_case_types():
    for pattern in AUTH_PATTERN_SCORES:
        assert pattern in AUTH_CASE_TYPES, f"{pattern} missing from AUTH_CASE_TYPES"


def test_auth_case_types_are_correct():
    assert AUTH_CASE_TYPES["FAILED_LOGIN_ACTIVITY"] == "AUTHENTICATION_REVIEW"
    assert AUTH_CASE_TYPES["FAILED_LOGIN_BURST"] == "AUTHENTICATION_REVIEW"
    assert AUTH_CASE_TYPES["AFTER_HOURS_LOGIN_ATTEMPT"] == "AUTHENTICATION_REVIEW"
    assert AUTH_CASE_TYPES["SUCCESSFUL_LOGIN_AFTER_FAILURES"] == "ACCOUNT_ACCESS_REVIEW"
    assert AUTH_CASE_TYPES["PATIENT_ACCESS_AFTER_AUTH_FAILURES"] == "ACCOUNT_MISUSE_REVIEW"


def test_auth_pattern_scores_ordered():
    # Lower-severity patterns score less than high-severity ones
    assert AUTH_PATTERN_SCORES["FAILED_LOGIN_ACTIVITY"] < AUTH_PATTERN_SCORES["FAILED_LOGIN_BURST"]
    assert AUTH_PATTERN_SCORES["FAILED_LOGIN_BURST"] < AUTH_PATTERN_SCORES["SUCCESSFUL_LOGIN_AFTER_FAILURES"]
    assert AUTH_PATTERN_SCORES["SUCCESSFUL_LOGIN_AFTER_FAILURES"] < AUTH_PATTERN_SCORES["PATIENT_ACCESS_AFTER_AUTH_FAILURES"]
