"""
Tests for evidence report generation - patient tokenization and disclaimers.
"""
import os
import pytest
from types import SimpleNamespace
from governance.evidence import tokenize_patient_id, generate_evidence_html
from db.models import Case
from datetime import datetime


class TestPatientTokenization:
    def test_patient_id_is_tokenized(self):
        token = tokenize_patient_id("12345")
        assert token.startswith("PT-")
        assert "12345" not in token

    def test_none_returns_dash(self):
        assert tokenize_patient_id(None) == "-"

    def test_different_patients_different_tokens(self):
        t1 = tokenize_patient_id("P001")
        t2 = tokenize_patient_id("P002")
        assert t1 != t2

    def test_same_patient_same_token(self):
        t1 = tokenize_patient_id("P100")
        t2 = tokenize_patient_id("P100")
        assert t1 == t2

    def test_token_with_secret(self):
        os.environ["TRUSTPULSE_PATIENT_TOKEN_SECRET"] = "test-secret-123"
        import importlib, governance.evidence as ev_mod
        importlib.reload(ev_mod)
        token = ev_mod.tokenize_patient_id("P001")
        assert token.startswith("PT-")
        del os.environ["TRUSTPULSE_PATIENT_TOKEN_SECRET"]
        importlib.reload(ev_mod)


class TestEvidenceReport:
    def _make_case(self):
        return Case(
            case_id    = "aaaa-bbbb-0001",
            title      = "Test Case",
            severity   = "P1_HIGH",
            pattern_type = "OFF_HOURS",
            user_id    = "dr_test",
            user_name  = "Dr Test",
            event_count = 2,
            date_start = datetime(2026, 1, 1),
            date_end   = datetime(2026, 1, 2),
            risk_score = 40.0,
            recommended_action = "FOLLOW_UP",
            status     = "OPEN",
            breach_risk = False,
            case_type  = "PATIENT_ACCESS_REVIEW",
            hipaa_provisions = ["§164.312(b)"],
            created_at = datetime(2026, 1, 1),
            is_demo    = False,
        )

    def test_report_contains_disclaimer(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "IMPORTANT DISCLAIMER" in html
        assert "supports human compliance/privacy review" in html
        assert "not an automatic finding of inappropriate access, breach, or HIPAA violation" in html
        assert "not a legal determination" in html
        assert "qualified legal and compliance personnel" in html

    def test_report_does_not_claim_tamper_proof(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        lower = html.lower()
        assert "tamper-proof" not in lower
        assert "ocr-ready" not in lower

    def test_demo_banner_shown_for_demo_case(self):
        c = self._make_case()
        c.is_demo = True
        html = generate_evidence_html(c, [], reviewer="co@test.local", is_demo=True)
        assert "DEMO SCENARIO REVIEW" in html

    def test_no_demo_banner_for_production_case(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "DEMO SCENARIO REVIEW" not in html

    def test_report_contains_ingestion_manifest_section(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "Source Ingestion Manifest" in html

    def test_report_notes_manifest_not_available_when_none(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local",
                                      manifest=None)
        assert "No manifest data" in html

    def test_report_has_contiguous_top_level_sections(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "SECTION 1 - CASE SUMMARY" in html
        assert "SECTION 2 - LINKED OPENEMR-DERIVED EVIDENCE" in html
        assert "SECTION 3 - RISK ANALYSIS AND RULE EXPLANATIONS" in html
        assert "SECTION 3A - PATIENT ACCESS APPROPRIATENESS REVIEW" not in html
        assert "SECTION 4 - MISSING CONTEXT / RULES NOT EVALUATED" in html
        assert "SECTION 5 - DISPOSITION AND CHAIN OF CUSTODY" in html
        assert "SECTION 5A - HUMAN REVIEW ACTION HISTORY" in html
        assert "SECTION 6 - TELEMETRY INTEGRITY & PROVENANCE" in html
        assert "SECTION 7 - LIMITATIONS AND DISCLAIMER" in html

    def test_report_uses_review_support_wording(self):
        assessment = {
            "case_type": "PATIENT_ACCESS_REVIEW",
            "summary": "This billing user accessed more patient records than expected for this review window.",
            "subject_user": "David Ross",
            "subject_username": "billing_ross",
            "subject_role": "Billing",
            "subject_department": "Billing",
            "linked_event_count": 3,
            "unique_patient_token_count": 3,
            "quick_review_reason": "This billing user accessed more patient records than expected for this review window.",
            "guidance_rationale": "Generated from structured patient-access review facts.",
            "quick_review_checks": ["Billing reason", "Work queue", "Supervisor confirmation"],
            "primary_suggested_action": {"action": "REQUEST_CONTEXT", "action_label": "Request billing justification"},
            "alternate_suggested_action": {},
            "missing_information_summary": [],
            "missing_context": [],
            "suggested_review_actions": [],
            "suggested_reason_codes": [],
            "technical_details": {},
            "related_authentication_signals": [],
        }
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local", patient_access_assessment=assessment)
        assert "Quick Review Guide / Review Support" in html

    def test_report_uses_openemr_derived_telemetry_wording(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "linked OpenEMR-derived audit events observed by TrustPulse" in html
        assert "events from real OpenEMR logs" not in html

    def test_report_uses_generated_by_when_no_review_action_exists(self):
        html = generate_evidence_html(
            self._make_case(),
            [],
            reviewer="exporter@test.local",
            reviewer_role="AUDITOR",
            actions=[],
        )
        assert "Generated by: exporter@test.local (AUDITOR)" in html
        assert "Reviewed by:" not in html
        assert "Current review status: OPEN" in html
        assert "Human review status: No disposition actions recorded" in html

    def test_report_uses_latest_substantive_case_action_for_latest_reviewer(self):
        actions = [
            SimpleNamespace(
                action="EVIDENCE_EXPORTED",
                actor_email="exporter@test.local",
                actor_role="AUDITOR",
                created_at=datetime(2026, 1, 1, 10, 0, 0),
                previous_status="OPEN",
                new_status="OPEN",
                notes="export",
                reason_code="",
                reason_label_snapshot="",
                record_hash="a" * 64,
            ),
            SimpleNamespace(
                action="REQUEST_CONTEXT",
                actor_email="reviewer@test.local",
                actor_role="COMPLIANCE_OFFICER",
                created_at=datetime(2026, 1, 1, 9, 0, 0),
                previous_status="OPEN",
                new_status="NEEDS_CONTEXT",
                notes="needs review",
                reason_code="NEED_PROVIDER_CONFIRMATION",
                reason_label_snapshot="Need provider confirmation",
                record_hash="b" * 64,
            ),
        ]
        case = self._make_case()
        case.status = "NEEDS_CONTEXT"

        html = generate_evidence_html(
            case,
            [],
            reviewer="exporter@test.local",
            reviewer_role="AUDITOR",
            actions=actions,
        )
        assert "Generated by: exporter@test.local (AUDITOR)" in html
        assert "Latest reviewer: reviewer@test.local (COMPLIANCE_OFFICER)" in html
        assert "Reviewed by:" not in html
        assert "Current review status: NEEDS_CONTEXT" in html

    def test_evidence_history_includes_reason_code_and_label(self):
        actions = [
            SimpleNamespace(
                created_at=datetime(2026, 1, 1, 9, 0, 0),
                actor_email="reviewer@test.local",
                actor_role="COMPLIANCE_OFFICER",
                action="REQUEST_CONTEXT",
                previous_status="UNDER_REVIEW",
                new_status="NEEDS_CONTEXT",
                reason_code="NEED_PROVIDER_CONFIRMATION",
                reason_label_snapshot="Need provider confirmation",
                notes="Need attending confirmation",
                record_hash="c" * 64,
            ),
        ]
        html = generate_evidence_html(
            self._make_case(),
            [],
            reviewer="exporter@test.local",
            reviewer_role="AUDITOR",
            actions=actions,
        )
        assert "Reason Code" in html
        assert "Reason Label" in html
        assert "NEED_PROVIDER_CONFIRMATION" in html
        assert "Need provider confirmation" in html

    def test_report_includes_hipaa_reference_note_and_labels(self):
        case = self._make_case()
        case.hipaa_provisions = [
            "§164.312(b)",
            "§164.502(b)",
            "§164.308(a)(1)",
            "HIPAA 45 CFR §164.312(a)(2)(i)",
        ]
        html = generate_evidence_html(case, [], reviewer="co@test.local")
        assert "Provision tags are compliance-review references, not automatic findings of violation." in html
        assert "Audit controls" in html
        assert "Minimum necessary standard" in html
        assert "Security management process" in html
        assert "Unique user identification" in html
        assert "automatic findings of violation" in html

    def test_not_evaluated_rules_are_grouped_by_rule_and_reason(self):
        events = [
            SimpleNamespace(
                id=1,
                source_log_id=1001,
                event_time=datetime(2026, 1, 1, 10, 0, 0),
                user_id="dr_test",
                event_type="patient_access",
                patient_id="PAT-001",
                ip_address="10.0.0.1",
                department="Cardiology",
                risk_score=40.0,
                triggered_rules=[
                    {
                        "rule_id": "R-04",
                        "rule_name": "Cross-Department Access",
                        "not_evaluated": True,
                        "not_evaluated_reason": "Department roster unavailable",
                        "fired": False,
                    }
                ],
            ),
            SimpleNamespace(
                id=2,
                source_log_id=1002,
                event_time=datetime(2026, 1, 1, 10, 5, 0),
                user_id="dr_test",
                event_type="patient_access",
                patient_id="PAT-002",
                ip_address="10.0.0.2",
                department="Cardiology",
                risk_score=41.0,
                triggered_rules=[
                    {
                        "rule_id": "R-04",
                        "rule_name": "Cross-Department Access",
                        "not_evaluated": True,
                        "not_evaluated_reason": "Department roster unavailable",
                        "fired": False,
                    },
                    {
                        "rule_id": "R-04",
                        "rule_name": "Cross-Department Access",
                        "not_evaluated": True,
                        "not_evaluated_reason": "Department roster unavailable",
                        "fired": False,
                    }
                ],
            ),
        ]

        html = generate_evidence_html(self._make_case(), events, reviewer="co@test.local")

        assert "SECTION 2 - LINKED OPENEMR-DERIVED EVIDENCE" in html
        assert "2 linked OpenEMR-derived audit events observed by TrustPulse." in html
        assert html.count("Department roster unavailable") == 1
        assert "Rules not evaluated due to missing context:" in html
        assert "Affected linked events: 2" in html

    def test_report_includes_patient_access_review_section_and_disclaimer(self):
        assessment = {
            "case_type": "PATIENT_ACCESS_REVIEW",
            "summary": "Billing user accessed 27 patient records in one day, requiring review.",
            "subject_user": "David Ross",
            "subject_username": "billing_ross",
            "subject_role": "Billing",
            "subject_department": "Demo Health Clinic",
            "linked_event_count": 27,
            "unique_patient_token_count": 12,
            "event_type_breakdown": {"patient_access": 25, "failed_login": 2},
            "baseline_maturity": "COLD_START",
            "primary_triggers": [
                {
                    "rule_id": "R-02",
                    "rule_name": "Bulk Patient Access",
                    "severity": "HIGH",
                    "confidence": "LOW",
                    "explanation": "Accessed 12 unique patients today (threshold 10, baseline avg 0.0, maturity=COLD_START)",
                }
            ],
            "missing_context": [
                {
                    "context": "Appointment/care relationship",
                    "status": "UNAVAILABLE",
                    "reason": "Appointment context unavailable - openemr_postcalendar_events table not accessible.",
                    "effect_on_confidence": "TrustPulse cannot determine whether the accessed patients had appointment or care-relationship context on the event date.",
                }
            ],
            "quick_review_reason": "Billing user accessed 27 patient records, above the expected review threshold.",
            "quick_review_checks": [
                "Confirm whether the access was related to billing, payment, claims, or insurance operations."
            ],
            "guidance_rationale": "Generated from: Billing role, Volume Spike, Bulk Patient Access, missing appointment/care relationship",
            "missing_information_summary": [
                "Appointment/work-queue context was not available from the accessible OpenEMR logs."
            ],
            "suggested_review_actions": [
                {
                    "action": "REQUEST_CONTEXT",
                    "reason_code": "NEED_BILLING_JUSTIFICATION",
                    "reason_label": "Need billing/payment justification",
                    "rationale": "Additional billing context is needed.",
                }
            ],
            "primary_suggested_action": {
                "action": "REQUEST_CONTEXT",
                "reason_code": "NEED_BILLING_JUSTIFICATION",
                "reason_label": "Need billing/payment justification",
                "rationale": "Additional billing context is needed.",
            },
            "primary_suggested_reason_code": "NEED_BILLING_JUSTIFICATION",
            "alternate_suggested_action": {
                "action": "ESCALATED",
                "reason_code": "HIGH_VOLUME_REQUIRES_PRIVACY_REVIEW",
                "reason_label": "High-volume access requires privacy review",
                "rationale": "Escalate if the business reason cannot be confirmed.",
            },
            "technical_details": {
                "triggered_rules": [
                    {
                        "rule_id": "R-02",
                        "rule_name": "Bulk Patient Access",
                        "confidence": "LOW",
                        "explanation": "Accessed 12 unique patients today (threshold 10, baseline avg 0.0, maturity=COLD_START)",
                        "supporting_fields": {"threshold": 10},
                    }
                ],
                "baseline_maturity": "COLD_START",
                "missing_source_details": ["Appointment context unavailable - openemr_postcalendar_events table not accessible."],
                "suggested_reason_codes": [
                    {
                        "action": "REQUEST_CONTEXT",
                        "reason_code": "NEED_BILLING_JUSTIFICATION",
                        "reason_label": "Need billing/payment justification",
                    }
                ],
                "check_sources": [
                    {
                        "item": "Confirm whether the access was related to billing, payment, claims, or insurance operations.",
                        "source": {"type": "role", "value": "Billing"},
                    }
                ],
            },
            "suggested_reason_codes": [
                {
                    "action": "REQUEST_CONTEXT",
                    "reason_code": "NEED_BILLING_JUSTIFICATION",
                    "reason_label": "Need billing/payment justification",
                }
            ],
        }
        html = generate_evidence_html(
            self._make_case(),
            [],
            reviewer="co@test.local",
            patient_access_assessment=assessment,
        )
        assert "Quick Review Guide" in html
        assert "SECTION 3A - PATIENT ACCESS APPROPRIATENESS REVIEW" in html
        assert "supports human review of patient-access appropriateness" in html
        assert "supports human compliance/privacy review" in html
        assert "not an automatic finding of inappropriate access, breach, or HIPAA violation" in html
        assert "Need billing/payment justification" in html
        assert "Show technical details" not in html

    def test_report_includes_context_request_and_escalation_sections(self):
        case = self._make_case()
        case.escalated_to_role = "Privacy Officer"
        case.escalated_to_email = "privacy@test.local"
        case.escalated_at = datetime(2026, 1, 3, 10, 0, 0)
        case.escalation_reason = "High-volume access needs privacy review."
        case.escalation_due_at = datetime(2026, 1, 4, 12, 0, 0)
        context_requests = [
            SimpleNamespace(
                requested_by_email="co@test.local",
                requested_from_role="Billing Supervisor",
                requested_from_email="billing@test.local",
                question="Was this access related to billing/payment operations for these patient accounts?",
                due_at=datetime(2026, 1, 3, 12, 0, 0),
                status="RESPONDED",
                response_text="Yes, this was billing follow-up.",
                responded_by_email="billing@test.local",
            )
        ]
        html = generate_evidence_html(
            case,
            [],
            reviewer="co@test.local",
            context_requests=context_requests,
        )
        assert "SECTION 5B - CONTEXT REQUESTS" in html
        assert "Billing Supervisor" in html
        assert "Yes, this was billing follow-up." in html
        assert "SECTION 5C - ESCALATION" in html
        assert "Privacy Officer" in html
        assert "High-volume access needs privacy review." in html

    def test_report_includes_context_request_provenance_and_final_submitted_question(self):
        case = self._make_case()
        context_requests = [
            SimpleNamespace(
                id=7,
                requested_by_email="co@test.local",
                requested_from_role="Billing Supervisor",
                requested_from_email="billing@test.local",
                question="Please confirm whether this access supported billing follow-up for the records linked to this case.",
                due_at=datetime(2026, 1, 3, 12, 0, 0),
                status="PENDING",
                response_text=None,
                responded_by_email=None,
                recommendation_provenance={
                    "suggested_template_id": "REQUEST_BILLING_JUSTIFICATION",
                    "suggested_rationale_labels": ["Billing role", "Volume spike", "Missing work-queue context"],
                    "generated_question_text": "Please confirm whether this access was related to billing, payment, claims, insurance follow-up, or other approved healthcare operations for the patient records linked to this case.",
                    "final_submitted_question_text": "Please confirm whether this access supported billing follow-up for the records linked to this case.",
                },
            )
        ]
        html = generate_evidence_html(
            case,
            [],
            reviewer="co@test.local",
            context_requests=context_requests,
        )
        assert "Context Request Recommendation Provenance" in html
        assert "REQUEST_BILLING_JUSTIFICATION" in html
        assert "Suggested because: Billing role" in html
        assert "Submitted question:" in html
        assert "billing follow-up for the records linked to this case" in html

    def test_authentication_review_evidence_uses_auth_labels(self):
        case = self._make_case()
        case.case_type = "AUTHENTICATION_REVIEW"
        case.pattern_type = "FAILED_LOGIN_ACTIVITY"
        assessment = {
            "case_type": "AUTHENTICATION_REVIEW",
            "summary": "Authentication review for repeated failed login attempts.",
            "subject_user": "Dr Test",
            "subject_username": "dr_test",
            "subject_role": "Physician",
            "linked_event_count": 3,
            "quick_review_checks": [
                "User expected this login or access",
                "Device or work location was expected",
                "Escalate to IT/security if not confirmed",
            ],
            "primary_suggested_action": {"action": "REQUEST_CONTEXT", "reason_label": "User confirmed activity", "reason_code": "USER_CONFIRMED_ACTIVITY"},
            "suggested_review_actions": [],
            "event_type_breakdown": {"failed_login": 3},
            "authentication_summary": {
                "failed_login_count": 3,
                "successful_login_after_failures": False,
                "patient_access_after_login": False,
                "window_minutes": 30,
                "review_focus": "Repeated failed login attempts",
            },
            "related_authentication_signals": [],
        }
        html = generate_evidence_html(case, [], reviewer="co@test.local", patient_access_assessment=assessment)
        assert "SECTION 3A - AUTHENTICATION REVIEW SUMMARY" in html
        assert "Patient Access Review Notice" not in html
        assert "Repeated failed login attempts" in html
        assert "not proof of account compromise" in html
