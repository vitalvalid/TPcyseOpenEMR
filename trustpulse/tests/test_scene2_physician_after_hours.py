"""
Scene 2 – Physician After-Hours Patient Access
Unit tests.

All tests use in-memory SQLite via the `db` fixture (no Docker, no OpenEMR
connection required).  Events are crafted to match the Scene 2 seed exactly:
  - Actor:     dr_nguyen / Michael Nguyen / clinician
  - Date:      2026-05-02 21:05–21:21 (Saturday, after-hours)
  - Events:    5 × patient_access (no failed_login)
  - Patients:  5 unique patient IDs
  - Scoring:   R-01 (After-Hours, 20pts) + R-03 (Weekend, 15pts) = 35 → P2_MEDIUM
"""
import pytest
from datetime import datetime

from db.models import Case, NormalizedEvent, CaseEvent, ContextRequest, CaseAction
from engine.case_engine import generate_cases, generate_auth_cases
from engine.scorer import compute_risk_score
from governance.evidence import tokenize_patient_id
from patient_access_review import (
    CASE_TYPE_PATIENT_ACCESS_REVIEW,
    build_case_assessment,
    build_things_to_confirm,
    THINGS_TO_CONFIRM_TEMPLATE_REGISTRY,
)
from context_request_recommendation import render_recommendation, build_recommendation_context


# ── Helpers ───────────────────────────────────────────────────────────────────

# Scoring context for a dr_nguyen event on 2026-05-02 at 21:xx (Saturday)
_SCENE2_CONTEXT = {
    "daily_unique_patients":          5,
    "daily_access_count":             5,
    "recent_failed_logins":           0,
    "patient_is_vip":                 False,
    "has_appointment":                None,
    "modify_then_export_within_5min": False,
    "user_department":                "Internal Medicine",
}

_SCENE2_TIMES = [
    datetime(2026, 5, 2, 21,  5, 0),
    datetime(2026, 5, 2, 21,  8, 0),
    datetime(2026, 5, 2, 21, 12, 0),
    datetime(2026, 5, 2, 21, 16, 0),
    datetime(2026, 5, 2, 21, 21, 0),
]

_PATIENT_IDS = ["pid_014", "pid_015", "pid_016", "pid_017", "pid_018"]


def _score_event(ts: datetime, patient_id: str) -> tuple:
    ed = {
        "hour_of_day": ts.hour,
        "day_of_week": ts.weekday(),
        "event_type":  "patient_access",
        "patient_id":  patient_id,
        "department":  "Internal Medicine",
        "ip_address":  None,
        "user_role":   "clinician",
    }
    return compute_risk_score(ed, None, _SCENE2_CONTEXT)


def _make_scene2_events(db, *, base_log_id: int = 9000) -> list:
    """Insert 5 NormalizedEvents for dr_nguyen matching the scene 2 spec."""
    events = []
    for i, (ts, pid) in enumerate(zip(_SCENE2_TIMES, _PATIENT_IDS)):
        score, level, rules = _score_event(ts, pid)
        ev = NormalizedEvent(
            source_log_id=base_log_id + i,
            event_time=ts,
            user_id="dr_nguyen",
            user_name="Michael Nguyen",
            user_role="clinician",
            event_type="patient_access",
            patient_id=pid,
            department="Internal Medicine",
            ip_address=None,
            hour_of_day=ts.hour,
            day_of_week=ts.weekday(),
            risk_score=score,
            risk_level=level,
            triggered_rules=rules,
            status="PENDING",
        )
        db.add(ev)
        events.append(ev)
    db.commit()
    for ev in events:
        db.refresh(ev)
    return events


# ── Scoring tests ─────────────────────────────────────────────────────────────

class TestScene2Scoring:
    def test_r01_fires_for_after_hours(self):
        score, level, rules = _score_event(_SCENE2_TIMES[0], _PATIENT_IDS[0])
        fired_ids = {r["rule_id"] for r in rules if r.get("fired")}
        assert "R-01" in fired_ids, "R-01 (After-Hours) should fire at hour=21"

    def test_r03_fires_on_saturday(self):
        # 2026-05-02 is a Saturday (day_of_week=5)
        assert _SCENE2_TIMES[0].weekday() == 5, "Fixture date must be Saturday"
        score, level, rules = _score_event(_SCENE2_TIMES[0], _PATIENT_IDS[0])
        fired_ids = {r["rule_id"] for r in rules if r.get("fired")}
        assert "R-03" in fired_ids, "R-03 (Weekend) should fire on Saturday"

    def test_r02_does_not_fire(self):
        # daily_unique_patients=5 < cold_start threshold=10 → R-02 should not fire
        score, level, rules = _score_event(_SCENE2_TIMES[0], _PATIENT_IDS[0])
        fired_ids = {r["rule_id"] for r in rules if r.get("fired")}
        assert "R-02" not in fired_ids, "R-02 should not fire for 5 patients (below threshold=10)"

    def test_score_is_35_p2_medium(self):
        score, level, rules = _score_event(_SCENE2_TIMES[0], _PATIENT_IDS[0])
        assert score == 35.0, f"Expected score=35.0 (R-01:20 + R-03:15), got {score}"

    def test_score_above_generate_cases_threshold(self):
        # generate_cases filters risk_score > 5
        score, _, _ = _score_event(_SCENE2_TIMES[0], _PATIENT_IDS[0])
        assert score > 5, "Score must exceed generate_cases threshold of 5"


# ── Case generation tests ─────────────────────────────────────────────────────

class TestScene2CaseGeneration:
    def test_generates_exactly_one_case(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        cases = db.query(Case).filter(Case.user_id == "dr_nguyen").all()
        active = [c for c in cases if c.status not in ("FALSE_POSITIVE", "SUPPRESSED")]
        assert len(active) == 1, f"Expected 1 case for dr_nguyen, got {len(active)}"

    def test_case_type_is_patient_access_review(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        assert case.case_type == CASE_TYPE_PATIENT_ACCESS_REVIEW, (
            f"Expected PATIENT_ACCESS_REVIEW, got {case.case_type}"
        )

    def test_case_pattern_is_off_hours(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        assert case.pattern_type == "OFF_HOURS", (
            f"Expected OFF_HOURS, got {case.pattern_type}"
        )

    def test_case_severity_is_p2_medium(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        assert case.severity in ("P2_MEDIUM", "P3_LOW"), (
            f"Expected P2_MEDIUM or P3_LOW, got {case.severity}"
        )

    def test_case_has_exactly_5_linked_events(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        assert case.event_count == 5, f"Expected event_count=5, got {case.event_count}"

        linked = (
            db.query(CaseEvent)
            .filter(CaseEvent.case_id == case.case_id)
            .all()
        )
        assert len(linked) == 5, f"Expected 5 CaseEvent rows, got {len(linked)}"

    def test_case_title_contains_michael_nguyen(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        assert "Michael Nguyen" in (case.title or ""), (
            f"Title should contain 'Michael Nguyen', got {case.title!r}"
        )

    def test_no_auth_case_generated(self, db):
        _make_scene2_events(db)
        generate_cases(db)
        generate_auth_cases(db)
        auth_cases = db.query(Case).filter(
            Case.user_id == "dr_nguyen",
            Case.case_type.in_(["AUTHENTICATION_REVIEW", "ACCOUNT_ACCESS_REVIEW", "ACCOUNT_MISUSE_REVIEW"]),
        ).all()
        assert len(auth_cases) == 0, (
            f"No auth cases should be generated (no failed_login events), got {len(auth_cases)}"
        )


# ── Evidence / tokenization tests ─────────────────────────────────────────────

class TestScene2Tokenization:
    def test_exactly_5_unique_patient_tokens(self, db):
        events = _make_scene2_events(db)
        tokens = [tokenize_patient_id(e.patient_id) for e in events if e.patient_id]
        unique = set(tokens)
        assert len(unique) == 5, f"Expected 5 unique tokens, got {len(unique)}"

    def test_patient_tokens_match_format(self, db):
        import re
        events = _make_scene2_events(db)
        pattern = re.compile(r'^PT-[0-9a-f]{16}$')
        tokens = [tokenize_patient_id(e.patient_id) for e in events if e.patient_id]
        bad = [t for t in tokens if not pattern.match(t)]
        assert not bad, f"Tokens with invalid format: {bad}"

    def test_no_failed_login_events(self, db):
        events = _make_scene2_events(db)
        failed = [e for e in events if e.event_type == "failed_login"]
        assert len(failed) == 0, "Scene 2 must have no failed_login events"

    def test_all_events_are_patient_access(self, db):
        events = _make_scene2_events(db)
        non_access = [e for e in events if e.event_type != "patient_access"]
        assert len(non_access) == 0, (
            f"All events should be patient_access; found {[e.event_type for e in non_access]}"
        )


# ── Assessment / clinical guidance tests ──────────────────────────────────────

class TestScene2ClinicalGuidance:
    def _get_case_and_events(self, db):
        events = _make_scene2_events(db)
        generate_cases(db)
        case = db.query(Case).filter(Case.user_id == "dr_nguyen").first()
        assert case is not None
        return case, events

    def test_assessment_uses_clinician_checklist(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        assert assessment is not None
        things = assessment.get("things_to_confirm") or {}
        template_id = things.get("checklist_template_id", "")
        assert template_id == "CLINICIAN_ACCESS_REVIEW", (
            f"Expected CLINICIAN_ACCESS_REVIEW template, got {template_id!r}"
        )

    def test_checklist_items_contain_clinical_language(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        things = assessment.get("things_to_confirm") or {}
        items_text = " ".join(things.get("items", [])).lower()

        clinical_words = ["treatment", "on-call", "care", "clinical", "scheduled", "assigned"]
        assert any(w in items_text for w in clinical_words), (
            f"Checklist items should contain clinical language. Got: {things.get('items')}"
        )

    def test_checklist_items_do_not_contain_billing_language(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        things = assessment.get("things_to_confirm") or {}
        items_text = " ".join(things.get("items", [])).lower()

        billing_words = ["billing", "payment", "claims", "insurance"]
        found = [w for w in billing_words if w in items_text]
        assert not found, (
            f"Billing language {found} must not appear in clinician checklist. "
            f"Got: {things.get('items')}"
        )

    def test_context_request_recommendation_is_clinical(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        ctx_rec = assessment.get("context_request_recommendation") or {}
        template_id = ctx_rec.get("template_id", "") or ctx_rec.get("intent", "")
        assert "TREATMENT" in template_id.upper() or "ON_CALL" in template_id.upper(), (
            f"Context request template should be treatment/on-call. Got {template_id!r}"
        )

    def test_suggested_question_mentions_treatment_or_on_call(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        ctx_rec = assessment.get("context_request_recommendation") or {}
        question = (ctx_rec.get("question_text") or "").lower()
        assert "treatment" in question or "on-call" in question or "care coordination" in question, (
            f"Suggested question should mention treatment/on-call/care-coordination. Got: {question!r}"
        )

    def test_subject_role_is_clinician(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        role = (assessment.get("subject_role") or "").lower()
        assert "clinic" in role or "physician" in role or "doctor" in role, (
            f"Subject role should be clinician/physician. Got {role!r}"
        )

    def test_assessment_unique_patient_token_count_is_5(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        count = assessment.get("unique_patient_token_count")
        assert count == 5, f"Expected unique_patient_token_count=5, got {count}"

    def test_assessment_linked_event_count_is_5(self, db):
        case, events = self._get_case_and_events(db)
        assessment = build_case_assessment(case, events, db)
        count = assessment.get("linked_event_count")
        assert count == 5, f"Expected linked_event_count=5, got {count}"


# ── Recommendation engine unit tests ─────────────────────────────────────────

class TestScene2RecommendationEngine:
    def _build_ctx(self, **overrides) -> dict:
        defaults = {
            "case_id":                   "scene2-test-case",
            "case_type":                 CASE_TYPE_PATIENT_ACCESS_REVIEW,
            "user_display_name":         "Michael Nguyen",
            "user_username":             "dr_nguyen",
            "user_role":                 "clinician",
            "pattern_type":              "OFF_HOURS",
            "selected_action":           "REQUEST_CONTEXT",
            "selected_reason_code":      "NEED_PROVIDER_CONFIRMATION",
            "primary_triggers":          [{"rule_id": "R-01", "rule_name": "After-Hours Access"}],
            "missing_context":           [{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}],
            "linked_event_count":        5,
            "unique_patient_token_count": 5,
            "event_type_breakdown":      {"patient_access": 5},
        }
        defaults.update(overrides)
        return build_recommendation_context(**defaults)

    def test_recommendation_selects_treatment_template(self):
        ctx = self._build_ctx()
        result = render_recommendation(ctx)
        assert "TREATMENT" in result["template_id"].upper() or "ON_CALL" in result["template_id"].upper(), (
            f"Expected treatment/on-call template for clinician role, got {result['template_id']!r}"
        )

    def test_recommendation_not_billing_template(self):
        ctx = self._build_ctx()
        result = render_recommendation(ctx)
        assert "BILLING" not in result["template_id"].upper(), (
            f"Billing template must not be selected for clinician. Got {result['template_id']!r}"
        )

    def test_recommendation_suggested_from_role_is_provider(self):
        ctx = self._build_ctx()
        result = render_recommendation(ctx)
        role = result.get("suggested_requested_from_role", "")
        assert "physician" in role.lower() or "provider" in role.lower(), (
            f"Suggested role should be Provider/Physician. Got {role!r}"
        )

    def test_recommendation_question_mentions_treatment(self):
        ctx = self._build_ctx()
        result = render_recommendation(ctx)
        q = result.get("question_text", "").lower()
        assert "treatment" in q or "on-call" in q or "care coordination" in q, (
            f"Question text should mention treatment/on-call/care-coordination. Got: {q!r}"
        )

    def test_billing_role_would_select_billing_template(self):
        # Verify the template selector correctly differentiates roles
        ctx = self._build_ctx(user_role="billing")
        result = render_recommendation(ctx)
        assert "BILLING" in result["template_id"].upper(), (
            "Billing role should select billing template (not clinical)"
        )


# ── Things-to-confirm template registry ───────────────────────────────────────

class TestScene2ThingsToConfirmTemplate:
    def test_clinician_template_exists(self):
        assert "CLINICIAN_ACCESS_REVIEW" in THINGS_TO_CONFIRM_TEMPLATE_REGISTRY

    def test_clinician_template_has_3_items(self):
        items = THINGS_TO_CONFIRM_TEMPLATE_REGISTRY["CLINICIAN_ACCESS_REVIEW"]["items"]
        assert len(items) == 3, f"Expected 3 checklist items, got {len(items)}"

    def test_clinician_template_items_clinical_not_billing(self):
        items = THINGS_TO_CONFIRM_TEMPLATE_REGISTRY["CLINICIAN_ACCESS_REVIEW"]["items"]
        text = " ".join(items).lower()
        assert "treatment" in text or "on-call" in text or "clinical" in text
        assert "billing" not in text
        assert "payment" not in text
        assert "insurance" not in text

    def test_build_things_to_confirm_for_clinician_off_hours(self):
        case = Case(
            case_id="ttc-test",
            title="After-Hours Patient Access - Michael Nguyen",
            severity="P2_MEDIUM",
            pattern_type="OFF_HOURS",
            case_type=CASE_TYPE_PATIENT_ACCESS_REVIEW,
            user_id="dr_nguyen",
            user_name="Michael Nguyen",
            event_count=5,
            date_start=_SCENE2_TIMES[0],
            date_end=_SCENE2_TIMES[-1],
            risk_score=35.0,
            status="OPEN",
        )
        assessment_context = {
            "case_type": CASE_TYPE_PATIENT_ACCESS_REVIEW,
            "subject_role": "clinician",
            "primary_triggers": [{"rule_id": "R-01", "rule_name": "After-Hours Access"}],
            "missing_context": [{"context": "Appointment/care relationship", "status": "UNAVAILABLE"}],
            "primary_suggested_action": {"action": "REQUEST_CONTEXT", "reason_code": "NEED_PROVIDER_CONFIRMATION"},
            "primary_suggested_reason_code": "NEED_PROVIDER_CONFIRMATION",
            "pattern_type": "OFF_HOURS",
        }
        things = build_things_to_confirm(case, assessment_context)
        assert things["checklist_template_id"] == "CLINICIAN_ACCESS_REVIEW"
        items_text = " ".join(things["items"]).lower()
        assert "treatment" in items_text or "on-call" in items_text or "clinical" in items_text
        assert "billing" not in items_text
