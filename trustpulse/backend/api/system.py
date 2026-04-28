"""
System / connectivity endpoints.
"""
import os
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from db.models import NormalizedEvent, IngestionManifest
from db.session import get_tp_session, get_openemr_engine, TRUSTPULSE_MODE
from ingestion.openemr_schema import inspect_schema
from api.auth import get_current_user, TrustPulseUser

router = APIRouter(prefix="/api/system", tags=["system"])

DEMO_LAB_MODE = os.environ.get("TRUSTPULSE_ALLOW_OPENEMR_DEMO_WRITE", "").lower() == "true"


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
            "last_manifest_hash": last_manifest.manifest_hash if last_manifest else None,
            "last_manifest_at":   last_manifest.completed_at.isoformat() if last_manifest else None,
        },
    }
