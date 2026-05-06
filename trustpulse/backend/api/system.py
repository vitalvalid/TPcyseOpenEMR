"""
System / connectivity endpoints.
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import NormalizedEvent, IngestionManifest
from db.session import get_tp_session, get_openemr_engine, TRUSTPULSE_MODE
from ingestion.openemr_schema import inspect_schema
from telemetry_health import summarize_manifest_telemetry
from api.auth import get_current_user, TrustPulseUser

router = APIRouter(prefix="/api/system", tags=["system"])

DEMO_LAB_MODE = os.environ.get("TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE", "").lower() == "true"


def history_maturity_label(history_days: int) -> str:
    """Map days of available audit history to a human-readable baseline maturity label."""
    if history_days < 7:   return "Cold Start"
    if history_days < 14:  return "Preliminary"
    if history_days < 30:  return "Partial"
    return "Stable"


@router.get("/openemr-schema")
def openemr_schema(_current: TrustPulseUser = Depends(get_current_user)):
    engine = get_openemr_engine()
    return inspect_schema(engine)


@router.get("/status")
def system_status(
    db: Session = Depends(get_tp_session),
    _current: TrustPulseUser = Depends(get_current_user),
):
    engine = get_openemr_engine()
    connected = False
    if engine:
        try:
            from sqlalchemy import text
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            connected = True
        except Exception:
            pass

    last_event = (
        db.query(NormalizedEvent.event_time)
        .order_by(NormalizedEvent.event_time.desc())
        .first()
    )
    last_manifest = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS")
        .order_by(IngestionManifest.completed_at.desc())
        .first()
    )

    now = datetime.utcnow()
    stale = False
    if last_event and last_event[0]:
        hours_since = (now - last_event[0]).total_seconds() / 3600
        stale = hours_since > 24
    schema = inspect_schema(engine)
    telemetry = summarize_manifest_telemetry(
        last_manifest,
        schema_info=schema,
        source_reachable=connected,
    )

    return {
        "openemr_connection": {
            "connected":         connected,
            "connector":         "openemr_real",
            "openemr_db_url":    "configured" if get_openemr_engine() else "not configured",
            "read_only_expected": True,
            "writeback_enabled": False,
            "last_event_time":   last_event[0].isoformat() if last_event and last_event[0] else None,
        },
        "trustpulse_mode":     TRUSTPULSE_MODE,
        "demo_lab_mode":       DEMO_LAB_MODE,
        "telemetry": {
            "stale": stale,
            "status": telemetry["status"],
            "status_label": telemetry["label"],
            "status_description": telemetry["description"],
            "last_manifest_hash": last_manifest.manifest_hash if last_manifest else None,
            "last_manifest_at":   last_manifest.completed_at.isoformat() if last_manifest else None,
        },
    }


@router.get("/review-config")
def review_config_summary(
    db: Session = Depends(get_tp_session),
    _current: TrustPulseUser = Depends(get_current_user),
):
    now        = datetime.utcnow()
    cutoff_24h = now - timedelta(hours=24)

    oldest       = db.query(func.min(NormalizedEvent.event_time)).scalar()
    history_days = max((now - oldest).days, 0) if oldest else 0

    events_24h = db.query(NormalizedEvent).filter(NormalizedEvent.event_time >= cutoff_24h).all()
    PATIENT_TYPES = {"patient_access", "view_record", "patient_demographics", "patient_records"}
    AUTH_TYPES    = {"failed_login", "successful_login", "login", "password_failure",
                     "authentication_failure", "logout"}
    pat24h  = sum(1 for e in events_24h if (e.event_type or "").lower() in PATIENT_TYPES)
    auth24h = sum(1 for e in events_24h if (e.event_type or "").lower() in AUTH_TYPES)

    user_rows = db.query(NormalizedEvent.user_id, NormalizedEvent.user_role).distinct().all()
    seen: set = set()
    role_breakdown: dict = {
        "Billing": 0, "Physician": 0, "Nursing": 0, "Administration": 0, "Other": 0
    }
    for uid, role in user_rows:
        if uid in seen:
            continue
        seen.add(uid)
        rl = (role or "").lower()
        u  = (uid  or "").lower()
        if   "billing" in rl or u.startswith("billing_"):
            role_breakdown["Billing"] += 1
        elif any(k in rl for k in ("physician", "clinician", "doctor")) or u.startswith("dr_"):
            role_breakdown["Physician"] += 1
        elif "nurse" in rl or "nursing" in rl or u.startswith("nurse_"):
            role_breakdown["Nursing"] += 1
        elif "admin" in rl:
            role_breakdown["Administration"] += 1
        else:
            role_breakdown["Other"] += 1

    latest_manifests = (
        db.query(IngestionManifest)
        .filter(IngestionManifest.status == "SUCCESS")
        .order_by(IngestionManifest.completed_at.desc())
        .limit(5)
        .all()
    )
    total_rows   = db.query(NormalizedEvent).count()
    invalid_rows = sum(m.parse_error_count            or 0 for m in latest_manifests)
    missing_flds = sum(m.rows_missing_required_fields  or 0 for m in latest_manifests)
    excl_policy  = sum(m.rows_excluded_by_policy       or 0 for m in latest_manifests)
    latest       = latest_manifests[0] if latest_manifests else None

    from engine.rules import BUSINESS_HOURS_START, BUSINESS_HOURS_END
    from engine.case_engine import (
        FAILED_LOGIN_REVIEW_THRESHOLD,
        FAILED_LOGIN_BURST_THRESHOLD,
        FAILED_LOGIN_REVIEW_WINDOW_MIN,
        FAILED_LOGIN_BURST_WINDOW_MIN,
        SUCCESS_AFTER_FAILURE_WINDOW_MINUTES,
        PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES,
    )

    return {
        "history_days":      history_days,
        "baseline_maturity": history_maturity_label(history_days),
        "baseline_basis":    "Configured policy threshold",
        "oldest_event_time": oldest.isoformat() if oldest else None,
        "patient_access_events_24h": pat24h,
        "auth_events_24h":           auth24h,
        "monitored_user_count":      len(seen),
        "role_breakdown":            role_breakdown,
        "validation_summary": {
            "total_rows_analyzed":     total_rows,
            "invalid_rows":            invalid_rows,
            "missing_required_fields": missing_flds,
            "excluded_by_policy":      excl_policy,
            "coverage_warning":        bool(latest and latest.coverage_warning),
            "coverage_warning_reason": (latest.coverage_warning_reason if latest else None),
            "last_validation_time":    (latest.completed_at.isoformat() if latest else None),
        },
        "review_policies": {
            "failed_login_threshold":                       FAILED_LOGIN_REVIEW_THRESHOLD,
            "failed_login_window_minutes":                  FAILED_LOGIN_REVIEW_WINDOW_MIN,
            "failed_login_burst_threshold":                 FAILED_LOGIN_BURST_THRESHOLD,
            "failed_login_burst_window_minutes":            FAILED_LOGIN_BURST_WINDOW_MIN,
            "success_after_failures_window_minutes":        SUCCESS_AFTER_FAILURE_WINDOW_MINUTES,
            "patient_access_after_failures_window_minutes": PATIENT_ACCESS_AFTER_CORRELATED_LOGIN_WINDOW_MINUTES,
            "business_hours_start":          BUSINESS_HOURS_START,
            "business_hours_end":            BUSINESS_HOURS_END,
            "billing_high_volume_threshold": int(os.environ.get("TRUSTPULSE_BULK_ACCESS_THRESHOLD", "10")),
        },
    }
