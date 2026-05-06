#!/usr/bin/env python3
"""
Scene 1 – Billing High-Volume Access Review
Idempotent reset script.

Removes prior Scene 1 demo data in two phases:

  Phase 1 – TrustPulse DB cleanup (via docker exec in trustpulse_app)
      Deletes:
        • NormalizedEvents for scene1 users on 2026-05-05
        • CaseEvents, CaseActions, ContextRequests for linked billing_ross cases
        • The Case records themselves

  Phase 2 – OpenEMR log cleanup (via docker exec in openemr_mariadb)
      Deletes rows WHERE comments = 'TRUSTPULSE_SCENE1_BILLING_2026_05_05'

Phase 1 runs before Phase 2 so source_log_ids are still in the TrustPulse
DB when we look up which events to delete.

SAFE TO RUN MULTIPLE TIMES – idempotent.
Does NOT touch OpenEMR users, patient_data, or other log rows.
Does NOT wipe unrelated TrustPulse cases or events.

USAGE
-----
  python reset_scene1.py

ENV VARS (optional overrides)
------------------------------
  TRUSTPULSE_CONTAINER  (default: trustpulse_app)
  OPENEMR_CONTAINER     (default: openemr_mariadb)
  OPENEMR_MYSQL_USER    (default: openemr)
  OPENEMR_MYSQL_PASSWORD (default: openemrpass)
  OPENEMR_MYSQL_DATABASE (default: openemr)
"""
import os
import subprocess
import sys

DEMO_MARKER = "TRUSTPULSE_SCENE1_BILLING_2026_05_05"
SCENE1_USERS = ["billing_ross", "dr_patel", "frontdesk_kim", "priya_patel"]
SCENE1_DATE_START = "2026-05-05 08:00:00"
SCENE1_DATE_END   = "2026-05-05 14:00:00"

TP_CONTAINER      = os.environ.get("TRUSTPULSE_CONTAINER",    "trustpulse_app")
MARIADB_CONTAINER = os.environ.get("OPENEMR_CONTAINER",       "openemr_mariadb")
MYSQL_USER        = os.environ.get("OPENEMR_MYSQL_USER",      "openemr")
MYSQL_PASS        = os.environ.get("OPENEMR_MYSQL_PASSWORD",  "openemrpass")
MYSQL_DB          = os.environ.get("OPENEMR_MYSQL_DATABASE",  "openemr")


# ── Phase 1: TrustPulse DB ────────────────────────────────────────────────────

_TP_CLEANUP_CODE = f"""
import sys
sys.path.insert(0, '/app')
from datetime import datetime
from db.session import TrustPulseSession
from db.models import NormalizedEvent, CaseEvent, Case, CaseAction, ContextRequest, UserBaseline

SCENE1_USERS  = {SCENE1_USERS!r}
SCENE1_START  = datetime(2026, 5, 5, 8, 0, 0)
SCENE1_END    = datetime(2026, 5, 5, 14, 0, 0)

db = TrustPulseSession()
try:
    # 1. Find scene1 normalized events by user+date window
    scene1_evs = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id.in_(SCENE1_USERS),
        NormalizedEvent.event_time >= SCENE1_START,
        NormalizedEvent.event_time <  SCENE1_END,
    ).all()
    ev_ids = [e.id for e in scene1_evs]

    # 2. Collect ALL billing_ross cases touching the demo day (not just those linked
    #    to the narrow seed window) so stale cases from background activity are also
    #    removed.
    billing_case_ids = list({{
        c.case_id for c in
        db.query(Case).filter(
            Case.user_id == 'billing_ross',
            Case.date_start >= datetime(2026, 5, 5, 0, 0, 0),
            Case.date_end   <  datetime(2026, 5, 6, 0, 0, 0),
        ).all()
    }})
    # Also include any cases directly linked to the scene1 events
    if ev_ids:
        for ce in db.query(CaseEvent).filter(CaseEvent.normalized_event_id.in_(ev_ids)).all():
            billing_case_ids.append(ce.case_id)
    billing_case_ids = list(set(billing_case_ids))

    # 4. Delete billing_ross case artifacts in dependency order
    if billing_case_ids:
        db.query(ContextRequest).filter(
            ContextRequest.case_id.in_(billing_case_ids)
        ).delete(synchronize_session=False)
        db.query(CaseAction).filter(
            CaseAction.case_id.in_(billing_case_ids)
        ).delete(synchronize_session=False)
        db.query(CaseEvent).filter(
            CaseEvent.case_id.in_(billing_case_ids)
        ).delete(synchronize_session=False)
        db.query(Case).filter(
            Case.case_id.in_(billing_case_ids)
        ).delete(synchronize_session=False)

    # 5. Delete all scene1 normalized events (including background noise)
    if ev_ids:
        db.query(NormalizedEvent).filter(
            NormalizedEvent.id.in_(ev_ids)
        ).delete(synchronize_session=False)

    # 6. Reset billing_ross UserBaseline so the next ingestion uses cold-start thresholds
    #    (threshold=10 unique patients / 20 events).  Without this, an ACTIVE baseline
    #    built from accumulated background activity raises thresholds above what the
    #    55-event demo seed can trigger, causing no case to be generated.
    bl = db.query(UserBaseline).filter(UserBaseline.user_id == 'billing_ross').first()
    baseline_reset = bl is not None
    if bl:
        db.delete(bl)

    db.commit()
    print(f"TrustPulse cleanup: {{len(ev_ids)}} events deleted, "
          f"{{len(billing_case_ids)}} billing_ross case(s) deleted, "
          f"billing_ross baseline reset={{baseline_reset}}.")
except Exception as exc:
    db.rollback()
    print(f"ERROR in TrustPulse cleanup: {{exc}}", file=sys.stderr)
    raise
finally:
    db.close()
"""


def _cleanup_trustpulse() -> None:
    print("  Cleaning TrustPulse DB (inside container)...")
    try:
        subprocess.run(
            ["docker", "exec", TP_CONTAINER, "python", "-c", _TP_CLEANUP_CODE],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  ERROR: TrustPulse cleanup failed (exit {exc.returncode})", file=sys.stderr)
        sys.exit(1)


# ── Phase 2: OpenEMR log ──────────────────────────────────────────────────────

def _cleanup_openemr() -> None:
    print(f"  Deleting OpenEMR log rows with marker '{DEMO_MARKER}'...")
    sql = f"DELETE FROM log WHERE comments='{DEMO_MARKER}';"
    try:
        result = subprocess.run(
            [
                "docker", "exec", MARIADB_CONTAINER,
                "mysql",
                "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
                "-e", sql,
            ],
            capture_output=True, text=True, check=True,
        )
        # MariaDB prints affected rows in the "Rows matched" line when verbose,
        # but with -e it's silent on success.
        # Query rows deleted for summary
        count_result = subprocess.run(
            [
                "docker", "exec", MARIADB_CONTAINER,
                "mysql",
                "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
                "--batch", "--skip-column-names",
                "-e", f"SELECT ROW_COUNT();",
            ],
            capture_output=True, text=True, check=True,
        )
        deleted = count_result.stdout.strip()
        print(f"  OpenEMR cleanup: {deleted} log row(s) deleted.")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or ""
        if "Access denied" in stderr:
            print(
                "  ERROR: Cannot connect to OpenEMR DB. Check OPENEMR_MYSQL_USER/PASSWORD.",
                file=sys.stderr,
            )
        elif "No such container" in stderr or "is not running" in stderr:
            print(
                f"  ERROR: Container '{MARIADB_CONTAINER}' not found. "
                "Is OpenEMR running?",
                file=sys.stderr,
            )
        else:
            print(f"  ERROR: {exc.stderr}", file=sys.stderr)
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 52)
    print("  Scene 1 Reset")
    print("=" * 52)
    print(f"  Marker:              {DEMO_MARKER}")
    print(f"  Scene1 users:        {', '.join(SCENE1_USERS)}")
    print(f"  Date window:         {SCENE1_DATE_START} – {SCENE1_DATE_END}")
    print()

    print("[1/2] Cleaning TrustPulse data...")
    _cleanup_trustpulse()

    print("[2/2] Cleaning OpenEMR source rows...")
    _cleanup_openemr()

    print()
    print("Reset complete.  Scene 1 data removed.")
    print("Next: run  seed_scene1_openemr_activity.py  to re-seed.")


if __name__ == "__main__":
    main()
