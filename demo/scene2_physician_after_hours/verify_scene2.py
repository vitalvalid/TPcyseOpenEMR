#!/usr/bin/env python3
"""
Scene 2 – Physician After-Hours Patient Access
Verification script: confirms expected case exists via TrustPulse REST API.

Checks:
  ✓ Exactly 1 PATIENT_ACCESS_REVIEW case for dr_nguyen on 2026-05-02
  ✓ Case severity is P2_MEDIUM or P3_LOW (OFF_HOURS pattern at 35 pts → P2_MEDIUM)
  ✓ case.event_count == 5 linked events
  ✓ Exactly 5 unique patient tokens (PT-{16hex} format)
  ✓ All 5 events are patient_access type (no failed_login events)
  ✓ No raw numeric patient IDs in event fields
  ✓ Assessment uses clinical language (treatment/on-call), not billing language
  ✓ context_request_recommendation is REQUEST_TREATMENT_OR_ON_CALL_CONTEXT
  ✓ Evidence export returns 200 HTML
  ✓ Telemetry status is not CRITICAL

USAGE
-----
  python verify_scene2.py

ENV VARS (optional overrides)
------------------------------
  TRUSTPULSE_URL            (default: http://localhost:8000)
  TRUSTPULSE_ADMIN_EMAIL    (default: admin@trustpulse.local)
  TRUSTPULSE_ADMIN_PASSWORD (default: TrustPulse@2026!)
"""
import os
import re
import sys
from datetime import date

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed.  Run: pip install requests", file=sys.stderr)
    sys.exit(1)

BASE_URL    = os.environ.get("TRUSTPULSE_URL",            "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("TRUSTPULSE_ADMIN_EMAIL",   "admin@trustpulse.local")
ADMIN_PASS  = os.environ.get("TRUSTPULSE_ADMIN_PASSWORD","TrustPulse@2026!")
SCENE2_DATE = "2026-05-02"
SCENE2_USER = "dr_nguyen"

PATIENT_TOKEN_RE = re.compile(r'^PT-[0-9a-f]{16}$')

_PASS = "[PASS]"
_FAIL = "[FAIL]"
_WARN = "[WARN]"

# Billing-specific language that must NOT appear in clinical context guidance
_BILLING_WORDS = [
    "billing", "payment", "claims", "insurance",
    "billing supervisor", "billing role", "work queue",
]

# Clinical language that MUST appear in the things_to_confirm / quick review guide
_CLINICAL_WORDS = [
    "treatment", "on-call", "care coordination", "care-coordination",
    "clinical", "scheduled", "admitted", "assigned",
]


# ── Auth ──────────────────────────────────────────────────────────────────────

def authenticate() -> str:
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"ERROR: Cannot authenticate. Status {resp.status_code}: {resp.text}", file=sys.stderr)
        sys.exit(1)
    token = resp.json().get("access_token") or resp.json().get("token")
    if not token:
        print(f"ERROR: No access_token in response: {resp.json()}", file=sys.stderr)
        sys.exit(1)
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check(label: str, condition: bool, detail: str = "") -> bool:
    status = _PASS if condition else _FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {status}  {label}{suffix}")
    return condition


# ── Checks ────────────────────────────────────────────────────────────────────

def check_nguyen_case(token: str):
    """Returns (all_pass, case_id_or_None, case_dict_or_None)."""
    resp = requests.get(f"{BASE_URL}/api/cases", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_FAIL}  GET /api/cases failed: {resp.status_code}")
        return False, None, None

    cases = resp.json()
    if isinstance(cases, dict) and "cases" in cases:
        cases = cases["cases"]

    scene2_cases = [
        c for c in cases
        if c.get("user_id") == SCENE2_USER
        and (
            (c.get("date_start") or "").startswith(SCENE2_DATE)
            or (c.get("date_end") or "").startswith(SCENE2_DATE)
        )
        and c.get("case_type") == "PATIENT_ACCESS_REVIEW"
        and c.get("status") not in ("FALSE_POSITIVE", "SUPPRESSED")
    ]

    ok_count = _check(
        "Exactly 1 PATIENT_ACCESS_REVIEW case for dr_nguyen on 2026-05-02",
        len(scene2_cases) == 1,
        f"found {len(scene2_cases)} matching case(s)",
    )
    if not ok_count:
        all_nguyen = [c for c in cases if c.get("user_id") == SCENE2_USER]
        if all_nguyen:
            print(f"         dr_nguyen cases found ({len(all_nguyen)} total):")
            for c in all_nguyen:
                print(f"           {c.get('case_id','')[:12]}...  type={c.get('case_type')}  "
                      f"status={c.get('status')}  date_start={c.get('date_start','')[:10]}")
        else:
            print("         No dr_nguyen cases found at all.")
        return False, None, None

    case = scene2_cases[0]
    case_id = case["case_id"]

    ok_type = _check(
        "Case type is PATIENT_ACCESS_REVIEW",
        case.get("case_type") == "PATIENT_ACCESS_REVIEW",
        f"got {case.get('case_type')}",
    )

    ok_pattern = _check(
        "Pattern type is OFF_HOURS",
        case.get("pattern_type") == "OFF_HOURS",
        f"got {case.get('pattern_type')}",
    )

    severity = case.get("severity", "")
    ok_sev = _check(
        "Severity is P2_MEDIUM or P3_LOW",
        severity in ("P2_MEDIUM", "P3_LOW"),
        f"got {severity}",
    )

    ok_status = _check(
        "Case status is OPEN or UNDER_REVIEW or NEEDS_CONTEXT",
        case.get("status") in ("OPEN", "UNDER_REVIEW", "NEEDS_CONTEXT", "REOPENED"),
        f"got {case.get('status')}",
    )

    return ok_count and ok_type and ok_pattern and ok_sev, case_id, case


def check_case_events(token: str, case_id: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/cases/{case_id}", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_FAIL}  GET /api/cases/{case_id[:12]}... failed: {resp.status_code}")
        return False

    data = resp.json()
    events = data.get("events", [])
    event_count = len(events)

    ok_count = _check(
        "Case has exactly 5 linked events",
        event_count == 5,
        f"got {event_count}",
    )

    patient_access = [e for e in events if e.get("event_type") == "patient_access"]
    ok_type = _check(
        "All 5 events are patient_access type",
        len(patient_access) == 5,
        f"got {len(patient_access)} patient_access events",
    )

    failed_logins = [e for e in events if e.get("event_type") == "failed_login"]
    ok_no_failures = _check(
        "No failed_login events linked",
        len(failed_logins) == 0,
        f"found {len(failed_logins)} failed_login event(s)",
    )

    tokens = [e.get("patient_token") for e in events if e.get("patient_token")]
    unique_tokens = set(tokens)
    ok_unique = _check(
        "Exactly 5 unique patient tokens",
        len(unique_tokens) == 5,
        f"got {len(unique_tokens)} unique token(s)",
    )

    ok_format = _check(
        "All patient_tokens match PT-{16hex} format",
        all(PATIENT_TOKEN_RE.match(t) for t in tokens if t),
        f"{sum(1 for t in tokens if t and not PATIENT_TOKEN_RE.match(t))} invalid token(s)",
    )

    # Verify no raw numeric patient IDs in event fields
    raw_id_leak = False
    for e in events:
        for field, val in e.items():
            if field in ("patient_token", "id", "source_log_id"):
                continue
            if isinstance(val, str) and re.fullmatch(r'\d{1,10}', val) and "patient" in field.lower():
                raw_id_leak = True
                break
    ok_noleak = _check(
        "No raw numeric patient IDs in event fields",
        not raw_id_leak,
    )

    return ok_count and ok_type and ok_no_failures and ok_unique and ok_format and ok_noleak


def check_clinical_guidance(token: str, case_id: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/cases/{case_id}", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_FAIL}  GET /api/cases/{case_id[:12]}... failed for guidance check")
        return False

    data = resp.json()
    assessment = data.get("assessment") or {}
    items_raw = []

    # Pull checklist items from all known locations in the assessment payload
    items_raw += assessment.get("checklist_items", [])
    items_raw += assessment.get("quick_review_checks", [])
    things = assessment.get("things_to_confirm") or {}
    items_raw += things.get("items", [])

    all_text = " ".join(str(i).lower() for i in items_raw)
    if not all_text:
        all_text = str(assessment).lower()

    ok_clinical = _check(
        "Clinical language (treatment/on-call/care-coordination) appears in guidance",
        any(w in all_text for w in _CLINICAL_WORDS),
        f"searched {len(items_raw)} checklist item(s)",
    )

    ok_no_billing = _check(
        "No billing-specific language in guidance",
        not any(w in all_text for w in _BILLING_WORDS),
        f"billing words found: {[w for w in _BILLING_WORDS if w in all_text] or 'none'}",
    )

    # Check context_request_recommendation template
    ctx_rec = assessment.get("context_request_recommendation") or {}
    template_id = ctx_rec.get("template_id", "") or ctx_rec.get("intent", "")
    ok_template = _check(
        "Context request template is treatment/on-call (not billing)",
        "TREATMENT" in template_id.upper() or "ON_CALL" in template_id.upper()
        or "SHIFT" in template_id.upper(),
        f"template_id={template_id!r}",
    )

    # Check suggested question text
    question = (ctx_rec.get("question_text") or "").lower()
    ok_question = _check(
        "Suggested question mentions treatment/on-call/care-coordination",
        any(w in question for w in ["treatment", "on-call", "care coordination", "care-coordination"]),
        f"question={question[:80]!r}",
    )

    # Subject role should be clinician, not billing
    subject_role = (assessment.get("subject_role") or "").lower()
    ok_role = _check(
        "Subject role is clinician (not billing, not nurse)",
        "clinic" in subject_role or "physician" in subject_role or "doctor" in subject_role,
        f"subject_role={subject_role!r}",
    )

    return ok_clinical and ok_no_billing and ok_template and ok_question and ok_role


def check_evidence_export(token: str, case_id: str) -> bool:
    resp = requests.get(
        f"{BASE_URL}/api/evidence/{case_id}",
        headers=_auth(token),
        timeout=20,
    )
    ok = _check(
        "Evidence export returns 200 HTML",
        resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""),
        f"status={resp.status_code} content-type={resp.headers.get('content-type','')}",
    )
    if not ok:
        return False

    content = resp.text

    # Spot-check no raw PIDs appear
    raw_pid_in_html = re.search(r'patient_id["\s:]+\d{3,}', content)
    ok_no_raw = _check(
        "Evidence HTML contains no raw patient_id integers",
        raw_pid_in_html is None,
        f"found: {raw_pid_in_html.group() if raw_pid_in_html else ''}",
    )

    # Evidence must mention clinical context or patient access
    ok_content = _check(
        "Evidence HTML references patient access / clinical context",
        "patient" in content.lower() and ("access" in content.lower() or "clinical" in content.lower()),
    )

    return ok and ok_no_raw and ok_content


def check_telemetry(token: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/ingestion/status", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_WARN}  GET /api/ingestion/status failed: {resp.status_code} (non-fatal)")
        return True

    data = resp.json()
    status = data.get("overall_status", "UNKNOWN")
    label  = data.get("overall_status_label", status)
    return _check(
        "Telemetry is not CRITICAL",
        status != "CRITICAL",
        f"status={label}",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 56)
    print("  Scene 2 Verification – Physician After-Hours Access")
    print("=" * 56)
    print(f"  TrustPulse:  {BASE_URL}")
    print(f"  Admin email: {ADMIN_EMAIL}")
    print(f"  Scene date:  {SCENE2_DATE}")
    print(f"  Actor:       {SCENE2_USER}  (Michael Nguyen)")
    print()

    print("Authenticating...")
    token = authenticate()
    print("  Authenticated OK")
    print()

    results: list = []

    print("[1/5] Case existence, type, severity, and pattern...")
    ok, case_id, _case = check_nguyen_case(token)
    results.append(ok)
    print()

    if case_id:
        print("[2/5] Case events (count, type, tokenization, no failed_login)...")
        results.append(check_case_events(token, case_id))
        print()

        print("[3/5] Clinical guidance (language, template, no billing language)...")
        results.append(check_clinical_guidance(token, case_id))
        print()

        print("[4/5] Evidence package export...")
        results.append(check_evidence_export(token, case_id))
        print()
    else:
        print("[2/5] Skipping event checks — no valid case found.")
        print("[3/5] Skipping guidance checks — no valid case found.")
        print("[4/5] Skipping evidence export — no valid case found.")
        results.extend([False, False, False])
        print()

    print("[5/5] Ingestion telemetry health...")
    results.append(check_telemetry(token))
    print()

    passed = sum(results)
    total  = len(results)
    print("=" * 56)
    if all(results):
        print(f"  ALL {total} CHECKS PASSED — Scene 2 ready for demo.")
    else:
        print(f"  {passed}/{total} CHECKS PASSED — Scene 2 has issues.")
        print()
        print("  Troubleshooting:")
        if not results[0]:
            print("  • No dr_nguyen case: run seed then run_scene2_ingestion.sh")
        if len(results) > 1 and not results[1]:
            print("  • Event count/token mismatch: check ingestion pipeline")
        if len(results) > 2 and not results[2]:
            print("  • Clinical guidance issue: check patient_access_review.py and user role mapping")
        if len(results) > 3 and not results[3]:
            print("  • Evidence export failed: check TrustPulse logs")
    print("=" * 56)

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
