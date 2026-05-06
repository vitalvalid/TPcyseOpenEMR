#!/usr/bin/env python3
"""
Scene 2 – Physician After-Hours Patient Access
Idempotent reset script.

Removes prior Scene 2 demo data in two phases:

  Phase 1 – TrustPulse DB cleanup (via docker exec in trustpulse_app)
      Deletes:
        • NormalizedEvents for dr_nguyen on 2026-05-02
        • CaseEvents, CaseActions, ContextRequests for linked dr_nguyen cases
        • The Case records themselves
        • dr_nguyen UserBaseline (so cold-start thresholds apply on next run)

  Phase 2 – OpenEMR log cleanup (via docker exec in openemr_mariadb)
      Deletes rows WHERE comments = 'TRUSTPULSE_SCENE2_PHYSICIAN_AFTER_HOURS_2026_05_02'

Phase 1 runs before Phase 2 so source_log_ids are still in the TrustPulse
DB when we look up which events to delete.

SAFE TO RUN MULTIPLE TIMES – idempotent.
Does NOT touch OpenEMR users, patient_data, or other log rows.
Does NOT wipe unrelated TrustPulse cases or events.

USAGE
-----
  python reset_scene2.py

ENV VARS (optional overrides)
------------------------------
  TRUSTPULSE_CONTAINER   (default: trustpulse_app)
  OPENEMR_CONTAINER      (default: openemr_mariadb)
  OPENEMR_MYSQL_USER     (default: openemr)
  OPENEMR_MYSQL_PASSWORD (default: openemrpass)
  OPENEMR_MYSQL_DATABASE (default: openemr)
"""
import os
import subprocess
import sys

DEMO_MARKER      = "TRUSTPULSE_SCENE2_PHYSICIAN_AFTER_HOURS_2026_05_02"
SCENE2_USER      = "dr_nguyen"
SCENE2_DATE_START = "2026-05-02 21:00:00"
SCENE2_DATE_END   = "2026-05-02 22:00:00"

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

SCENE2_USER  = {SCENE2_USER!r}
SCENE2_START = datetime(2026, 5, 2, 21, 0, 0)
SCENE2_END   = datetime(2026, 5, 2, 22, 0, 0)

db = TrustPulseSession()
try:
    # 1. Find scene2 normalized events by user + date window
    scene2_evs = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == SCENE2_USER,
        NormalizedEvent.event_time >= SCENE2_START,
        NormalizedEvent.event_time <  SCENE2_END,
    ).all()
    ev_ids = [e.id for e in scene2_evs]

    # 2. Collect all dr_nguyen cases on the demo day
    nguyen_case_ids = list({{
        c.case_id for c in
        db.query(Case).filter(
            Case.user_id == SCENE2_USER,
            Case.date_start >= datetime(2026, 5, 2, 0, 0, 0),
            Case.date_end   <  datetime(2026, 5, 3, 0, 0, 0),
        ).all()
    }})
    # Also include any cases directly linked to the scene2 events
    if ev_ids:
        for ce in db.query(CaseEvent).filter(CaseEvent.normalized_event_id.in_(ev_ids)).all():
            nguyen_case_ids.append(ce.case_id)
    nguyen_case_ids = list(set(nguyen_case_ids))

    # 3. Delete dr_nguyen case artifacts in dependency order
    if nguyen_case_ids:
        db.query(ContextRequest).filter(
            ContextRequest.case_id.in_(nguyen_case_ids)
        ).delete(synchronize_session=False)
        db.query(CaseAction).filter(
            CaseAction.case_id.in_(nguyen_case_ids)
        ).delete(synchronize_session=False)
        db.query(CaseEvent).filter(
            CaseEvent.case_id.in_(nguyen_case_ids)
        ).delete(synchronize_session=False)
        db.query(Case).filter(
            Case.case_id.in_(nguyen_case_ids)
        ).delete(synchronize_session=False)

    # 4. Delete scene2 normalized events
    if ev_ids:
        db.query(NormalizedEvent).filter(
            NormalizedEvent.id.in_(ev_ids)
        ).delete(synchronize_session=False)

    # 5. Reset dr_nguyen UserBaseline so cold-start thresholds apply on next run
    bl = db.query(UserBaseline).filter(UserBaseline.user_id == SCENE2_USER).first()
    baseline_reset = bl is not None
    if bl:
        db.delete(bl)

    db.commit()
    print(f"TrustPulse cleanup: {{len(ev_ids)}} events deleted, "
          f"{{len(nguyen_case_ids)}} dr_nguyen case(s) deleted, "
          f"dr_nguyen baseline reset={{baseline_reset}}.")
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
        subprocess.run(
            [
                "docker", "exec", MARIADB_CONTAINER,
                "mysql",
                "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
                "-e", sql,
            ],
            capture_output=True, text=True, check=True,
        )
        count_result = subprocess.run(
            [
                "docker", "exec", MARIADB_CONTAINER,
                "mysql",
                "-u", MYSQL_USER, f"-p{MYSQL_PASS}", MYSQL_DB,
                "--batch", "--skip-column-names",
                "-e", "SELECT ROW_COUNT();",
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
    print("=" * 56)
    print("  Scene 2 Reset – Physician After-Hours Access")
    print("=" * 56)
    print(f"  Marker:      {DEMO_MARKER}")
    print(f"  Actor:       {SCENE2_USER}  (Michael Nguyen)")
    print(f"  Date window: {SCENE2_DATE_START} – {SCENE2_DATE_END}")
    print()

    print("[1/2] Cleaning TrustPulse data...")
    _cleanup_trustpulse()

    print("[2/2] Cleaning OpenEMR source rows...")
    _cleanup_openemr()

    print()
    print("Reset complete.  Scene 2 data removed.")
    print("Next: run  seed_scene2_physician_after_hours.py  to re-seed.")


if __name__ == "__main__":
    main()
