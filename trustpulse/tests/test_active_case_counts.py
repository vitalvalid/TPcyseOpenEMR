from datetime import datetime, timedelta

from api.cases import get_case, list_cases
from api.dashboard import dashboard
from api.users import list_users, user_timeline
from db.models import Case, NormalizedEvent
from governance.report_generator import generate_periodic_report


def _make_case(db, case_id: str, status: str, severity: str = "P1_HIGH", user_id: str = "dr_active"):
    case = Case(
        case_id=case_id,
        title=f"Case {case_id}",
        severity=severity,
        pattern_type="OFF_HOURS",
        user_id=user_id,
        user_name="Dr Active",
        event_count=1,
        date_start=datetime.utcnow() - timedelta(days=1),
        date_end=datetime.utcnow() - timedelta(days=1),
        risk_score=55.0,
        recommended_action="FOLLOW_UP",
        breach_risk=False,
        status=status,
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(case)
    db.commit()
    return case


def _make_event(db, user_id: str = "dr_active", risk_level: str = "HIGH"):
    event = NormalizedEvent(
        source_log_id=int(datetime.utcnow().timestamp() * 1000) % 1000000000,
        event_time=datetime.utcnow() - timedelta(hours=1),
        user_id=user_id,
        user_name="Dr Active",
        user_role="PHYSICIAN",
        event_type="patient_access",
        patient_id="PAT-001",
        department="Cardiology",
        ip_address="10.0.0.1",
        hour_of_day=10,
        day_of_week=1,
        risk_score=55.0,
        risk_level=risk_level,
        triggered_rules=[],
        status="PENDING",
    )
    db.add(event)
    db.commit()
    return event


def test_dashboard_uses_active_lifecycle_statuses(db, compliance_user):
    _make_case(db, "case-open", "OPEN", severity="P0_CRITICAL")
    _make_case(db, "case-review", "UNDER_REVIEW", severity="P1_HIGH")
    _make_case(db, "case-p2", "REOPENED", severity="P2_MEDIUM")
    _make_case(db, "case-resolved", "RESOLVED", severity="P1_HIGH")

    payload = dashboard(db=db, _current=compliance_user)

    assert len(payload["today_cases"]) == 2
    assert {c["status"] for c in payload["today_cases"]} == {"OPEN", "UNDER_REVIEW"}
    assert payload["remaining_p2"] == 1
    assert payload["all_clear"] is False


def test_user_summary_counts_active_cases(monkeypatch, db):
    monkeypatch.setattr("api.users._openemr_roster", lambda: {})
    _make_event(db, user_id="dr_active")
    _make_case(db, "user-case-1", "NEEDS_CONTEXT", user_id="dr_active")
    _make_case(db, "user-case-2", "ESCALATED", user_id="dr_active")
    _make_case(db, "user-case-3", "FALSE_POSITIVE", user_id="dr_active")

    rows = list_users(db=db)
    user_row = next(r for r in rows if r["user_id"] == "dr_active")

    assert user_row["open_cases"] == 2
    assert user_row["active_cases"] == 2


def test_user_timeline_includes_all_active_case_statuses(monkeypatch, db):
    monkeypatch.setattr("api.users._openemr_roster", lambda: {})
    _make_event(db, user_id="dr_active")
    _make_case(db, "timeline-open", "OPEN", user_id="dr_active")
    _make_case(db, "timeline-reopened", "REOPENED", user_id="dr_active")
    _make_case(db, "timeline-suppressed", "SUPPRESSED", user_id="dr_active")

    payload = user_timeline("dr_active", db=db)

    assert {c["case_id"] for c in payload["open_cases"]} == {"timeline-open", "timeline-reopened"}


def test_periodic_report_uses_active_case_counts(db):
    _make_case(db, "report-open", "OPEN")
    _make_case(db, "report-context", "NEEDS_CONTEXT")
    _make_case(db, "report-escalated", "ESCALATED")
    _make_case(db, "report-fp", "FALSE_POSITIVE")

    html = generate_periodic_report(db, period_days=7, generated_by="pytest")

    assert "Active Cases" in html
    assert "Open Cases" not in html
    assert ">3</div>" in html


def test_list_cases_supports_status_group_active_and_closed(db, compliance_user):
    _make_case(db, "list-open", "OPEN")
    _make_case(db, "list-review", "UNDER_REVIEW")
    _make_case(db, "list-closed", "RESOLVED")
    _make_case(db, "list-fp", "FALSE_POSITIVE")

    active_payload = list_cases(status=None, status_group="active", severity=None, triage_bucket=None, db=db, _user=compliance_user)
    closed_payload = list_cases(status=None, status_group="closed", severity=None, triage_bucket=None, db=db, _user=compliance_user)
    all_payload = list_cases(status=None, status_group=None, severity=None, triage_bucket=None, db=db, _user=compliance_user)

    assert {c["status"] for c in active_payload["cases"]} == {"OPEN", "UNDER_REVIEW"}
    assert {c["status"] for c in closed_payload["cases"]} == {"RESOLVED", "FALSE_POSITIVE"}
    assert {c["status"] for c in all_payload["cases"]} == {"OPEN", "UNDER_REVIEW", "RESOLVED", "FALSE_POSITIVE"}


def test_list_cases_returns_newest_cases_first_and_detail_fetches_by_canonical_case_id(db, compliance_user):
    older = _make_case(db, "older-case", "OPEN")
    older.created_at = datetime.utcnow() - timedelta(days=3)
    older.date_end = datetime.utcnow() - timedelta(days=2)
    newer = _make_case(db, "newer-case", "OPEN")
    newer.created_at = datetime.utcnow() - timedelta(hours=2)
    newer.date_end = datetime.utcnow() - timedelta(hours=1)
    db.commit()

    payload = list_cases(status=None, status_group=None, severity=None, triage_bucket=None, db=db, _user=compliance_user)

    assert [row["case_id"] for row in payload["cases"][:2]] == ["newer-case", "older-case"]
    for row in payload["cases"]:
        detail = get_case(row["case_id"], db=db, _user=compliance_user)
        assert detail["case_id"] == row["case_id"]
