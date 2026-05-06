#!/usr/bin/env python3
"""
Scene 2 – Physician After-Hours Patient Access
OpenEMR source activity seed.

Inserts rows directly into the OpenEMR *log* table so the existing
openemr_real connector reads them during its normal ingestion cycle.
Does NOT create TrustPulse cases directly.

Event plan
----------
  5 × patient-record for dr_nguyen on 2026-05-02 21:05–21:21 UTC (Saturday)
      Each event touches a unique patient (pids[13]–pids[17], slot names P014–P018).
      Timestamps are deterministic and match the spec exactly:
        21:05:00, 21:08:00, 21:12:00, 21:16:00, 21:21:00

  No failed-login events — this scene is strictly a patient-access review,
  not a credential-risk case.

All rows carry  comments = 'TRUSTPULSE_SCENE2_PHYSICIAN_AFTER_HOURS_2026_05_02'
for idempotent cleanup.

USAGE
-----
  TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE=true python seed_scene2_physician_after_hours.py

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
from datetime import datetime

DEMO_MARKER = "TRUSTPULSE_SCENE2_PHYSICIAN_AFTER_HOURS_2026_05_02"
SCENE2_USER = "dr_nguyen"

MARIADB_CONTAINER = os.environ.get("OPENEMR_CONTAINER",      "openemr_mariadb")
MYSQL_USER        = os.environ.get("OPENEMR_MYSQL_USER",     "openemr")
MYSQL_PASS        = os.environ.get("OPENEMR_MYSQL_PASSWORD", "openemrpass")
MYSQL_DB          = os.environ.get("OPENEMR_MYSQL_DATABASE", "openemr")

# Exact event timestamps from spec
_EVENT_TIMES = [
    datetime(2026, 5, 2, 21,  5, 0),
    datetime(2026, 5, 2, 21,  8, 0),
    datetime(2026, 5, 2, 21, 12, 0),
    datetime(2026, 5, 2, 21, 16, 0),
    datetime(2026, 5, 2, 21, 21, 0),
]

# Use the 14th–18th patients (0-indexed 13–17) so scene 2 uses a distinct
# slot from scene 1 (which uses pids[0]–pids[27]).
_PID_OFFSET   = 13
_PID_COUNT    = 5
_MIN_PIDS     = _PID_OFFSET + _PID_COUNT   # 18 total patients required


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

def get_patient_pids() -> list:
    """Return 5 PIDs from slot positions 14–18 (0-indexed 13–17)."""
    out = _mysql_query(
        f"SELECT pid FROM patient_data ORDER BY pid LIMIT {_MIN_PIDS};"
    )
    all_pids = [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]
    return all_pids[_PID_OFFSET : _PID_OFFSET + _PID_COUNT]


# ── Idempotency ───────────────────────────────────────────────────────────────

def count_existing_rows() -> int:
    out = _mysql_query(
        f"SELECT COUNT(*) FROM log WHERE comments='{DEMO_MARKER}';"
    )
    try:
        return int(out.strip())
    except ValueError:
        return 0


# ── SQL generation ────────────────────────────────────────────────────────────

def _ins(ts: datetime, user: str, pid: int) -> str:
    ts_s = ts.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"INSERT INTO log (date, event, user, patient_id, success, comments) "
        f"VALUES ('{ts_s}', 'patient-record', '{user}', {pid}, 1, '{DEMO_MARKER}');"
    )


def build_all_inserts(pids: list) -> list:
    return [_ins(_EVENT_TIMES[i], SCENE2_USER, pids[i]) for i in range(_PID_COUNT)]


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _require_demo_write_flag()

    print(f"Scene 2 Seed  |  marker={DEMO_MARKER}")
    print(f"Demo date: 2026-05-02 (Saturday)  |  user: {SCENE2_USER}")
    print()

    existing = count_existing_rows()
    if existing > 0:
        print(f"WARNING: {existing} Scene 2 rows already present in OpenEMR log.")
        if os.environ.get("SCENE2_FORCE_RESEED", "").lower() != "true":
            print("Run  reset_scene2.py  first, or set  SCENE2_FORCE_RESEED=true  to proceed.")
            sys.exit(1)
        print("SCENE2_FORCE_RESEED=true — proceeding despite existing rows.")
    print()

    print("Querying OpenEMR for patient PIDs...")
    pids = get_patient_pids()
    if len(pids) < _PID_COUNT:
        print(
            f"ERROR: only {len(pids)} patient(s) available at offset {_PID_OFFSET}. "
            f"Need ≥ {_MIN_PIDS} total patients in patient_data. "
            "Run the seed container first:\n"
            "  docker compose up seed",
            file=sys.stderr,
        )
        sys.exit(1)
    pid_display = ", ".join(str(p) for p in pids)
    print(f"  PIDs (slot P014–P018): {pid_display}")
    print()

    stmts = build_all_inserts(pids)
    sql_script = "\n".join(stmts) + "\n"

    print(f"Inserting {len(stmts)} rows into OpenEMR log table...")
    _mysql_exec(sql_script)

    print()
    print("=" * 56)
    print("  SEED SUMMARY – Scene 2 Physician After-Hours Access")
    print("=" * 56)
    print(f"  Source rows inserted:             {len(stmts)}")
    print(f"  Actor:                            {SCENE2_USER}  (Michael Nguyen)")
    print(f"  OpenEMR event type:               patient-record")
    print(f"  TrustPulse event type:            patient_access")
    print(f"  Unique patients (P014–P018):      {_PID_COUNT}")
    print(f"  Time window:                      2026-05-02 21:05–21:21 UTC")
    print(f"  Day of week:                      Saturday (day_of_week=5)")
    print(f"  Marker:                           {DEMO_MARKER}")
    print("=" * 56)
    print()
    print("Next: run  run_scene2_ingestion.sh  to ingest and generate the case.")


if __name__ == "__main__":
    main()
