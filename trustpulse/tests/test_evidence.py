"""
Tests for evidence report generation - patient tokenization and disclaimers.
"""
import os
import pytest
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
            hipaa_provisions = ["§164.312(b)"],
            created_at = datetime(2026, 1, 1),
            is_demo    = False,
        )

    def test_report_contains_disclaimer(self):
        html = generate_evidence_html(self._make_case(), [], reviewer="co@test.local")
        assert "IMPORTANT DISCLAIMER" in html
        assert "not a legal determination" in html

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
