"""
Tests for the real OpenEMR connector.
"""
from datetime import datetime

import pytest
from ingestion.connectors.openemr_real import _assert_select_only, _normalize_raw_rows


class TestSQLAllowlist:
    def test_select_allowed(self):
        _assert_select_only("SELECT id FROM log LIMIT 10")

    def test_select_with_whitespace(self):
        _assert_select_only("  SELECT * FROM log WHERE id > 0")

    def test_insert_rejected(self):
        with pytest.raises(PermissionError, match="allowlist"):
            _assert_select_only("INSERT INTO log VALUES (1, 'now', 'admin', 'login')")

    def test_update_rejected(self):
        with pytest.raises(PermissionError):
            _assert_select_only("UPDATE log SET event='hacked' WHERE id=1")

    def test_delete_rejected(self):
        with pytest.raises(PermissionError):
            _assert_select_only("DELETE FROM log")

    def test_drop_rejected(self):
        with pytest.raises(PermissionError):
            _assert_select_only("DROP TABLE log")

    def test_select_case_insensitive(self):
        _assert_select_only("select id from log limit 1")
        _assert_select_only("SELECT id FROM api_log")

    def test_subquery_allowed(self):
        _assert_select_only(
            "SELECT COUNT(*) FROM openemr_postcalendar_events "
            "WHERE pc_aid = (SELECT id FROM users WHERE username='test' LIMIT 1)"
        )


def test_failed_login_row_normalizes_to_auth_event_for_dr_patel(monkeypatch):
    monkeypatch.setattr(
        "ingestion.connectors.openemr_real._get_user_info",
        lambda engine, username: {
            "user_name": "Priya Patel",
            "user_role": "clinician",
            "department": "Internal Medicine",
        },
    )
    rows = [{
        "id": 101,
        "date": datetime(2026, 1, 1, 9, 0, 0),
        "user": "dr_patel",
        "event": "login-failure",
        "log_patient_id": None,
        "method": "POST",
        "request": "/portal/login",
        "ip_address": "10.0.0.8",
    }]
    normalized, errors, meta = _normalize_raw_rows(None, rows)

    assert errors == []
    assert meta["rows_excluded_by_policy"] == 0
    assert normalized[0]["event_type"] == "failed_login"
    assert normalized[0]["user_id"] == "dr_patel"
    assert normalized[0]["user_name"] == "Priya Patel"
    assert normalized[0]["user_name"] != "David Ross"


def test_login_event_with_success_zero_normalizes_to_failed_login(monkeypatch):
    monkeypatch.setattr(
        "ingestion.connectors.openemr_real._get_user_info",
        lambda engine, username: {
            "user_name": "Priya Patel",
            "user_role": "clinician",
            "department": "Internal Medicine",
        },
    )
    rows = [{
        "id": 171,
        "date": datetime(2026, 5, 1, 23, 43, 43),
        "user": "dr_patel",
        "event": "login",
        "success": 0,
        "comments": "failure",
        "log_patient_id": None,
        "method": None,
        "request": None,
        "ip_address": None,
    }]
    normalized, errors, meta = _normalize_raw_rows(None, rows)

    assert errors == []
    assert meta["rows_excluded_by_policy"] == 0
    assert normalized[0]["event_type"] == "failed_login"
    assert normalized[0]["user_id"] == "dr_patel"
