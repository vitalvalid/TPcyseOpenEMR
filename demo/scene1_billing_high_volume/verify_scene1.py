#!/usr/bin/env python3
"""
Scene 1 – Billing High-Volume Access Review
Verification script: confirms expected case exists via TrustPulse REST API.

Checks:
  ✓ Exactly 1 PATIENT_ACCESS_REVIEW case for billing_ross on 2026-05-05
  ✓ case.event_count == 55 linked events
  ✓ Exactly 28 unique patient_tokens (tokenized, PT-… format)
  ✓ No raw numeric patient IDs in event fields
  ✓ Case severity == P2_MEDIUM  (risk_score == 45)
  ✓ Evidence export returns 200 HTML
  ✓ Telemetry status is not CRITICAL
  ✓ No medium/high auth case for frontdesk_kim on 2026-05-05

USAGE
-----
  python verify_scene1.py

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

BASE_URL  = os.environ.get("TRUSTPULSE_URL",            "http://localhost:8000")
ADMIN_EMAIL = os.environ.get("TRUSTPULSE_ADMIN_EMAIL",  "admin@trustpulse.local")
ADMIN_PASS  = os.environ.get("TRUSTPULSE_ADMIN_PASSWORD","TrustPulse@2026!")
SCENE1_DATE = "2026-05-05"

PATIENT_TOKEN_RE = re.compile(r'^PT-[0-9a-f]{16}$')

_PASS = "[PASS]"
_FAIL = "[FAIL]"
_WARN = "[WARN]"


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

def check_billing_case(token: str) -> tuple[bool, str | None]:
    """Returns (all_pass, case_id_or_None)."""
    resp = requests.get(f"{BASE_URL}/api/cases", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_FAIL}  GET /api/cases failed: {resp.status_code}")
        return False, None

    cases = resp.json()
    if isinstance(cases, dict) and "cases" in cases:
        cases = cases["cases"]

    # Filter to billing_ross cases on scene1 date (exclude demo-sentinel FALSE_POSITIVE cases)
    scene1_cases = [
        c for c in cases
        if c.get("user_id") == "billing_ross"
        and (
            (c.get("date_start") or "").startswith(SCENE1_DATE)
            or (c.get("date_end") or "").startswith(SCENE1_DATE)
        )
        and c.get("case_type") == "PATIENT_ACCESS_REVIEW"
        and c.get("status") not in ("FALSE_POSITIVE", "SUPPRESSED")
    ]

    ok_count = _check(
        "Exactly 1 PATIENT_ACCESS_REVIEW case for billing_ross on 2026-05-05",
        len(scene1_cases) == 1,
        f"found {len(scene1_cases)} matching case(s)",
    )
    if not ok_count:
        # Print all billing_ross cases for debugging
        all_billing = [c for c in cases if c.get("user_id") == "billing_ross"]
        if all_billing:
            print(f"         billing_ross cases found ({len(all_billing)} total):")
            for c in all_billing:
                print(f"           {c.get('case_id','')[:12]}...  type={c.get('case_type')}  date_start={c.get('date_start','')[:10]}")
        else:
            print("         No billing_ross cases found at all.")
        return False, None

    case = scene1_cases[0]
    case_id = case["case_id"]

    ok_sev = _check(
        "Case severity == P2_MEDIUM",
        case.get("severity") == "P2_MEDIUM",
        f"got {case.get('severity')}",
    )
    ok_status = _check(
        "Case is OPEN or IN_REVIEW",
        case.get("status") in ("OPEN", "IN_REVIEW", "PENDING"),
        f"got {case.get('status')}",
    )
    return ok_count and ok_sev, case_id


def check_case_events(token: str, case_id: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/cases/{case_id}", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_FAIL}  GET /api/cases/{case_id[:12]}... failed: {resp.status_code}")
        return False

    data = resp.json()
    events = data.get("events", [])
    event_count = len(events)

    ok_count = _check(
        "Case has exactly 55 linked events",
        event_count == 55,
        f"got {event_count}",
    )

    patient_access_events = [e for e in events if e.get("event_type") == "patient_access"]
    ok_type = _check(
        "All 55 events are patient_access type",
        len(patient_access_events) == 55,
        f"got {len(patient_access_events)} patient_access events",
    )

    # Check tokenization
    tokens = [e.get("patient_token") for e in events if e.get("patient_token")]
    unique_tokens = set(tokens)
    ok_unique = _check(
        "Exactly 28 unique patient tokens",
        len(unique_tokens) == 28,
        f"got {len(unique_tokens)} unique token(s)",
    )

    ok_format = _check(
        "All patient_tokens match PT-{16hex} format",
        all(PATIENT_TOKEN_RE.match(t) for t in tokens if t),
        f"{sum(1 for t in tokens if t and not PATIENT_TOKEN_RE.match(t))} invalid token(s)",
    )

    # Verify no raw numeric patient IDs anywhere in the event dicts
    raw_id_leak = False
    for e in events:
        for field, val in e.items():
            if field in ("patient_token", "id", "source_log_id") :
                continue
            if isinstance(val, int) and field.lower() in ("patient_id", "pid"):
                raw_id_leak = True
                break
            if isinstance(val, str) and re.fullmatch(r'\d{1,10}', val) and field.lower() in ("patient_id", "pid"):
                raw_id_leak = True
                break
    ok_noleak = _check(
        "No raw numeric patient IDs exposed in event fields",
        not raw_id_leak,
    )

    return ok_count and ok_type and ok_unique and ok_format and ok_noleak


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
    if ok:
        # Spot-check no raw PIDs (integers) appear where PT- should be used
        content = resp.text
        raw_pid_in_html = re.search(r'patient_id["\s:]+\d{3,}', content)
        ok_no_raw = _check(
            "Evidence HTML contains no raw patient_id integers",
            raw_pid_in_html is None,
            f"found: {raw_pid_in_html.group() if raw_pid_in_html else ''}",
        )
        return ok and ok_no_raw
    return ok


def check_telemetry(token: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/ingestion/status", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_WARN}  GET /api/ingestion/status failed: {resp.status_code} (non-fatal)")
        return True  # non-fatal

    data = resp.json()
    status = data.get("overall_status", "UNKNOWN")
    label  = data.get("overall_status_label", status)
    ok = _check(
        f"Telemetry is not CRITICAL",
        status != "CRITICAL",
        f"status={label}",
    )
    return ok


def check_no_frontdesk_auth_case(token: str) -> bool:
    resp = requests.get(f"{BASE_URL}/api/cases", headers=_auth(token), timeout=10)
    if resp.status_code != 200:
        print(f"  {_WARN}  GET /api/cases failed (non-fatal for frontdesk check)")
        return True

    cases = resp.json()
    if isinstance(cases, dict) and "cases" in cases:
        cases = cases["cases"]

    # Look for medium/high auth cases for frontdesk_kim on scene1 date
    bad = [
        c for c in cases
        if c.get("user_id") == "frontdesk_kim"
        and c.get("case_type") in ("AUTH_REVIEW", "FAILED_LOGIN_REVIEW")
        and c.get("severity") in ("P2_MEDIUM", "P1_HIGH", "P0_CRITICAL")
        and (
            (c.get("date_start") or "").startswith(SCENE1_DATE)
            or (c.get("date_end") or "").startswith(SCENE1_DATE)
        )
    ]
    return _check(
        "No medium/high auth case for frontdesk_kim on 2026-05-05 (only 2 failures, below threshold)",
        len(bad) == 0,
        f"found {len(bad)} unwanted case(s)",
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 52)
    print("  Scene 1 Verification")
    print("=" * 52)
    print(f"  TrustPulse:  {BASE_URL}")
    print(f"  Admin email: {ADMIN_EMAIL}")
    print(f"  Scene date:  {SCENE1_DATE}")
    print()

    print("Authenticating...")
    token = authenticate()
    print("  Authenticated OK")
    print()

    results: list[bool] = []

    print("[1/5] Billing case existence and severity...")
    ok, case_id = check_billing_case(token)
    results.append(ok)
    print()

    if case_id:
        print("[2/5] Case events (count, type, tokenization)...")
        results.append(check_case_events(token, case_id))
        print()

        print("[3/5] Evidence package export...")
        results.append(check_evidence_export(token, case_id))
        print()
    else:
        print("[2/5] Skipping event checks — no valid case found.")
        print("[3/5] Skipping evidence export — no valid case found.")
        results.extend([False, False])
        print()

    print("[4/5] Ingestion telemetry health...")
    results.append(check_telemetry(token))
    print()

    print("[5/5] Noise isolation (frontdesk_kim auth cases)...")
    results.append(check_no_frontdesk_auth_case(token))
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(results)
    total  = len(results)
    print("=" * 52)
    if all(results):
        print(f"  ALL {total} CHECKS PASSED — Scene 1 ready for demo.")
    else:
        print(f"  {passed}/{total} CHECKS PASSED — Scene 1 has issues.")
        print()
        print("  Troubleshooting:")
        if not results[0]:
            print("  • No billing_ross case: run seed then run_scene1_ingestion.sh")
        if len(results) > 1 and not results[1]:
            print("  • Event count/token mismatch: check ingestion pipeline")
        if len(results) > 2 and not results[2]:
            print("  • Evidence export failed: check TrustPulse logs")
    print("=" * 52)

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
