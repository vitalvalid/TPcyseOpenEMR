#!/usr/bin/env bash
# Scene 1 – Billing High-Volume Access Review
# Runs the ingestion and case-generation pipeline inside the trustpulse_app container.
#
# USAGE
#   ./run_scene1_ingestion.sh
#
# ENV VARS (optional overrides)
#   TRUSTPULSE_CONTAINER  (default: trustpulse_app)
#
# EXIT CODES
#   0  success – events ingested and cases generated
#   1  ingestion failure or container not found

set -euo pipefail

TP_CONTAINER="${TRUSTPULSE_CONTAINER:-trustpulse_app}"

echo "===================================================="
echo "  Scene 1 Ingestion & Case Generation"
echo "===================================================="
echo "  Container: ${TP_CONTAINER}"
echo ""

# ── Check container is running ────────────────────────────────────────────────

if ! docker inspect --format '{{.State.Running}}' "${TP_CONTAINER}" 2>/dev/null | grep -q true; then
    echo "ERROR: Container '${TP_CONTAINER}' is not running." >&2
    echo "       Start TrustPulse with: docker compose up -d" >&2
    exit 1
fi

# ── Run ingestion + case generation inside container ─────────────────────────

PIPELINE_CODE='
import sys, json
sys.path.insert(0, "/app")

from db.session import TrustPulseSession, init_db
from api.ingestion import run_ingestion_cycle
from engine.baseline import compute_baselines, save_baselines
from engine.case_engine import generate_cases, generate_auth_cases
from engine.compliance import save_trust_scores
from engine.scorer import compute_risk_score
from db.models import NormalizedEvent, UserBaseline
from datetime import datetime

SCENE1_USER  = "billing_ross"
SCENE1_START = datetime(2026, 5, 5, 10, 0, 0)
SCENE1_END   = datetime(2026, 5, 5, 10, 46, 0)

init_db()

# ── Phase 1: ingest any new events ────────────────────────────────────────────
# The background poller may have already ingested some or all of the seed events.
# If it did, run_ingestion_cycle will either return 0 events or raise a duplicate
# key error.  Either outcome is fine — we just need the events in the DB.
print("  [1/4] Running ingestion cycle...", flush=True)
db = TrustPulseSession()
try:
    result = run_ingestion_cycle(db)
    print(json.dumps(result, indent=2, default=str), flush=True)
except Exception as exc:
    db.rollback()
    print(f"  Ingestion cycle note (background poller may have handled it): {exc}", flush=True)
    result = {"events_ingested": 0}
finally:
    db.close()

# ── Phase 2: force-score scene1 events and generate the case ──────────────────
# Use a fresh session so any prior transaction errors are fully cleared.
# Force cold-start scoring for billing_ross scene1 events regardless of whether
# the background poller already scored them with an inflated ACTIVE baseline.
print(f"  [2/4] Force-scoring scene1 billing_ross events...", flush=True)
db = TrustPulseSession()
try:
    # Delete stale billing_ross baseline so cold thresholds (10 patients / 20 events)
    # are used.  An ACTIVE baseline built from months of background activity would
    # raise the R-02 threshold to ~38 and R-08 to ~578, both exceeding the 55-event
    # seed, so no anomaly would be detected without this reset.
    bl = db.query(UserBaseline).filter(UserBaseline.user_id == SCENE1_USER).first()
    if bl:
        db.delete(bl)
        db.flush()

    scene1_evs = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == SCENE1_USER,
        NormalizedEvent.event_time >= SCENE1_START,
        NormalizedEvent.event_time <  SCENE1_END,
    ).all()

    if not scene1_evs:
        print("  WARNING: No billing_ross scene1 events in TrustPulse. "
              "Seed may not have run.", flush=True)
    else:
        # Score using scene1 window context only (28 unique patients, 55 events).
        # This isolates the anomaly window from background activity on the same day.
        daily_pts = len({e.patient_id for e in scene1_evs if e.patient_id})
        daily_ct  = len(scene1_evs)
        for ev in scene1_evs:
            ed = {
                "hour_of_day": ev.hour_of_day, "day_of_week": ev.day_of_week,
                "event_type": ev.event_type or "", "patient_id": ev.patient_id,
                "department": ev.department, "ip_address": ev.ip_address,
                "user_role": ev.user_role or "",
            }
            ctx = {
                "daily_unique_patients": daily_pts,
                "daily_access_count": daily_ct,
                "recent_failed_logins": 0, "patient_is_vip": False,
                "has_appointment": None,
                "modify_then_export_within_5min": False,
                "user_department": ev.department,
            }
            s, l, fr = compute_risk_score(ed, None, ctx)  # None = cold-start baseline
            ev.risk_score = s; ev.risk_level = l; ev.triggered_rules = fr
        db.commit()
        top = max(e.risk_score for e in scene1_evs)
        print(f"  Force-score: {len(scene1_evs)} events, top={top:.1f}, "
              f"pts={daily_pts}, ct={daily_ct}", flush=True)

    # Zero out background billing_ross events on 2026-05-05 that are OUTSIDE the
    # scene1 window (hour=18-19 background activity).  If left with non-zero scores
    # from a previous background-poller cycle, generate_cases would create a stale
    # OFF_HOURS case that clutters the demo UI.
    outside_evs = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == SCENE1_USER,
        NormalizedEvent.event_time >= datetime(2026, 5, 5, 0, 0, 0),
        NormalizedEvent.event_time <  datetime(2026, 5, 6, 0, 0, 0),
        NormalizedEvent.event_time <  SCENE1_START,
    ).all()
    outside_evs += db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == SCENE1_USER,
        NormalizedEvent.event_time >= SCENE1_END,
        NormalizedEvent.event_time <  datetime(2026, 5, 6, 0, 0, 0),
    ).all()
    zeroed = 0
    for ev in outside_evs:
        if ev.risk_score > 0:
            ev.risk_score = 0.0; ev.risk_level = "LOW"; ev.triggered_rules = []
            zeroed += 1
    if zeroed:
        db.commit()
        print(f"  Zeroed {zeroed} outside-window billing_ross events to suppress "
              "stale cases.", flush=True)

    print(f"  [3/4] Generating cases...", flush=True)
    generate_cases(db)
    generate_auth_cases(db)

    # Block stale after-hours cases from being (re)created when billing_ross
    # logs into OpenEMR during the demo.  We do two things:
    #   1. Mark any existing non-scene1 billing_ross cases on the demo day as
    #      FALSE_POSITIVE (handles cases that already exist).
    #   2. Pre-create a FALSE_POSITIVE sentinel for the OFF_HOURS case slot
    #      (blocks the background poller from creating it even if it does not
    #      exist yet at setup time).
    # generate_cases skips cases whose status is RESOLVED or FALSE_POSITIVE.
    from db.models import Case as CaseModel
    from engine.case_engine import _stable_case_id
    from datetime import date as ddate
    day_offset    = (SCENE1_START.date() - ddate(2026, 1, 1)).days
    week_bucket   = day_offset // 7
    scene1_case_id   = _stable_case_id(SCENE1_USER, "VOLUME_SPIKE", week_bucket)
    off_hours_case_id = _stable_case_id(SCENE1_USER, "OFF_HOURS",    week_bucket)

    # Mark any existing stale cases
    stale_cases = db.query(CaseModel).filter(
        CaseModel.user_id == SCENE1_USER,
        CaseModel.case_id != scene1_case_id,
        CaseModel.date_start >= datetime(2026, 5, 5, 0, 0, 0),
        CaseModel.date_start <  datetime(2026, 5, 6, 0, 0, 0),
        CaseModel.status.notin_(["RESOLVED", "FALSE_POSITIVE"]),
    ).all()
    for sc in stale_cases:
        sc.status = "FALSE_POSITIVE"
        print(f"  Suppressed stale case {sc.case_id[:12]} "
              f"(type={sc.case_type}, events={sc.event_count}) as FALSE_POSITIVE.",
              flush=True)
    if stale_cases:
        db.commit()

    # Pre-create sentinel for OFF_HOURS slot if it does not exist yet
    sentinel = db.get(CaseModel, off_hours_case_id)
    if not sentinel:
        sentinel = CaseModel(
            case_id=off_hours_case_id,
            title=f"After-Hours Access - {SCENE1_USER} [demo-blocked]",
            severity="P3_LOW", pattern_type="OFF_HOURS",
            case_type="PATIENT_ACCESS_REVIEW",
            user_id=SCENE1_USER, user_name=SCENE1_USER,
            event_count=0,
            date_start=SCENE1_START, date_end=SCENE1_END,
            risk_score=0.0, recommended_action="",
            breach_risk=False, breach_deadline=None,
            status="FALSE_POSITIVE", hipaa_provisions=[],
            created_at=datetime.utcnow(),
        )
        db.add(sentinel)
        db.commit()
        print(f"  Created OFF_HOURS sentinel case {off_hours_case_id[:12]} "
              "as FALSE_POSITIVE — future live logins will not recreate it.",
              flush=True)
    elif sentinel.status not in ("RESOLVED", "FALSE_POSITIVE"):
        sentinel.status = "FALSE_POSITIVE"
        db.commit()
        print(f"  Marked existing OFF_HOURS case {off_hours_case_id[:12]} as FALSE_POSITIVE.",
              flush=True)

    print(f"  [4/4] Computing baselines and saving trust scores...", flush=True)
    baselines = compute_baselines(db)
    save_baselines(db, baselines)
    save_trust_scores(db)
    db.commit()
    print("  Case generation complete.", flush=True)

except Exception as exc:
    db.rollback()
    print(f"ERROR: {exc}", file=sys.stderr, flush=True)
    sys.exit(1)
finally:
    db.close()
'

echo "[1/2] Running ingestion pipeline..."
if ! docker exec "${TP_CONTAINER}" python -c "${PIPELINE_CODE}"; then
    echo "" >&2
    echo "ERROR: Ingestion pipeline failed." >&2
    exit 1
fi

# ── Print telemetry/case summary ──────────────────────────────────────────────

SUMMARY_CODE='
import sys, json
sys.path.insert(0, "/app")

from db.session import TrustPulseSession
from db.models import NormalizedEvent, Case, IngestionManifest

db = TrustPulseSession()
try:
    from datetime import datetime
    scene1_events = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == "billing_ross",
        NormalizedEvent.event_time >= datetime(2026, 5, 5, 8, 0, 0),
        NormalizedEvent.event_time <  datetime(2026, 5, 5, 14, 0, 0),
    ).count()

    billing_cases = db.query(Case).filter(
        Case.user_id == "billing_ross",
        Case.date_start >= datetime(2026, 5, 5, 0, 0, 0),
        Case.date_end   <= datetime(2026, 5, 6, 0, 0, 0),
        Case.status.notin_(["FALSE_POSITIVE", "SUPPRESSED"]),
    ).all()

    last_manifest = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS")
        .order_by(IngestionManifest.completed_at.desc())
        .first()
    )

    print("")
    print("=" * 52)
    print("  INGESTION SUMMARY")
    print("=" * 52)
    print(f"  Scene1 NormalizedEvents (billing_ross):  {scene1_events}")
    print(f"  billing_ross cases on 2026-05-05:        {len(billing_cases)}")
    for c in billing_cases:
        print(f"    case_id={c.case_id[:12]}...  type={c.case_type}  sev={c.severity}  events={c.event_count}")
    if last_manifest:
        print(f"  Last manifest telemetry: {last_manifest.telemetry_status_label or last_manifest.telemetry_status}")
        mhash = (last_manifest.manifest_hash or "")[:12]
        print(f"  Manifest hash (12):      {mhash}")
    print("=" * 52)
finally:
    db.close()
'

echo ""
echo "[2/2] Collecting summary..."
docker exec "${TP_CONTAINER}" python -c "${SUMMARY_CODE}"

echo ""
echo "Next: run  verify_scene1.py  to validate the expected case."
