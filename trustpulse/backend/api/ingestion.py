"""
Ingestion API - orchestrates manifest creation, event fetch, normalization.
Implements hash-chained IngestionManifest for provenance.
"""
import hashlib
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from db.models import IngestionManifest, RawAuditEvent, IngestionError, NormalizedEvent
from db.session import get_tp_session
from ingestion.log_reader import fetch_new_logs, get_source_batch_hash
from ingestion.normalizer import normalize_and_score
from ingestion.openemr_schema import inspect_schema
from db.session import get_openemr_engine
from telemetry_health import summarize_manifest_telemetry
from api.auth import require_permission

router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])
log    = logging.getLogger("trustpulse.ingestion")


# ── Manifest hash chain ───────────────────────────────────────────────────────

def _get_previous_manifest_hash(db: Session) -> str:
    last = (
        db.query(IngestionManifest.manifest_hash)
        .filter(IngestionManifest.status == "SUCCESS",
                IngestionManifest.manifest_hash.isnot(None))
        .order_by(IngestionManifest.completed_at.desc())
        .first()
    )
    return last[0] if last else "0" * 64


def _compute_manifest_hash(m: IngestionManifest, previous_hash: str) -> str:
    canonical = json.dumps({
        "connector_name":          m.connector_name,
        "source_system":           m.source_system,
        "source_min_id":           m.source_min_id,
        "source_max_id":           m.source_max_id,
        "source_row_count":        m.source_row_count,
        "inserted_count":          m.inserted_count,
        "duplicate_count":         m.duplicate_count,
        "parse_error_count":       m.parse_error_count,
        "source_batch_sha256":     m.source_batch_sha256,
        "normalized_batch_sha256": m.normalized_batch_sha256,
        "started_at":              m.started_at.isoformat(),
        "previous_manifest_hash":  previous_hash,
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _compute_normalized_batch_hash(events: list) -> str:
    ids = sorted(str(e.source_log_id) for e in events)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


def _detect_gaps(last_id: int, source_ids: list, *, is_first_ingestion: bool = False) -> tuple:
    """Detect gaps between last ingested ID and new batch, and within the batch."""
    if not source_ids:
        return False, []
    sorted_ids = sorted(source_ids)
    gaps = []
    first = sorted_ids[0]
    if not is_first_ingestion and first > last_id + 1:
        gaps.append({"from": last_id + 1, "to": first - 1})
    for i in range(1, len(sorted_ids)):
        if sorted_ids[i] - sorted_ids[i - 1] > 1:
            gaps.append({"from": sorted_ids[i - 1] + 1, "to": sorted_ids[i] - 1})
    return bool(gaps), gaps


# ── Core ingestion cycle ──────────────────────────────────────────────────────

def run_ingestion_cycle(db: Session) -> dict:
    last = (
        db.query(NormalizedEvent.source_log_id)
        .order_by(NormalizedEvent.source_log_id.desc())
        .first()
    )
    last_id = last[0] if last else 0
    is_first_ingestion = last is None

    previous_hash = _get_previous_manifest_hash(db)

    manifest = IngestionManifest(
        connector_name        = "openemr_real",
        source_system         = "openemr",
        source_name           = "openemr_log+api_log",
        started_at            = datetime.utcnow(),
        status                = "IN_PROGRESS",
        previous_manifest_hash = previous_hash,
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)

    # Legacy IngestionRun record for backwards compat
    from db.models import IngestionRun
    run = IngestionRun(run_at=datetime.utcnow(), status="IN_PROGRESS")
    db.add(run)
    db.commit()

    duplicate_ct = 0

    try:
        schema_info = inspect_schema(get_openemr_engine())
        raw_rows, parse_errors_list, fetch_meta = fetch_new_logs(last_id)

        if not raw_rows:
            telemetry = summarize_manifest_telemetry(
                manifest,
                schema_info=schema_info,
                source_reachable=schema_info.get("connected", False),
            )
            manifest.source_row_count = 0
            manifest.source_rows_seen = 0
            manifest.inserted_count   = 0
            manifest.rows_inserted    = 0
            manifest.duplicate_count  = 0
            manifest.parse_error_count = 0
            manifest.source_batch_sha256 = hashlib.sha256(b"").hexdigest()
            manifest.normalized_batch_sha256 = hashlib.sha256(b"").hexdigest()
            manifest.gap_detected     = False
            manifest.raw_source_gap_detected = False
            manifest.coverage_warning = False
            manifest.telemetry_status = telemetry["status"]
            manifest.telemetry_status_label = telemetry["label"]
            manifest.telemetry_status_description = telemetry["description"]
            manifest.status           = "SUCCESS"
            manifest.completed_at     = datetime.utcnow()
            manifest.manifest_hash    = _compute_manifest_hash(manifest, previous_hash)

            run.events_ingested = 0
            run.events_scored   = 0
            run.highest_risk    = 0.0
            run.status          = "SUCCESS"
            db.commit()
            return {"run_id": run.id, "events_ingested": 0, "events_scored": 0,
                    "highest_risk": 0.0, "status": "SUCCESS",
                    "manifest_hash": manifest.manifest_hash}

        # Store raw audit events (governance metadata only, no clinical content)
        for r in raw_rows:
            db.add(RawAuditEvent(
                manifest_id             = manifest.id,
                source_system           = "openemr",
                connector_name          = "openemr_real",
                source_log_id           = str(r["source_log_id"]),
                event_time              = r["event_time"],
                source_payload_hash     = r["source_payload_hash"],
                source_payload_minimized = r["raw_payload_minimized"],
            ))

        # Record parse errors in IngestionError table
        for err in parse_errors_list:
            db.add(IngestionError(
                manifest_id           = manifest.id,
                source_log_id         = err["source_log_id"],
                error_type            = err["error_type"],
                error_message         = err["error_message"],
                raw_payload_minimized = err["raw_payload_minimized"],
            ))

        existing_ids = {row[0] for row in db.query(NormalizedEvent.source_log_id).all()}
        source_ids   = [r["source_log_id"] for r in raw_rows]
        duplicate_ct = sum(1 for sid in source_ids if sid in existing_ids)

        raw_gap_detected, gaps = _detect_gaps(last_id, source_ids, is_first_ingestion=is_first_ingestion)
        source_batch_hash  = get_source_batch_hash(raw_rows)

        new_events = normalize_and_score(raw_rows, db, manifest_id=manifest.id)
        normalized_hash = _compute_normalized_batch_hash(new_events)
        coverage_warning = any([
            (fetch_meta.get("rows_excluded_by_policy", 0) or 0) > 0,
            (fetch_meta.get("rows_unsupported_event_type", 0) or 0) > 0,
            (fetch_meta.get("rows_missing_required_fields", 0) or 0) > 0,
        ])
        coverage_reasons = []
        if fetch_meta.get("rows_excluded_system_admin", 0):
            coverage_reasons.append(f"{fetch_meta['rows_excluded_system_admin']} admin/system rows were excluded by monitoring policy.")
        if fetch_meta.get("rows_unsupported_event_type", 0):
            coverage_reasons.append(f"{fetch_meta['rows_unsupported_event_type']} rows used unsupported event types.")
        if fetch_meta.get("rows_missing_required_fields", 0):
            coverage_reasons.append(f"{fetch_meta['rows_missing_required_fields']} rows were missing required fields.")

        manifest.source_row_count        = len(raw_rows)
        manifest.source_rows_seen        = fetch_meta.get("source_rows_seen", len(raw_rows))
        manifest.rows_inserted           = len(new_events)
        manifest.source_min_id           = min(source_ids)
        manifest.source_max_id           = max(source_ids)
        manifest.inserted_count          = len(new_events)
        manifest.duplicate_count         = duplicate_ct
        manifest.rows_excluded_by_policy = fetch_meta.get("rows_excluded_by_policy", 0)
        manifest.rows_excluded_system_admin = fetch_meta.get("rows_excluded_system_admin", 0)
        manifest.rows_unsupported_event_type = fetch_meta.get("rows_unsupported_event_type", 0)
        manifest.rows_missing_required_fields = fetch_meta.get("rows_missing_required_fields", 0)
        manifest.parse_error_count       = len(parse_errors_list)
        manifest.source_batch_sha256     = source_batch_hash
        manifest.normalized_batch_sha256 = normalized_hash
        manifest.gap_detected            = raw_gap_detected
        manifest.gap_ranges_json         = gaps if gaps else None
        manifest.raw_source_gap_detected = raw_gap_detected
        manifest.raw_source_gap_ranges_json = gaps if gaps else None
        manifest.coverage_warning        = coverage_warning
        manifest.coverage_warning_reason = " ".join(coverage_reasons) if coverage_reasons else None
        manifest.last_source_id_seen     = fetch_meta.get("last_source_id_seen")
        manifest.last_source_id_ingested = fetch_meta.get("last_source_id_ingested")
        manifest.status                  = "SUCCESS"
        manifest.completed_at            = datetime.utcnow()
        telemetry = summarize_manifest_telemetry(
            manifest,
            schema_info=schema_info,
            source_reachable=schema_info.get("connected", False),
        )
        manifest.telemetry_status = telemetry["status"]
        manifest.telemetry_status_label = telemetry["label"]
        manifest.telemetry_status_description = telemetry["description"]
        manifest.manifest_hash           = _compute_manifest_hash(manifest, previous_hash)

        highest = max((e.risk_score for e in new_events), default=0.0)
        run.events_ingested = len(raw_rows)
        run.events_scored   = len(new_events)
        run.highest_risk    = highest
        run.status          = "SUCCESS"
        db.commit()

        return {
            "run_id":          run.id,
            "manifest_id":     manifest.id,
            "events_ingested": len(raw_rows),
            "events_scored":   len(new_events),
            "highest_risk":    highest,
            "status":          "SUCCESS",
            "manifest_hash":   manifest.manifest_hash,
            "source_hash":     source_batch_hash,
            "gap_detected":    raw_gap_detected,
            "telemetry_status": manifest.telemetry_status,
        }

    except Exception as exc:
        schema_info = inspect_schema(get_openemr_engine())
        manifest.status        = "FAILED"
        manifest.error_message = str(exc)
        manifest.completed_at  = datetime.utcnow()
        telemetry = summarize_manifest_telemetry(
            manifest,
            schema_info=schema_info,
            source_reachable=schema_info.get("connected", False),
        )
        manifest.telemetry_status = telemetry["status"]
        manifest.telemetry_status_label = telemetry["label"]
        manifest.telemetry_status_description = telemetry["description"]
        run.status             = "FAILED"
        run.error_message      = str(exc)
        db.commit()
        log.error("Ingestion cycle failed: %s", exc)
        raise


# ── Hash-chain verification ───────────────────────────────────────────────────

@router.get("/verify")
def verify_ingestion_chain(
    db: Session = Depends(get_tp_session),
    _user=Depends(require_permission("review")),
):
    """
    Walk all SUCCESS manifests in chronological order and re-derive each
    manifest_hash from its stored fields.  Returns per-manifest pass/fail
    and an overall integrity verdict.
    """
    manifests = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS",
                IngestionManifest.manifest_hash.isnot(None))
        .order_by(IngestionManifest.completed_at.asc())
        .all()
    )

    if not manifests:
        return {"overall": "NO_DATA", "verified": 0, "failed": 0, "results": []}

    results = []
    prev_hash = "0" * 64
    failed = 0

    for m in manifests:
        expected = _compute_manifest_hash(m, m.previous_manifest_hash or prev_hash)
        ok = expected == m.manifest_hash
        if not ok:
            failed += 1
        results.append({
            "manifest_id":      m.id,
            "started_at":       m.started_at.isoformat(),
            "source_row_count": m.source_row_count,
            "inserted_count":   m.inserted_count,
            "stored_hash":      m.manifest_hash,
            "computed_hash":    expected,
            "integrity":        "PASS" if ok else "FAIL - hash mismatch",
        })
        prev_hash = m.manifest_hash

    return {
        "overall":  "PASS" if failed == 0 else "FAIL",
        "verified": len(manifests),
        "failed":   failed,
        "results":  results,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/run")
def trigger_ingestion(
    db: Session = Depends(get_tp_session),
    _user=Depends(require_permission("trigger_ingestion")),
):
    return run_ingestion_cycle(db)


@router.get("/status")
def ingestion_status(
    db: Session = Depends(get_tp_session),
    _user=Depends(require_permission("review")),
):
    manifests = (
        db.query(IngestionManifest)
        .order_by(IngestionManifest.started_at.desc())
        .limit(10)
        .all()
    )

    total_events   = db.query(NormalizedEvent).count()
    total_errors   = (
        db.query(IngestionError)
        .count()
    )
    last_success   = next((m for m in manifests if m.status == "SUCCESS"), None)
    gap_manifests  = [m for m in manifests if m.gap_detected]
    parser_errors  = sum(m.parse_error_count or 0 for m in manifests)
    schema_info = inspect_schema(get_openemr_engine())
    latest = manifests[0] if manifests else None
    telemetry = summarize_manifest_telemetry(
        latest,
        schema_info=schema_info,
        source_reachable=schema_info.get("connected", False),
    )

    return {
        "overall_status":     telemetry["status"],
        "overall_status_label": telemetry["label"],
        "overall_status_description": telemetry["description"],
        "total_events_stored": total_events,
        "total_parse_errors":  total_errors,
        "last_success_hash":  last_success.manifest_hash if last_success else None,
        "last_source_hash":   last_success.source_batch_sha256 if last_success else None,
        "manifests": [
            {
                "id":                  m.id,
                "started_at":          m.started_at.isoformat(),
                "completed_at":        m.completed_at.isoformat() if m.completed_at else None,
                "status":              m.status,
                "source_row_count":    m.source_row_count,
                "source_rows_seen":    m.source_rows_seen,
                "inserted_count":      m.inserted_count,
                "rows_inserted":       m.rows_inserted,
                "duplicate_count":     m.duplicate_count,
                "rows_excluded_by_policy": m.rows_excluded_by_policy,
                "rows_excluded_system_admin": m.rows_excluded_system_admin,
                "rows_unsupported_event_type": m.rows_unsupported_event_type,
                "rows_missing_required_fields": m.rows_missing_required_fields,
                "parse_error_count":   m.parse_error_count,
                "gap_detected":        m.gap_detected,
                "gap_ranges":          m.gap_ranges_json,
                "raw_source_gap_detected": m.raw_source_gap_detected,
                "raw_source_gap_ranges": m.raw_source_gap_ranges_json,
                "coverage_warning":    m.coverage_warning,
                "coverage_warning_reason": m.coverage_warning_reason,
                "last_source_id_seen": m.last_source_id_seen,
                "last_source_id_ingested": m.last_source_id_ingested,
                "telemetry_status":    m.telemetry_status,
                "telemetry_status_label": m.telemetry_status_label,
                "telemetry_status_description": m.telemetry_status_description,
                "source_batch_sha256": m.source_batch_sha256,
                "normalized_batch_sha256": m.normalized_batch_sha256,
                "manifest_hash":       m.manifest_hash,
                "error_message":       m.error_message,
            }
            for m in manifests
        ],
    }
