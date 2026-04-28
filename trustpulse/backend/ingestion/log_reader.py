"""
log_reader - delegates to the real OpenEMR connector.
All event fetching goes through openemr_real.py.
"""
from typing import List, Optional, Set

from db.session import get_openemr_engine
from ingestion.connectors.openemr_real import (
    fetch_new_events,
    fetch_vip_patient_ids,
    fetch_user_department,
    check_appointment_context,
    compute_source_batch_hash,
)


def fetch_new_logs(last_ingested_id: int = 0, limit: int = 5000) -> tuple:
    """
    Returns (events, parse_errors).
    Raises RuntimeError if OPENEMR_DB_URL is not configured or DB is unreachable.
    """
    engine = get_openemr_engine()
    if engine is None:
        raise RuntimeError(
            "OPENEMR_DB_URL is not configured; cannot ingest OpenEMR audit logs"
        )
    return fetch_new_events(engine, last_ingested_id, limit)


def get_vip_patient_ids() -> Set[str]:
    engine = get_openemr_engine()
    if engine is None:
        return set()
    return fetch_vip_patient_ids(engine)


def get_user_department(user_id: str) -> Optional[str]:
    engine = get_openemr_engine()
    if engine is None:
        return None
    return fetch_user_department(engine, user_id)


def get_appointment_context(user_id: str, patient_id: str) -> Optional[bool]:
    """Returns True/False if appointment table exists, None if unavailable."""
    engine = get_openemr_engine()
    if engine is None:
        return None
    return check_appointment_context(engine, user_id, patient_id)


def get_source_batch_hash(events: List[dict]) -> str:
    return compute_source_batch_hash(events)
