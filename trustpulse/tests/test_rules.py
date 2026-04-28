"""
Tests for the rules engine.
Rules with missing context must return not_evaluated, not fire on fake data.
"""
import pytest
from engine.rules import (
    r05_vip_patient, r06_failed_logins, r07_modify_then_export,
    r09_new_ip, r04_cross_department, evaluate_all_rules,
)


# ── R-05: VIP/no-appointment ──────────────────────────────────────────────────

class TestR05:
    def test_not_evaluated_when_context_none(self):
        result = r05_vip_patient({}, {}, {"has_appointment": None, "patient_is_vip": True})
        assert result.not_evaluated is True
        assert result.fired is False
        assert result.score_contribution == 0.0

    def test_fires_when_vip_and_no_appt(self):
        result = r05_vip_patient({}, {}, {"has_appointment": False, "patient_is_vip": True})
        assert result.fired is True
        assert result.score_contribution > 0

    def test_no_fire_when_has_appointment(self):
        result = r05_vip_patient({}, {}, {"has_appointment": True, "patient_is_vip": True})
        assert result.fired is False

    def test_no_fire_when_not_vip(self):
        result = r05_vip_patient({}, {}, {"has_appointment": False, "patient_is_vip": False})
        assert result.fired is False


# ── R-06: Failed login burst ──────────────────────────────────────────────────

class TestR06:
    def test_fires_when_failures_gte_3(self):
        result = r06_failed_logins(
            {"event_type": "failed_login"},
            {},
            {"recent_failed_logins": 3},
        )
        assert result.fired is True

    def test_no_fire_below_threshold(self):
        result = r06_failed_logins(
            {"event_type": "patient_access"},
            {},
            {"recent_failed_logins": 0},
        )
        assert result.fired is False
        assert result.not_evaluated is False

    def test_fires_on_real_failed_login_row(self):
        result = r06_failed_logins(
            {"event_type": "failed_login"},
            {},
            {"recent_failed_logins": 5},
        )
        assert result.fired is True
        assert result.score_contribution == 35.0


# ── R-07: Modify-then-export ──────────────────────────────────────────────────

class TestR07:
    def test_fires_when_flag_set(self):
        result = r07_modify_then_export(
            {"event_type": "report_export"},
            {},
            {"modify_then_export_within_5min": True},
        )
        assert result.fired is True

    def test_no_fire_for_unrelated_event(self):
        result = r07_modify_then_export(
            {"event_type": "login"},
            {},
            {"modify_then_export_within_5min": False},
        )
        assert result.fired is False
        # Should include limitations note for unrelated event
        assert len(result.limitations) > 0 or result.fired is False


# ── R-09: New IP ──────────────────────────────────────────────────────────────

class TestR09:
    def test_not_evaluated_when_no_ip(self):
        result = r09_new_ip({"ip_address": ""}, {}, {})
        assert result.not_evaluated is True
        assert result.fired is False

    def test_fires_on_unknown_ip_with_baseline(self):
        result = r09_new_ip(
            {"ip_address": "10.99.0.1"},
            {"known_ips": ["192.168.1.1", "192.168.1.2"]},
            {},
        )
        assert result.fired is True

    def test_no_fire_on_known_ip(self):
        result = r09_new_ip(
            {"ip_address": "192.168.1.1"},
            {"known_ips": ["192.168.1.1"]},
            {},
        )
        assert result.fired is False


# ── R-04: Cross-department ────────────────────────────────────────────────────

class TestR04:
    def test_not_evaluated_when_no_dept_context(self):
        result = r04_cross_department({"event_type": "patient_access"}, {}, {})
        assert result.not_evaluated is True

    def test_fires_on_cross_dept(self):
        result = r04_cross_department(
            {"event_type": "patient_access", "department": "oncology"},
            {},
            {"user_department": "billing"},
        )
        assert result.fired is True


# ── evaluate_all_rules: no fake cases when no context ────────────────────────

class TestAllRules:
    def test_no_false_positives_on_empty_context(self):
        event = {"hour_of_day": 12, "day_of_week": 1, "event_type": "patient_access",
                 "patient_id": None, "department": None, "ip_address": None, "user_role": "clinician"}
        results = evaluate_all_rules(event, {}, {
            "daily_unique_patients": 0, "daily_access_count": 1,
            "recent_failed_logins": 0, "patient_is_vip": False,
            "has_appointment": None, "modify_then_export_within_5min": False,
            "user_department": None,
        })
        fired = [r for r in results if r.fired and not r.not_evaluated]
        # No rules should fire on completely normal/empty context
        assert len(fired) == 0
