"""
Tests for the real OpenEMR connector.
"""
import pytest
from ingestion.connectors.openemr_real import _assert_select_only


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
