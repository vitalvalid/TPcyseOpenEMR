"""
Tests for CaseAction append-only hash chain.
"""
import hashlib
import json
from datetime import datetime

import pytest
from starlette.requests import Request

from api.cases import (
    ContextResponseRequest,
    DispositionRequest,
    _compute_action_hash,
    _get_last_action_hash,
    _record_action,
    case_disposition,
    get_case,
    reason_codes,
    respond_to_context_request,
)
from db.models import Case, CaseAction, ContextRequest, TrustPulseUser
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


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/cases/test/disposition",
            "headers": [(b"user-agent", b"pytest-agent")],
            "client": ("127.0.0.1", 12345),
        }
    )


class TestCaseActionHashChain:
    def test_first_action_uses_zero_hash(self, db):
        c = _make_case(db)
        prev = _get_last_action_hash(c.case_id, db)
        assert prev == "0" * 64

    def test_hash_is_deterministic(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEW_STARTED", "previous_status": "OPEN", "new_status": "UNDER_REVIEW",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        h2 = _compute_action_hash(fields, "0" * 64)
        assert h1 == h2

    def test_hash_changes_with_different_action(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEW_STARTED", "previous_status": "OPEN", "new_status": "UNDER_REVIEW",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        fields["action"] = "ESCALATED"
        h2 = _compute_action_hash(fields, "0" * 64)
        assert h1 != h2

    def test_hash_changes_with_different_previous(self):
        fields = {
            "case_id": "abc", "actor_email": "x@y", "actor_role": "CO",
            "action": "REVIEW_STARTED", "previous_status": "OPEN", "new_status": "UNDER_REVIEW",
            "reason_code": "", "notes": "", "created_at": "2026-01-01T00:00:00",
        }
        h1 = _compute_action_hash(fields, "0" * 64)
        h2 = _compute_action_hash(fields, "f" * 64)
        assert h1 != h2

    def test_chain_links_correctly(self, db):
        c     = _make_case(db)
        actor = _make_actor(db)
        _record_action(db, c, actor, "REVIEW_STARTED", "UNDER_REVIEW", previous_status="OPEN", notes="First review")
        db.commit()
        first  = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).first()
        assert first.previous_hash == "0" * 64
        assert first.record_hash is not None
        assert len(first.record_hash) == 64

        _record_action(db, c, actor, "ESCALATED", "ESCALATED", previous_status="UNDER_REVIEW", notes="Escalate")
        db.commit()
        actions = (db.query(CaseAction)
                   .filter(CaseAction.case_id == c.case_id)
                   .order_by(CaseAction.created_at)
                   .all())
        assert len(actions) == 2
        assert actions[1].previous_hash == actions[0].record_hash


class TestCaseLifecycleActions:
    def test_open_to_escalated_records_transition(self, db):
        c = _make_case(db)
        actor = _make_actor(db)

        result = case_disposition(
            c.case_id,
            DispositionRequest(action="ESCALATED", reason_code="POSSIBLE_INAPPROPRIATE_ACCESS", notes="Escalating for officer review", escalated_to_role="Privacy Officer", escalation_reason="Needs privacy review"),
            http_req=_request(),
            db=db,
            current=actor,
        )

        db.refresh(c)
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert result["new_status"] == "ESCALATED"
        assert c.status == "ESCALATED"
        assert action.previous_status == "OPEN"
        assert action.new_status == "ESCALATED"
        assert action.source_ip == "127.0.0.1"
        assert action.user_agent == "pytest-agent"

    def test_escalated_does_not_set_resolved_at(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="ESCALATED", reason_code="POSSIBLE_INAPPROPRIATE_ACCESS", notes="Escalating for officer review", escalated_to_role="Privacy Officer", escalation_reason="Needs privacy review"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        assert c.resolved_at is None

    def test_request_context_does_not_set_resolved_at(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="REQUEST_CONTEXT", reason_code="NEED_SUPERVISOR_REVIEW", notes="Need manager clarification", requested_from_role="Clinic Administrator", context_question="Please confirm why this access was needed."),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        assert c.status == "NEEDS_CONTEXT"
        assert c.resolved_at is None

    def test_resolved_authorized_access_sets_resolved(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="RESOLVED_AUTHORIZED_ACCESS", reason_code="TREATMENT_RELATIONSHIP_CONFIRMED", notes="Access confirmed as authorized"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        assert c.status == "RESOLVED"
        assert c.resolved_at is not None

    def test_false_positive_requires_notes_and_sets_resolved(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        with pytest.raises(Exception) as exc:
            case_disposition(
                c.case_id,
                DispositionRequest(action="FALSE_POSITIVE", reason_code="KNOWN_WORKFLOW_PATTERN", notes=""),
                http_req=_request(),
                db=db,
                current=actor,
            )
        assert "Notes required" in str(exc.value)

        case_disposition(
            c.case_id,
            DispositionRequest(action="FALSE_POSITIVE", reason_code="KNOWN_WORKFLOW_PATTERN", notes="Log correlation shows no issue"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        assert c.status == "FALSE_POSITIVE"
        assert c.resolved_at is not None

    def test_comment_added_does_not_change_status(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="COMMENT_ADDED", notes="Adding analyst note"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert c.status == "OPEN"
        assert action.previous_status == "OPEN"
        assert action.new_status == "OPEN"

    def test_evidence_exported_does_not_change_status(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="EVIDENCE_EXPORTED", notes=""),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert c.status == "OPEN"
        assert action.action == "EVIDENCE_EXPORTED"
        assert action.previous_status == "OPEN"
        assert action.new_status == "OPEN"

    def test_case_detail_returns_status_transition_history(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="ESCALATED", reason_code="POSSIBLE_BREACH_RISK", notes="Escalating for review", escalated_to_role="Privacy Officer", escalation_reason="Needs privacy review"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        detail = get_case(c.case_id, db=db, _user=actor)
        assert detail["action_history"][0]["action"] == "ESCALATED"
        assert detail["action_history"][0]["previous_status"] == "OPEN"
        assert detail["action_history"][0]["new_status"] == "ESCALATED"
        assert detail["action_history"][0]["reason_label_snapshot"] == "Possible breach-risk condition for privacy review"

    def test_case_detail_returns_updated_history_after_each_action(self, db):
        c = _make_case(db)
        actor = _make_actor(db)

        case_disposition(
            c.case_id,
            DispositionRequest(action="REVIEW_STARTED", notes=""),
            http_req=_request(),
            db=db,
            current=actor,
        )
        first = get_case(c.case_id, db=db, _user=actor)
        assert first["status"] == "UNDER_REVIEW"
        assert len(first["action_history"]) == 1
        assert first["action_history"][0]["action"] == "REVIEW_STARTED"

        case_disposition(
            c.case_id,
            DispositionRequest(action="REQUEST_CONTEXT", reason_code="NEED_PROVIDER_CONFIRMATION", notes="Need department confirmation", requested_from_role="Provider / Physician", context_question="Please confirm the business reason for this access."),
            http_req=_request(),
            db=db,
            current=actor,
        )
        second = get_case(c.case_id, db=db, _user=actor)
        assert second["status"] == "NEEDS_CONTEXT"
        assert len(second["action_history"]) == 2
        assert second["action_history"][1]["action"] == "REQUEST_CONTEXT"
        assert second["action_history"][1]["previous_status"] == "UNDER_REVIEW"
        assert second["action_history"][1]["new_status"] == "NEEDS_CONTEXT"

    def test_valid_action_and_reason_succeeds(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        result = case_disposition(
            c.case_id,
            DispositionRequest(action="REQUEST_CONTEXT", reason_code="NEED_APPOINTMENT_CONTEXT", notes="Need scheduler input", requested_from_role="Billing Supervisor", context_question="Was this access related to billing/payment operations for these patient accounts?", due_date="2026-01-03T12:00:00"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        context_request = db.query(ContextRequest).filter(ContextRequest.case_id == c.case_id).one()
        assert result["new_status"] == "NEEDS_CONTEXT"
        assert action.reason_code == "NEED_APPOINTMENT_CONTEXT"
        assert action.reason_label_snapshot == "Need appointment or scheduling context"
        assert context_request.requested_from_role == "Billing Supervisor"
        assert "billing/payment operations" in context_request.question

    def test_invalid_reason_for_action_returns_400(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        with pytest.raises(Exception) as exc:
            case_disposition(
                c.case_id,
                DispositionRequest(action="ESCALATED", reason_code="LOG_MAPPING_ERROR", notes="Investigate"),
                http_req=_request(),
                db=db,
                current=actor,
            )
        assert "Invalid reason_code" in str(exc.value)

    def test_missing_reason_for_sensitive_action_returns_400(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        with pytest.raises(Exception) as exc:
            case_disposition(
                c.case_id,
                DispositionRequest(action="ESCALATED", notes="Investigate"),
                http_req=_request(),
                db=db,
                current=actor,
            )
        assert "Reason code required" in str(exc.value)

    def test_missing_note_for_sensitive_action_returns_400(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        with pytest.raises(Exception) as exc:
            case_disposition(
                c.case_id,
                DispositionRequest(action="REQUEST_CONTEXT", reason_code="INSUFFICIENT_LOG_CONTEXT", notes="", requested_from_role="Clinic Administrator", context_question="Need context"),
                http_req=_request(),
                db=db,
                current=actor,
            )
        assert "Notes required" in str(exc.value)

    def test_request_context_creates_context_request_and_case_detail_returns_it(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(
                action="REQUEST_CONTEXT",
                reason_code="NEED_BILLING_JUSTIFICATION",
                notes="Need billing confirmation",
                requested_from_role="Billing Supervisor",
                requested_from_email="billing.supervisor@test.local",
                context_question="Was this access related to billing/payment operations for these patient accounts?",
                due_date="2026-01-04T09:00:00",
            ),
            http_req=_request(),
            db=db,
            current=actor,
        )
        detail = get_case(c.case_id, db=db, _user=actor)
        assert detail["status"] == "NEEDS_CONTEXT"
        assert len(detail["context_requests"]) == 1
        assert detail["context_requests"][0]["requested_from_role"] == "Billing Supervisor"
        assert detail["context_requests"][0]["status"] == "PENDING"

    def test_context_response_updates_request_and_adds_action(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(
                action="REQUEST_CONTEXT",
                reason_code="NEED_PROVIDER_CONFIRMATION",
                notes="Need provider confirmation",
                requested_from_role="Provider / Physician",
                context_question="Please confirm treatment context for this access.",
            ),
            http_req=_request(),
            db=db,
            current=actor,
        )
        context_request = db.query(ContextRequest).filter(ContextRequest.case_id == c.case_id).one()
        respond_to_context_request(
            c.case_id,
            context_request.id,
            ContextResponseRequest(response_text="This access was part of scheduled follow-up.", notes="Response received"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(context_request)
        assert context_request.status == "RESPONDED"
        assert "scheduled follow-up" in context_request.response_text
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert action.action == "CONTEXT_RESPONSE_ADDED"
        assert action.new_status == "NEEDS_CONTEXT"

    def test_request_context_stores_reviewer_edited_question_and_provenance(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        generated = "Please confirm whether this access was related to billing, payment, claims, insurance follow-up, or other approved healthcare operations for the patient records linked to this case."
        submitted = "Please confirm whether this access supported billing or insurance follow-up for the records linked to this case."

        case_disposition(
            c.case_id,
            DispositionRequest(
                action="REQUEST_CONTEXT",
                reason_code="NEED_BILLING_JUSTIFICATION",
                notes="Need billing confirmation",
                requested_from_role="Billing Supervisor",
                context_question=submitted,
                suggested_intent="REQUEST_BILLING_JUSTIFICATION",
                suggested_template_id="REQUEST_BILLING_JUSTIFICATION",
                suggested_template_version="1.0",
                suggested_rationale_labels=["Billing role", "Volume spike", "Missing work-queue context"],
                generated_question_text=generated,
            ),
            http_req=_request(),
            db=db,
            current=actor,
        )

        context_request = db.query(ContextRequest).filter(ContextRequest.case_id == c.case_id).one()
        assert context_request.question == submitted
        assert context_request.recommendation_provenance["suggested_template_id"] == "REQUEST_BILLING_JUSTIFICATION"
        assert context_request.recommendation_provenance["generated_question_text"] == generated
        assert context_request.recommendation_provenance["final_submitted_question_text"] == submitted

    def test_escalated_stores_ownership_metadata(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(
                action="ESCALATED",
                reason_code="HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW",
                notes="Escalating for privacy review",
                escalated_to_role="Privacy Officer",
                escalated_to_email="privacy@test.local",
                escalation_reason="High-volume access needs privacy review.",
                escalation_due_date="2026-01-05T10:00:00",
            ),
            http_req=_request(),
            db=db,
            current=actor,
        )
        db.refresh(c)
        detail = get_case(c.case_id, db=db, _user=actor)
        assert c.status == "ESCALATED"
        assert c.escalated_to_role == "Privacy Officer"
        assert c.escalated_to_email == "privacy@test.local"
        assert "High-volume access" in c.escalation_reason
        assert detail["escalated_to_role"] == "Privacy Officer"

    def test_review_started_succeeds_without_reason_code(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="REVIEW_STARTED", notes=""),
            http_req=_request(),
            db=db,
            current=actor,
        )
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert action.reason_code in (None, "")

    def test_comment_added_succeeds_without_reason_code(self, db):
        c = _make_case(db)
        actor = _make_actor(db)
        case_disposition(
            c.case_id,
            DispositionRequest(action="COMMENT_ADDED", notes="Quick note"),
            http_req=_request(),
            db=db,
            current=actor,
        )
        action = db.query(CaseAction).filter(CaseAction.case_id == c.case_id).order_by(CaseAction.created_at.desc()).first()
        assert action.reason_code in (None, "")

    def test_reason_codes_endpoint_returns_expected_options(self, db, compliance_user):
        request_context = reason_codes(action="REQUEST_CONTEXT", db=db, _user=compliance_user)
        escalated = reason_codes(action="ESCALATED", db=db, _user=compliance_user)
        resolved = reason_codes(action="RESOLVED_AUTHORIZED_ACCESS", db=db, _user=compliance_user)
        false_positive = reason_codes(action="FALSE_POSITIVE", db=db, _user=compliance_user)

        assert {r["code"] for r in request_context["reason_codes"]} == {
            "NEED_BILLING_JUSTIFICATION",
            "NEED_PROVIDER_CONFIRMATION",
            "NEED_APPOINTMENT_CONTEXT",
            "NEED_SUPERVISOR_REVIEW",
            "INSUFFICIENT_LOG_CONTEXT",
            "USER_CONFIRMED_ACTIVITY",
            "PASSWORD_RESET_REQUIRED",
            "KNOWN_REMOTE_ACCESS",
        }
        assert {r["code"] for r in escalated["reason_codes"]} == {
            "ACCOUNT_SHARING_SUSPECTED",
            "ESCALATED_TO_SECURITY",
            "POSSIBLE_INAPPROPRIATE_ACCESS",
            "POSSIBLE_CREDENTIAL_MISUSE",
            "HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW",
            "REPEATED_ACCESS_PATTERN",
            "POSSIBLE_BREACH_RISK",
        }
        assert "TREATMENT_RELATIONSHIP_CONFIRMED" in {r["code"] for r in resolved["reason_codes"]}
        assert "LOG_MAPPING_ERROR" in {r["code"] for r in false_positive["reason_codes"]}
