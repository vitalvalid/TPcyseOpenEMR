#!/usr/bin/env python3
"""
OpenEMR Demo Cleanup Tool

Deactivates or removes demo records created by openemr_demo_scenario_generator.py.
Only operates through supported OpenEMR API mechanisms.
Does NOT delete audit logs (audit log retention is required by HIPAA).

WARNING: Do NOT run against a production OpenEMR system.

REQUIRES
--------
  TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true
  OPENEMR_BASE_URL, OPENEMR_ADMIN_USER, OPENEMR_ADMIN_PASS
"""
import os
import sys
import time
from datetime import datetime

try:
    import requests
    from requests import Session as HTTPSession
except ImportError:
    print("[ERROR] requests not installed.", file=sys.stderr)
    sys.exit(1)

ALLOW_DEMO = os.environ.get("TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE", "").lower() == "true"
BASE_URL   = os.environ.get("OPENEMR_BASE_URL", "http://localhost:8080")
ADMIN_USER = os.environ.get("OPENEMR_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("OPENEMR_ADMIN_PASS", "pass")
DEMO_PREFIX = "TP_DEMO"


def get_session(base_url: str) -> HTTPSession:
    s = HTTPSession()
    s.headers.update({"Accept": "application/json"})
    resp = s.post(
        f"{base_url}/interface/main/main_info.php",
        data={"authUser": ADMIN_USER, "clearPass": ADMIN_PASS, "languageChoice": "1"},
        timeout=15, allow_redirects=True, verify=False,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Login failed: HTTP {resp.status_code}")
    return s


def list_demo_patients(s: HTTPSession, base_url: str) -> list:
    """Search for patients with TP_DEMO_ prefix."""
    url = f"{base_url}/apis/default/api/patient"
    try:
        resp = s.get(url, params={"fname": DEMO_PREFIX}, timeout=10, verify=False)
        if resp.status_code == 200:
            data = resp.json()
            patients = data.get("data", data) if isinstance(data, dict) else data
            return [p for p in patients if
                    str(p.get("fname", "")).startswith(DEMO_PREFIX)
                    or str(p.get("lname", "")).startswith(DEMO_PREFIX)]
    except Exception as exc:
        print(f"  [warn] Patient list failed: {exc}")
    return []


def deactivate_patient(s: HTTPSession, base_url: str, pid: str) -> bool:
    """Mark patient as inactive via OpenEMR API."""
    url = f"{base_url}/apis/default/api/patient/{pid}"
    try:
        resp = s.put(url, json={"data": {"active": "0"}}, timeout=10, verify=False)
        return resp.status_code in (200, 201)
    except Exception:
        return False


def main():
    if not ALLOW_DEMO:
        print(
            "[ERROR] TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true is required.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("=" * 60)
    print("TrustPulse OpenEMR Demo Cleanup Tool")
    print("WARNING: Modifying OpenEMR at", BASE_URL)
    print("=" * 60)

    print("\nNOTE: This tool does NOT delete OpenEMR audit logs.")
    print("Audit log retention is required under HIPAA §164.312(b) and §164.530(j).")
    print("Demo patients will be deactivated (not hard-deleted) where the API allows.\n")

    try:
        s = get_session(BASE_URL)
    except Exception as exc:
        print(f"[FATAL] Auth failed: {exc}", file=sys.stderr)
        sys.exit(1)

    demo_patients = list_demo_patients(s, BASE_URL)
    print(f"Found {len(demo_patients)} demo patient records")

    deactivated = 0
    for p in demo_patients:
        pid  = str(p.get("pid") or p.get("id", ""))
        name = f"{p.get('fname','')} {p.get('lname','')}".strip()
        if pid and deactivate_patient(s, BASE_URL, pid):
            deactivated += 1
            print(f"  deactivated: {name} (pid={pid})")
        else:
            print(f"  [skip] could not deactivate: {name} (pid={pid})")
        time.sleep(0.2)

    print(f"\nCleanup complete - {deactivated}/{len(demo_patients)} patients deactivated.")
    print("Audit log entries for demo activity have been preserved as required.")


if __name__ == "__main__":
    main()
