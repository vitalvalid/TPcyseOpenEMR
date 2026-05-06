#!/usr/bin/env bash
# Scene 2 – Physician After-Hours Patient Access
# Runs the ingestion and case-generation pipeline inside the trustpulse_app container.
#
# USAGE
#   ./run_scene2_ingestion.sh
#
# ENV VARS (optional overrides)
#   TRUSTPULSE_CONTAINER  (default: trustpulse_app)
#
# EXIT CODES
#   0  success – events ingested and case generated
#   1  ingestion failure or container not found

set -euo pipefail

TP_CONTAINER="${TRUSTPULSE_CONTAINER:-trustpulse_app}"

echo "===================================================="
echo "  Scene 2 Ingestion & Case Generation"
echo "  Physician After-Hours Patient Access"
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
# Force-scores dr_nguyen events so the after-hours rules (R-01, R-03) fire
# reliably regardless of what the background poller already scored.
#
# Expected scoring per event (cold-start baseline, hour=21, Saturday):
#   R-01 After-Hours Access : 20.0  (hour=21 >= BUSINESS_HOURS_END=19)
#   R-03 Weekend Access     : 15.0  (day_of_week=5)
#   Total per event         : 35.0  → P2_MEDIUM

PIPELINE_CODE='
import sys, json
sys.path.insert(0, "/app")

from db.session import TrustPulseSession, init_db
from api.ingestion import run_ingestion_cycle
from engine.baseline import compute_baselines, save_baselines
from engine.case_engine import generate_cases, generate_auth_cases, _stable_case_id
from engine.compliance import save_trust_scores
from engine.scorer import compute_risk_score
from db.models import NormalizedEvent, UserBaseline, Case
from datetime import datetime, date as ddate

SCENE2_USER  = "dr_nguyen"
SCENE2_START = datetime(2026, 5, 2, 21, 0, 0)
SCENE2_END   = datetime(2026, 5, 2, 22, 0, 0)

init_db()

# ── Phase 1: ingest any new events ────────────────────────────────────────────
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

# ── Phase 2: force-score scene2 events ────────────────────────────────────────
# Delete any stale dr_nguyen baseline so cold-start thresholds apply.
# hour=21 is after-hours (BUSINESS_HOURS_END=19) → R-01 fires (20 pts).
# day_of_week=5 is Saturday → R-03 fires (15 pts). Total = 35 → P2_MEDIUM.
print("  [2/4] Force-scoring scene2 dr_nguyen events...", flush=True)
db = TrustPulseSession()
try:
    bl = db.query(UserBaseline).filter(UserBaseline.user_id == SCENE2_USER).first()
    if bl:
        db.delete(bl)
        db.flush()

    scene2_evs = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == SCENE2_USER,
        NormalizedEvent.event_time >= SCENE2_START,
        NormalizedEvent.event_time <  SCENE2_END,
    ).all()

    if not scene2_evs:
        print("  WARNING: No dr_nguyen scene2 events in TrustPulse. "
              "Seed may not have run yet.", flush=True)
    else:
        # Score in isolation: 5 unique patients, 5 events total, no failed logins
        daily_pts = len({e.patient_id for e in scene2_evs if e.patient_id})
        daily_ct  = len(scene2_evs)
        for ev in scene2_evs:
            ed = {
                "hour_of_day": ev.event_time.hour,
                "day_of_week": ev.event_time.weekday(),
                "event_type":  ev.event_type or "",
                "patient_id":  ev.patient_id,
                "department":  ev.department,
                "ip_address":  ev.ip_address,
                "user_role":   ev.user_role or "",
            }
            ctx = {
                "daily_unique_patients":           daily_pts,
                "daily_access_count":              daily_ct,
                "recent_failed_logins":            0,
                "patient_is_vip":                  False,
                "has_appointment":                 None,
                "modify_then_export_within_5min":  False,
                "user_department":                 ev.department,
            }
            # None = cold-start baseline → R-01 and R-03 fire on hour+DOW alone
            s, l, fr = compute_risk_score(ed, None, ctx)
            ev.risk_score      = s
            ev.risk_level      = l
            ev.triggered_rules = fr
        db.commit()
        top = max(e.risk_score for e in scene2_evs)
        print(f"  Force-score: {len(scene2_evs)} events, top={top:.1f}, "
              f"unique_pts={daily_pts}, total={daily_ct}", flush=True)

    # ── Phase 3: generate cases ────────────────────────────────────────────────
    print("  [3/4] Generating cases...", flush=True)
    generate_cases(db)
    generate_auth_cases(db)

    # Suppress any stale OFF_HOURS cases for dr_nguyen on 2026-05-02 that
    # might have been created by the background poller from background activity
    # (e.g. dr_nguyen logging into OpenEMR outside the seed window).
    day_offset  = (ddate(2026, 5, 2) - ddate(2026, 1, 1)).days
    week_bucket = day_offset // 7
    scene2_case_id = _stable_case_id(SCENE2_USER, "OFF_HOURS", week_bucket)

    stale_cases = db.query(Case).filter(
        Case.user_id == SCENE2_USER,
        Case.case_id != scene2_case_id,
        Case.date_start >= datetime(2026, 5, 2, 0, 0, 0),
        Case.date_start <  datetime(2026, 5, 3, 0, 0, 0),
        Case.status.notin_(["RESOLVED", "FALSE_POSITIVE"]),
    ).all()
    for sc in stale_cases:
        sc.status = "FALSE_POSITIVE"
        print(f"  Suppressed stale case {sc.case_id[:12]} "
              f"(type={sc.case_type}, pattern={sc.pattern_type}) as FALSE_POSITIVE.",
              flush=True)
    if stale_cases:
        db.commit()

    # ── Phase 4: baselines + trust scores ─────────────────────────────────────
    print("  [4/4] Computing baselines and saving trust scores...", flush=True)
    baselines = compute_baselines(db)
    save_baselines(db, baselines)
    save_trust_scores(db)
    db.commit()
    print("  Case generation complete.", flush=True)

except Exception as exc:
    db.rollback()
    print(f"ERROR: {exc}", file=sys.stderr, flush=True)
    import traceback; traceback.print_exc()
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

# ── Print summary ─────────────────────────────────────────────────────────────

SUMMARY_CODE='
import sys
sys.path.insert(0, "/app")
from db.session import TrustPulseSession
from db.models import NormalizedEvent, Case, IngestionManifest
from datetime import datetime

db = TrustPulseSession()
try:
    scene2_events = db.query(NormalizedEvent).filter(
        NormalizedEvent.user_id == "dr_nguyen",
        NormalizedEvent.event_time >= datetime(2026, 5, 2, 21, 0, 0),
        NormalizedEvent.event_time <  datetime(2026, 5, 2, 22, 0, 0),
    ).all()

    nguyen_cases = db.query(Case).filter(
        Case.user_id == "dr_nguyen",
        Case.date_start >= datetime(2026, 5, 2, 0, 0, 0),
        Case.date_end   <= datetime(2026, 5, 3, 0, 0, 0),
        Case.status.notin_(["FALSE_POSITIVE", "SUPPRESSED"]),
    ).all()

    last_manifest = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS")
        .order_by(IngestionManifest.completed_at.desc())
        .first()
    )

    print("")
    print("=" * 56)
    print("  INGESTION SUMMARY – Scene 2")
    print("=" * 56)
    print(f"  Scene2 NormalizedEvents (dr_nguyen): {len(scene2_events)}")
    for ev in scene2_events:
        print(f"    {ev.event_time}  score={ev.risk_score:.1f}  pid={ev.patient_id}")
    print(f"  dr_nguyen cases on 2026-05-02:       {len(nguyen_cases)}")
    for c in nguyen_cases:
        uid = len(set(e.patient_id for e in scene2_events if e.patient_id))
        print(f"    case_id={c.case_id[:12]}...  type={c.case_type}"
              f"  sev={c.severity}  events={c.event_count}  pattern={c.pattern_type}")
    if last_manifest:
        mstatus = last_manifest.telemetry_status_label or last_manifest.telemetry_status
        mhash   = (last_manifest.manifest_hash or "")[:12]
        print(f"  Last manifest status: {mstatus}")
        print(f"  Manifest hash (12):   {mhash}")
    print("=" * 56)
finally:
    db.close()
'

echo ""
echo "[2/2] Collecting summary..."
docker exec "${TP_CONTAINER}" python -c "${SUMMARY_CODE}"

echo ""
echo "Next: run  verify_scene2.py  to validate the expected case."
