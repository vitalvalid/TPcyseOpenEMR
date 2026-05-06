#!/usr/bin/env python3
"""
Scene 1 – Billing High-Volume Access Review
OpenEMR source activity seed.

Inserts rows directly into the OpenEMR *log* table so the existing
openemr_real connector reads them during its normal ingestion cycle.
Does NOT create TrustPulse cases directly.

Event plan
----------
  55 × patient-record for billing_ross on 2026-05-05 10:00–10:45
      First 28 events each touch a unique patient (pids[0]–pids[27]).
      Remaining 27 events repeat from the same 28-patient pool.
      Timestamps deterministic: base + i * 50 s → spans exactly 45 min.

  Background noise (same date, different users):
      5  login-success for dr_patel / frontdesk_kim / priya_patel
      8  patient-record for dr_patel / frontdesk_kim
      2  login-failure  for frontdesk_kim   (only 2 → below auth threshold of 3)

All rows carry  comments = 'TRUSTPULSE_SCENE1_BILLING_2026_05_05'
for idempotent cleanup.

USAGE
-----
  TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python seed_scene1_openemr_activity.py

ENV VARS (optional overrides)
------------------------------
  OPENEMR_CONTAINER       docker container name  (default: openemr_mariadb)
  OPENEMR_MYSQL_USER      DB user with INSERT     (default: openemr)
  OPENEMR_MYSQL_PASSWORD  DB password             (default: openemrpass)
  OPENEMR_MYSQL_DATABASE  database name           (default: openemr)
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta

DEMO_MARKER = "TRUSTPULSE_SCENE1_BILLING_2026_05_05"
SCENE1_USER = "billing_ross"
DEMO_DATE   = datetime(2026, 5, 5)

MARIADB_CONTAINER = os.environ.get("OPENEMR_CONTAINER",       "openemr_mariadb")
MYSQL_USER        = os.environ.get("OPENEMR_MYSQL_USER",      "openemr")
MYSQL_PASS        = os.environ.get("OPENEMR_MYSQL_PASSWORD",  "openemrpass")
MYSQL_DB          = os.environ.get("OPENEMR_MYSQL_DATABASE",  "openemr")


# ── Safety gate ───────────────────────────────────────────────────────────────

def _require_demo_write_flag() -> None:
    if os.environ.get("TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE", "").lower() != "true":
        print(
            "ERROR: TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE is not set to 'true'.\n"
            "This script writes demo rows directly into the OpenEMR log table.\n"
            "Set TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true to proceed.\n"
            "Do NOT run against a production OpenEMR instance.",
            file=sys.stderr,
        )
        sys.exit(1)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _mysql_query(sql: str) -> str:
    """Run a SELECT via docker exec and return stripped stdout."""
    result = subprocess.run(
        [
            "docker", "exec", MARIADB_CONTAINER,
            "mysql",
            "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
            "--batch", "--skip-column-names",
            "-e", sql,
        ],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _mysql_exec(sql_script: str) -> None:
    """Pipe a multi-statement SQL script into MariaDB via docker exec -i."""
    subprocess.run(
        [
            "docker", "exec", "-i", MARIADB_CONTAINER,
            "mysql",
            "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
        ],
        input=sql_script.encode(),
        check=True,
    )


# ── Patient PIDs ──────────────────────────────────────────────────────────────

def get_patient_pids(count: int = 28) -> list[int]:
    """Return the first *count* patient PIDs from OpenEMR patient_data."""
    out = _mysql_query(
        f"SELECT pid FROM patient_data ORDER BY pid LIMIT {count};"
    )
    return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]


# ── Idempotency check ─────────────────────────────────────────────────────────

def count_existing_rows() -> int:
    out = _mysql_query(
        f"SELECT COUNT(*) FROM log WHERE comments='{DEMO_MARKER}';"
    )
    try:
        return int(out.strip())
    except ValueError:
        return 0


# ── SQL generation ────────────────────────────────────────────────────────────

def _ins(ts: datetime, event: str, user: str, pid, success: int) -> str:
    ts_s  = ts.strftime("%Y-%m-%d %H:%M:%S")
    pid_s = str(pid) if pid is not None else "NULL"
    return (
        f"INSERT INTO log (date, event, user, patient_id, success, comments) "
        f"VALUES ('{ts_s}', '{event}', '{user}', {pid_s}, {success}, '{DEMO_MARKER}');"
    )


def build_all_inserts(pids: list[int]) -> list[str]:
    stmts: list[str] = []

    # ── Main billing_ross patient-access events ───────────────────────────────
    base = datetime(2026, 5, 5, 10, 0, 0)   # 10:00 AM UTC on demo date
    for i in range(55):
        ts  = base + timedelta(seconds=i * 50)
        pid = pids[i % 28]                   # first 28 each unique; next 27 repeat
        stmts.append(_ins(ts, "patient-record", SCENE1_USER, pid, 1))

    # ── Background noise: 5 successful login events ───────────────────────────
    # These normalise to event_type='login' (auth category) and do not contribute
    # to the billing case.
    noise_logins = [
        (datetime(2026, 5, 5,  8, 15), "dr_patel"),
        (datetime(2026, 5, 5,  9,  0), "dr_patel"),
        (datetime(2026, 5, 5,  8, 30), "frontdesk_kim"),
        (datetime(2026, 5, 5,  9, 15), "frontdesk_kim"),
        (datetime(2026, 5, 5, 11, 30), "priya_patel"),
    ]
    for ts, user in noise_logins:
        stmts.append(_ins(ts, "login-success", user, None, 1))

    # ── Background noise: 8 patient-access events ────────────────────────────
    # Only 8 events for dr_patel/frontdesk_kim → daily_unique_patients ≤ 8 < 10
    # and daily_access_count ≤ 8 < 20 → R-02/R-08 do NOT fire for those users.
    noise_access = [
        (datetime(2026, 5, 5,  9,  5), "dr_patel",      pids[0]),
        (datetime(2026, 5, 5,  9, 15), "dr_patel",      pids[1]),
        (datetime(2026, 5, 5,  9, 25), "dr_patel",      pids[2]),
        (datetime(2026, 5, 5,  9, 35), "dr_patel",      pids[3]),
        (datetime(2026, 5, 5,  9, 45), "dr_patel",      pids[4]),
        (datetime(2026, 5, 5, 10, 55), "frontdesk_kim", pids[0]),
        (datetime(2026, 5, 5, 11,  5), "frontdesk_kim", pids[1]),
        (datetime(2026, 5, 5, 11, 15), "frontdesk_kim", pids[2]),
    ]
    for ts, user, pid in noise_access:
        stmts.append(_ins(ts, "patient-record", user, pid, 1))

    # ── Background noise: 2 failed-login events ───────────────────────────────
    # Only 2 events for frontdesk_kim → below FAILED_LOGIN_REVIEW_THRESHOLD (3).
    # generate_auth_cases will NOT create a medium/high auth review case.
    for ts in [datetime(2026, 5, 5, 11, 0), datetime(2026, 5, 5, 11, 1)]:
        stmts.append(_ins(ts, "login-failure", "frontdesk_kim", None, 0))

    return stmts


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _require_demo_write_flag()

    print(f"Scene 1 Seed  |  marker={DEMO_MARKER}")
    print(f"Demo date: 2026-05-05  |  user: {SCENE1_USER}")
    print()

    # Idempotency guard
    existing = count_existing_rows()
    if existing > 0:
        print(f"WARNING: {existing} Scene 1 rows already present in OpenEMR log.")
        if os.environ.get("SCENE1_FORCE_RESEED", "").lower() != "true":
            print("Run  reset_scene1.py  first, or set  SCENE1_FORCE_RESEED=true  to proceed.")
            sys.exit(1)
        print("SCENE1_FORCE_RESEED=true — proceeding despite existing rows.")
    print()

    # Fetch patient PIDs
    print("Querying OpenEMR for patient PIDs...")
    pids = get_patient_pids(28)
    if len(pids) < 28:
        print(
            f"ERROR: only {len(pids)} patient(s) found in OpenEMR patient_data. "
            "Expected ≥ 28. Run the seed container first:\n"
            "  docker compose up seed",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"  PIDs available: {pids[0]} … {pids[-1]}  (using first 28)")
    print()

    # Build and execute
    stmts = build_all_inserts(pids)
    sql_script = "\n".join(stmts) + "\n"

    print(f"Inserting {len(stmts)} rows into OpenEMR log table...")
    _mysql_exec(sql_script)

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 52)
    print("  SEED SUMMARY")
    print("=" * 52)
    print(f"  Source rows inserted (total):        {len(stmts)}")
    print(f"  billing_ross patient_access events:  55")
    print(f"  Unique patients (billing_ross):       28")
    print(f"  Time window:                          2026-05-05 10:00–10:45 UTC")
    print(f"  Background login events:              5")
    print(f"  Background patient_access events:     8")
    print(f"  Background failed_login events:       2  (frontdesk_kim)")
    print(f"  Marker:                               {DEMO_MARKER}")
    print("=" * 52)
    print()
    print("Next: run  run_scene1_ingestion.sh  to ingest and generate the case.")


if __name__ == "__main__":
    main()
