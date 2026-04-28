#!/usr/bin/env python3
"""
OpenEMR Demo Scenario Generator for TrustPulse

PURPOSE
-------
Creates demo patients, users, and access activity INSIDE a lab OpenEMR instance
so that OpenEMR itself generates real audit log entries.
TrustPulse then reads those logs through its normal read-only connector.

THIS TOOL:
  - writes demo records into OpenEMR (patients, appointments, access events)
  - does NOT write to TrustPulse's own database
  - does NOT insert rows into OpenEMR's audit tables directly
  - does NOT fabricate TrustPulse events

WARNING
-------
Do NOT run against a production OpenEMR system.
All demo records are labelled with TP_DEMO_ prefix for easy identification.

REQUIRES
--------
  TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true   (safety gate)
  OPENEMR_BASE_URL                           (default: http://localhost:8080)
  OPENEMR_ADMIN_USER                         (default: admin)
  OPENEMR_ADMIN_PASS                         (default: pass)

USAGE
-----
  python tools/openemr_demo_scenario_generator.py [--scenario all|A|B|C|D]
  python tools/openemr_demo_scenario_generator.py --list-scenarios

SCENARIO SUMMARY
----------------
  A  Routine access (low-risk baseline)
  B  After-hours access
  C  Bulk patient access (same user, many patients)
  D  Failed-login burst (if OpenEMR logs failed logins)
  E  VIP/no-appointment (only if calendar table accessible)
  F  Modify-then-export (if both event types exist)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

try:
    import requests
    from requests import Session as HTTPSession
except ImportError:
    print("[ERROR] requests not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

ALLOW_DEMO = os.environ.get("TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE", "").lower() == "true"
BASE_URL   = os.environ.get("OPENEMR_BASE_URL", "http://localhost:8080")
ADMIN_USER = os.environ.get("OPENEMR_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("OPENEMR_ADMIN_PASS", "pass")

DEMO_PREFIX    = "TP_DEMO"
DEMO_PATIENTS  = [
    {"fname": f"{DEMO_PREFIX}_Alice",  "lname": "Routine",    "DOB": "1985-03-15", "sex": "Female"},
    {"fname": f"{DEMO_PREFIX}_Bob",    "lname": "Bulktest",   "DOB": "1972-07-22", "sex": "Male"},
    {"fname": f"{DEMO_PREFIX}_Carol",  "lname": "AfterHours", "DOB": "1990-11-05", "sex": "Female"},
    {"fname": f"{DEMO_PREFIX}_Dan",    "lname": "VIPtest",    "DOB": "1965-01-30", "sex": "Male"},
    {"fname": f"{DEMO_PREFIX}_Eve",    "lname": "Exporttest", "DOB": "1998-09-12", "sex": "Female"},
]
# Create 15 extra patients for bulk scenario C
for _i in range(1, 16):
    DEMO_PATIENTS.append({"fname": f"{DEMO_PREFIX}_Extra{_i:02d}",
                          "lname": "BulkPatient", "DOB": "1980-01-01", "sex": "Female"})


# ── Auth / session ────────────────────────────────────────────────────────────

def get_openemr_session(base_url: str, username: str, password: str) -> HTTPSession:
    """
    Obtain an authenticated OpenEMR session using OAuth2 client credentials
    or fall back to a direct session cookie if the REST API is not configured.

    For a lab OpenEMR without a pre-registered OAuth2 client, we attempt to
    register a client automatically (OpenEMR's dynamic registration endpoint).
    """
    s = HTTPSession()
    s.headers.update({"Accept": "application/json"})

    # Step 1: Try dynamic OAuth2 client registration
    reg_url = f"{base_url}/oauth2/default/registration"
    client_payload = {
        "application_type": "private",
        "redirect_uris": [f"{base_url}/trustpulse-callback"],
        "client_name": "TrustPulse Demo Generator",
        "token_endpoint_auth_method": "client_secret_post",
        "scope": (
            "openid offline_access api:oemr api:fhir user/Patient.read "
            "user/Patient.write user/Encounter.read user/Encounter.write"
        ),
        "contacts": ["demo@trustpulse.lab"],
        "grant_types": ["password"],
        "response_types": ["token"],
    }
    try:
        reg_resp = requests.post(reg_url, json=client_payload, timeout=10, verify=False)
        if reg_resp.status_code == 201:
            reg_data = reg_resp.json()
            client_id     = reg_data.get("client_id")
            client_secret = reg_data.get("client_secret")

            token_url = f"{base_url}/oauth2/default/token"
            token_resp = requests.post(token_url, data={
                "grant_type":    "password",
                "client_id":     client_id,
                "client_secret": client_secret,
                "username":      username,
                "password":      password,
                "scope":         client_payload["scope"],
            }, timeout=10, verify=False)

            if token_resp.status_code == 200:
                token = token_resp.json().get("access_token")
                s.headers["Authorization"] = f"Bearer {token}"
                print(f"  [auth] OAuth2 bearer token obtained via {token_url}")
                return s
    except Exception as exc:
        print(f"  [auth] OAuth2 attempt failed ({exc}), trying session cookie...")

    # Step 2: Session cookie fallback (simulates browser login)
    login_url = f"{base_url}/interface/main/main_info.php"
    try:
        login_data = {"authUser": username, "clearPass": password, "languageChoice": "1"}
        resp = s.post(login_url, data=login_data, timeout=15, allow_redirects=True, verify=False)
        if resp.status_code == 200:
            print(f"  [auth] Session cookie login attempted at {login_url}")
        else:
            print(f"  [auth] Login returned HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  [auth] Session login failed: {exc}")
        raise RuntimeError(
            "Could not authenticate with OpenEMR. "
            "Check OPENEMR_BASE_URL, OPENEMR_ADMIN_USER, OPENEMR_ADMIN_PASS."
        )
    return s


# ── Patient operations ────────────────────────────────────────────────────────

def create_demo_patient(s: HTTPSession, base_url: str, p: dict) -> Optional[str]:
    """Create a demo patient via OpenEMR API. Returns patient ID or None."""
    url = f"{base_url}/apis/default/api/patient"
    payload = {
        "fname":   p["fname"],
        "lname":   p["lname"],
        "DOB":     p["DOB"],
        "sex":     p["sex"],
        "address": "123 Demo Lane",
        "city":    "DemoCity",
        "state":   "DC",
        "postal_code": "00000",
    }
    try:
        resp = s.post(url, json={"data": payload}, timeout=15, verify=False)
        if resp.status_code in (200, 201):
            data = resp.json()
            pid  = (data.get("data", {}).get("pid")
                    or data.get("pid")
                    or str(data.get("id", "")))
            if pid:
                print(f"  [patient] Created {p['fname']} {p['lname']} → pid={pid}")
                return str(pid)
        print(f"  [patient] Failed to create {p['fname']}: HTTP {resp.status_code}")
    except Exception as exc:
        print(f"  [patient] Error creating {p['fname']}: {exc}")
    return None


def view_patient(s: HTTPSession, base_url: str, pid: str) -> bool:
    """Access a patient record via OpenEMR API (generates audit log)."""
    url = f"{base_url}/apis/default/api/patient/{pid}"
    try:
        resp = s.get(url, timeout=10, verify=False)
        return resp.status_code == 200
    except Exception:
        return False


def modify_patient(s: HTTPSession, base_url: str, pid: str) -> bool:
    """Update a patient record (generates record_modify audit log)."""
    url = f"{base_url}/apis/default/api/patient/{pid}"
    payload = {"data": {"notes": f"Demo update at {datetime.utcnow().isoformat()}"}}
    try:
        resp = s.put(url, json=payload, timeout=10, verify=False)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def attempt_failed_login(base_url: str, username: str, n: int = 4) -> int:
    """Attempt n failed logins. Returns how many were attempted."""
    attempted = 0
    for i in range(n):
        try:
            s_tmp = HTTPSession()
            resp = s_tmp.post(
                f"{base_url}/interface/main/main_info.php",
                data={"authUser": username, "clearPass": "WRONGPASSWORD_DEMO_FAIL"},
                timeout=10, allow_redirects=False, verify=False
            )
            attempted += 1
            time.sleep(0.3)
        except Exception:
            pass
    return attempted


# ── Scenario record ───────────────────────────────────────────────────────────

class ScenarioRun:
    def __init__(self, scenario_id: str, name: str):
        self.scenario_id  = scenario_id
        self.scenario_name = name
        self.started_at   = datetime.utcnow()
        self.actions      = 0
        self.expected_log_types: list = []
        self.status       = "RUNNING"
        self.notes        = ""

    def complete(self, notes: str = ""):
        self.completed_at = datetime.utcnow()
        self.status       = "COMPLETED"
        self.notes        = notes

    def fail(self, notes: str = ""):
        self.completed_at = datetime.utcnow()
        self.status       = "FAILED"
        self.notes        = notes

    def summary(self) -> dict:
        return {
            "scenario_id":               self.scenario_id,
            "scenario_name":             self.scenario_name,
            "started_at":                self.started_at.isoformat(),
            "completed_at":              getattr(self, "completed_at", None) and self.completed_at.isoformat(),
            "openemr_actions_attempted": self.actions,
            "expected_log_types":        self.expected_log_types,
            "status":                    self.status,
            "notes":                     self.notes,
        }


# ── Scenarios ────────────────────────────────────────────────────────────────���

def scenario_a_routine(s: HTTPSession, base_url: str, pids: list) -> ScenarioRun:
    """A: Routine access during business hours - expected TrustPulse result: low risk."""
    run = ScenarioRun("A", "Routine Patient Access")
    run.expected_log_types = ["patient_access", "login"]
    print("\n[Scenario A] Routine patient access...")
    for pid in pids[:3]:
        if view_patient(s, base_url, pid):
            run.actions += 1
            print(f"  viewed patient pid={pid}")
        time.sleep(0.2)
    run.complete("Scenario A complete - expect low/no risk cases.")
    return run


def scenario_b_after_hours(s: HTTPSession, base_url: str, pids: list) -> ScenarioRun:
    """B: Access outside business hours. TrustPulse R-01 may fire."""
    run = ScenarioRun("B", "After-Hours Access")
    run.expected_log_types = ["patient_access"]
    print("\n[Scenario B] After-hours access (accessing records now - if current hour is outside 07:00-19:00, R-01 fires)...")
    now = datetime.utcnow()
    outside = now.hour < 7 or now.hour >= 19
    print(f"  Current UTC hour: {now.hour:02d}:00 - {'OUTSIDE business hours (R-01 should fire)' if outside else 'inside business hours (configure BUSINESS_HOURS rules for demo)'}")
    for pid in pids[:2]:
        if view_patient(s, base_url, pid):
            run.actions += 1
            print(f"  viewed patient pid={pid}")
        time.sleep(0.2)
    run.complete(f"Scenario B complete (after_hours={outside}).")
    return run


def scenario_c_bulk_access(s: HTTPSession, base_url: str, pids: list) -> ScenarioRun:
    """C: Bulk patient access - TrustPulse R-02/R-08 may fire."""
    run = ScenarioRun("C", "Bulk Patient Access")
    run.expected_log_types = ["patient_access"]
    print(f"\n[Scenario C] Bulk patient access - accessing {len(pids)} patients...")
    for pid in pids:
        if view_patient(s, base_url, pid):
            run.actions += 1
        time.sleep(0.1)
    print(f"  accessed {run.actions} patient records")
    run.complete(f"Scenario C complete - {run.actions} records accessed.")
    return run


def scenario_d_failed_logins(base_url: str) -> ScenarioRun:
    """D: Failed-login burst - TrustPulse R-06 fires if OpenEMR logs failed logins."""
    run = ScenarioRun("D", "Failed-Login Burst")
    run.expected_log_types = ["failed_login"]
    print("\n[Scenario D] Attempting failed logins (may not be logged in all OpenEMR configs)...")
    attempted = attempt_failed_login(base_url, ADMIN_USER, n=5)
    run.actions = attempted
    print(f"  attempted {attempted} failed logins")
    run.complete(
        f"Scenario D complete - {attempted} failed login attempts made. "
        "NOTE: TrustPulse R-06 fires ONLY if OpenEMR logs failed logins in the "
        "accessible 'log' table. If not logged, this is a known limitation."
    )
    return run


def scenario_e_vip(s: HTTPSession, base_url: str, pids: list) -> ScenarioRun:
    """E: Access a patient without a corresponding appointment (VIP context)."""
    run = ScenarioRun("E", "No-Appointment/VIP Access")
    run.expected_log_types = ["patient_access"]
    print("\n[Scenario E] VIP/no-appointment access...")
    print("  NOTE: R-05 evaluates ONLY if openemr_postcalendar_events is accessible.")
    if pids:
        if view_patient(s, base_url, pids[0]):
            run.actions += 1
            print(f"  accessed patient pid={pids[0]} (no appointment created)")
    run.complete(
        "Scenario E: if calendar context is unavailable, R-05 will show 'not evaluated' "
        "rather than firing."
    )
    return run


def scenario_f_modify_export(s: HTTPSession, base_url: str, pids: list) -> ScenarioRun:
    """F: Modify a record then view it (simulates modify-then-export pattern)."""
    run = ScenarioRun("F", "Modify-Then-Export")
    run.expected_log_types = ["record_modify", "patient_access"]
    print("\n[Scenario F] Modify-then-export pattern...")
    if not pids:
        run.fail("No patient IDs available for modify scenario.")
        return run
    pid = pids[0]
    if modify_patient(s, base_url, pid):
        run.actions += 1
        print(f"  modified patient pid={pid}")
        time.sleep(2)
    if view_patient(s, base_url, pid):
        run.actions += 1
        print(f"  viewed (exported) patient pid={pid} - R-07 may fire if within 5 min window")
    run.complete("Scenario F complete - R-07 fires if both event types are in OpenEMR logs within 5 min.")
    return run


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not ALLOW_DEMO:
        print(
            "[ERROR] TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE is not set to 'true'.\n"
            "This tool writes demo records into OpenEMR. "
            "Set TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true to proceed.\n"
            "Do NOT run against a production OpenEMR system.",
            file=sys.stderr,
        )
        sys.exit(1)

    parser = argparse.ArgumentParser(description="TrustPulse OpenEMR Demo Scenario Generator")
    parser.add_argument("--scenario", default="all",
                        help="Scenario to run: all, A, B, C, D, E, F")
    parser.add_argument("--list-scenarios", action="store_true")
    parser.add_argument("--base-url", default=BASE_URL)
    args = parser.parse_args()

    if args.list_scenarios:
        print("Available scenarios:")
        print("  A  Routine access (low risk)")
        print("  B  After-hours access (R-01)")
        print("  C  Bulk patient access (R-02, R-08)")
        print("  D  Failed-login burst (R-06, if OpenEMR logs it)")
        print("  E  VIP/no-appointment (R-05, if calendar accessible)")
        print("  F  Modify-then-export (R-07)")
        return

    print("=" * 60)
    print("TrustPulse OpenEMR Demo Scenario Generator")
    print("WARNING: Writing demo records into OpenEMR at", args.base_url)
    print("Do NOT run against a production OpenEMR system.")
    print("=" * 60)

    try:
        s = get_openemr_session(args.base_url, ADMIN_USER, ADMIN_PASS)
    except Exception as exc:
        print(f"[FATAL] Cannot authenticate with OpenEMR: {exc}", file=sys.stderr)
        sys.exit(1)

    # Create demo patients
    print("\n[setup] Creating demo patients (TP_DEMO_ prefix)...")
    pids = []
    for p in DEMO_PATIENTS:
        pid = create_demo_patient(s, args.base_url, p)
        if pid:
            pids.append(pid)
        time.sleep(0.3)
    print(f"  Created {len(pids)} demo patients")

    if not pids:
        print(
            "[WARN] No patients created - API may not be configured or auth failed.\n"
            "TrustPulse will not generate demo scenarios without patient IDs.\n"
            "See README for how to register an OpenEMR API client.",
            file=sys.stderr,
        )

    runs = []
    sel  = args.scenario.upper()

    if sel in ("ALL", "A"):
        runs.append(scenario_a_routine(s, args.base_url, pids))
    if sel in ("ALL", "B"):
        runs.append(scenario_b_after_hours(s, args.base_url, pids))
    if sel in ("ALL", "C"):
        runs.append(scenario_c_bulk_access(s, args.base_url, pids))
    if sel in ("ALL", "D"):
        runs.append(scenario_d_failed_logins(args.base_url))
    if sel in ("ALL", "E"):
        runs.append(scenario_e_vip(s, args.base_url, pids))
    if sel in ("ALL", "F"):
        runs.append(scenario_f_modify_export(s, args.base_url, pids))

    print("\n" + "=" * 60)
    print("SCENARIO RUN SUMMARY")
    print("=" * 60)
    for r in runs:
        sm = r.summary()
        print(f"  [{sm['scenario_id']}] {sm['scenario_name']}: {sm['status']}")
        if sm["notes"]:
            print(f"       {sm['notes']}")

    print("\nNext steps:")
    print("  1. Wait 60 seconds for TrustPulse's background poller to ingest OpenEMR logs.")
    print("  2. Or POST /api/ingestion/run to trigger immediately.")
    print("  3. View cases at GET /api/cases")
    print("  4. Check telemetry at GET /api/ingestion/status")
    print()
    print("NOTE: TrustPulse cases are generated ONLY from real OpenEMR audit logs.")
    print("      No events were inserted into TrustPulse directly.")


if __name__ == "__main__":
    main()
