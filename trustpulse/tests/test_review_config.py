"""
Tests for Review Configuration / Baseline Readiness feature.

Covers:
- Backend history_maturity_label() pure function
- Frontend HTML assertions for ReviewConfigPage
- Policy window values
- No ML/UEBA claims in UI text
"""
import sys
import os
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from api.system import history_maturity_label


# ── Backend maturity label unit tests ─────────────────────────────────────────

def test_cold_start_range():
    """0–6 days → Cold Start."""
    for d in range(7):
        assert history_maturity_label(d) == "Cold Start", f"day {d} should be Cold Start"


def test_preliminary_range():
    """7–13 days → Preliminary."""
    for d in range(7, 14):
        assert history_maturity_label(d) == "Preliminary", f"day {d} should be Preliminary"


def test_partial_range():
    """14–29 days → Partial."""
    for d in range(14, 30):
        assert history_maturity_label(d) == "Partial", f"day {d} should be Partial"


def test_stable_range():
    """30+ days → Stable."""
    for d in [30, 31, 45, 60, 90, 180, 365]:
        assert history_maturity_label(d) == "Stable", f"day {d} should be Stable"


def test_boundary_day_7():
    assert history_maturity_label(6)  == "Cold Start"
    assert history_maturity_label(7)  == "Preliminary"


def test_boundary_day_14():
    assert history_maturity_label(13) == "Preliminary"
    assert history_maturity_label(14) == "Partial"


def test_boundary_day_30():
    assert history_maturity_label(29) == "Partial"
    assert history_maturity_label(30) == "Stable"


# ── Frontend HTML assertions ───────────────────────────────────────────────────

def _html():
    return Path(__file__).resolve().parents[1].joinpath("frontend", "index.html").read_text()


def test_review_config_page_function_present():
    html = _html()
    assert "ReviewConfigPage" in html, "ReviewConfigPage component must be defined"


def test_baseline_maturity_info_function_present():
    html = _html()
    assert "baselineMaturityInfo" in html, "baselineMaturityInfo() must be defined"


def test_review_config_nav_item():
    html = _html()
    assert "review_config" in html, "review_config page key must be in nav"
    assert "Review Config" in html, "Review Config label must appear in nav"


def test_route_mapping():
    html = _html()
    assert "config:'review_config'" in html, "config route must map to review_config page"


def test_baseline_maturity_labels_in_html():
    html = _html()
    assert "Cold Start"  in html, "'Cold Start' label must appear in HTML"
    assert "Preliminary" in html, "'Preliminary' label must appear in HTML"
    assert "Partial"     in html, "'Partial' label must appear in HTML"
    assert "Stable"      in html, "'Stable' label must appear in HTML"


def test_baseline_maturity_days_boundaries_in_html():
    """Check that the day boundary comparisons appear in the baselineMaturityInfo function."""
    # Collapse whitespace for comparison
    html_nospace = _html().replace(" ", "")
    assert "days<7"  in html_nospace, "days<7 boundary must be in baselineMaturityInfo"
    assert "days<14" in html_nospace, "days<14 boundary must be in baselineMaturityInfo"
    assert "days<30" in html_nospace, "days<30 boundary must be in baselineMaturityInfo"


def test_review_policies_failed_login_windows():
    html = _html()
    assert "Failed Login Threshold" in html,  "Policy card 'Failed Login Threshold' must be present"
    assert "Failed Login Burst"     in html,  "Policy card 'Failed Login Burst' must be present"
    assert "failed_login_window_minutes"       in html, "failed_login_window_minutes must be referenced"
    assert "failed_login_burst_window_minutes" in html, "failed_login_burst_window_minutes must be referenced"
    assert "within" in html, "'within' text must appear in policy card descriptions"


def test_baseline_readiness_explanation():
    html = _html()
    assert "Thirty days is preferred for stable baseline maturity" in html, \
        "Baseline readiness explanation text must appear"
    assert "Configured policy threshold" in html, \
        "Baseline basis label must appear"


def test_review_policies_read_only_badge():
    html = _html()
    assert "Read-only" in html, "Read-only badge must appear on Review Policies section"


def test_monitored_entities_section():
    html = _html()
    assert "Monitored Entities" in html, "Monitored Entities section header must appear"
    assert "OpenEMR users under review" in html, "User count label must appear"
    assert "Physician / Clinician" in html, "Physician / Clinician role label must appear"


def test_telemetry_health_section():
    html = _html()
    assert "Telemetry Health" in html, "Telemetry Health section must appear"
    assert "Patient Access (24h)" in html, "24h patient-access stat tile must appear"
    assert "Auth Events (24h)"    in html, "24h auth events stat tile must appear"


def test_validation_summary_section():
    html = _html()
    assert "Validation Summary" in html, "Validation Summary section must appear"
    assert "Total OpenEMR rows analyzed" in html, "rows analyzed label must appear"
    assert "Missing required fields"     in html, "missing fields label must appear"
    assert "Rows excluded by policy"     in html, "excluded by policy label must appear"


def test_severity_settings_section():
    html = _html()
    assert "Risk" in html and "Severity" in html, "Risk/Severity section must appear"
    assert "P1 · High"    in html, "P1 High severity card must appear"
    assert "P2 · Medium"  in html, "P2 Medium severity card must appear"
    assert "P3 · Low"     in html, "P3 Low severity card must appear"
    assert "P0 · Critical" in html, "P0 Critical severity card must appear"


def test_no_machine_learning_claims():
    html_lower = _html().lower()
    assert "machine learning"  not in html_lower, "No 'machine learning' text allowed in UI"
    assert "machine-learning"  not in html_lower, "No 'machine-learning' text allowed in UI"


def test_no_ueba_claims_in_visible_ui():
    html = _html()
    # "ueba" must not appear in visible user-facing text
    # Acceptable only inside JS comments or HTML comments
    for line in html.split("\n"):
        stripped = line.strip()
        if "ueba" in stripped.lower():
            # Must be inside a comment
            is_comment = (
                stripped.startswith("//")
                or stripped.startswith("/*")
                or stripped.startswith("*")
                or "<!--" in stripped
            )
            assert is_comment, f"UEBA found in non-comment line: {line!r}"


def test_governance_notice_no_breach_claim():
    html = _html()
    # The governance notice must NOT claim automatic breach detection
    assert "does not make breach-notification determinations" in html, \
        "Governance notice must disclaim automatic breach determination"
    assert "does not" in html, "Governance notice must include limitation statement"
