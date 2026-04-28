"""
Tests for CaseAction append-only hash chain.
"""
import hashlib
import json
from datetime import datetime

import pytest
from api.cases import _compute_action_hash, _get_last_action_hash, _record_action
from db.models import Case, CaseAction, TrustPulseUser
from api.auth import hash_password


def _make_case(db) -> Case:
    c = Case(
        case_id      = "test-case-0001",
        title        = "Test Case",
        severity     = "P1_HIGH",
        pattern_type = "OFF_HOURS",
        user_id      = "dr_test",
        user_name    = "Dr Test",
        event_count  = 3,
        date_start   = datetime(2026, 1, 1),
        date_end     = datetime(2026, 1, 2),
        risk_score   = 45.0,
        recommended_action = "FOLLOW_UP",
        status       = "OPEN",
    )
    db.add(c)
    db.commit()
    return c


def _make_actor(db) -> TrustPulseUser:
    u = TrustPulseUser(
        email="co@test.local",
        hashed_password=hash_password("pass"),
        display_name="CO",
        role="COMPLIANCE_OFFICER",
        is_active=True,
    )
    db.add(u)
    db.commit()
    return u


class TestCaseActionHashChain:
    def test_first_action_uses_zero_hash(self, db):
        c = _make_case(db)
        prev = _get_last_action_hash(c.case_id, db)
        assert prev == "0" * 64

    def test_hash_is_deterministic(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEWED", "previous_status": "OPEN", "new_status": "REVIEWED",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        h2 = _compute_action_hash(fields, "0" * 64)
        assert h1 == h2

    def test_hash_changes_with_different_action(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEWED", "previous_status": "OPEN", "new_status": "REVIEWED",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        fields["action"] = "DISMISSED"
        h2 = _compute_action_hash(fields, "0" * 64)
        assert h1 != h2

    def test_hash_changes_with_different_previous(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEWED", "previous_status": "OPEN", "new_status": "REVIEWED",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        h2 = _compute_action_hash(fields, "f" * 64)
        assert h1 != h2

    def test_chain_links_correctly(self, db):
        c     = _make_case(db)
        actor = _make_actor(db)
        _record_action(db, c, actor, "REVIEWED", "REVIEWED", notes="First review")
        db.commit()
        first  = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).first()
        assert first.previous_hash == "0" * 64
        assert first.record_hash is not None
        assert len(first.record_hash) == 64

        _record_action(db, c, actor, "ESCALATED", "ESCALATED", notes="Escalate")
        db.commit()
        actions = (db.query(CaseAction)
                   .filter(CaseAction.case_id == c.case_id)
                   .order_by(CaseAction.created_at)
                   .all())
        assert len(actions) == 2
        assert actions[1].previous_hash == actions[0].record_hash
