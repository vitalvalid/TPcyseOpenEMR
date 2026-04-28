"""
OpenEMR schema inspector - checks which tables and columns are accessible.
Determines the best ingestion strategy and reports honest limitations.
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("trustpulse.openemr_schema")

TABLES_TO_INSPECT: Dict[str, List[str]] = {
    "log": ["id", "date", "user", "event", "patient_id"],
    "api_log": ["id", "log_id", "method", "request", "ip_address"],
    "users": ["id", "username", "fname", "lname", "specialty", "facility"],
    "patient_data": ["pid", "fname", "lname", "DOB"],
    "openemr_postcalendar_events": ["pc_eid", "pc_aid", "pc_pid", "pc_eventDate"],
}


def inspect_schema(engine: Optional[Engine]) -> Dict[str, Any]:
    if engine is None:
        return {
            "connected": False,
            "error": "OPENEMR_DB_URL is not configured",
            "tables": {},
            "selected_ingestion_strategy": "none",
            "limitations": ["OpenEMR database URL is not configured."],
        }

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
            "tables": {},
            "selected_ingestion_strategy": "none",
            "limitations": [f"Cannot connect to OpenEMR database: {exc}"],
        }

    tables: Dict[str, Any] = {}
    limitations: List[str] = []

    for tbl, expected_cols in TABLES_TO_INSPECT.items():
        tables[tbl] = _inspect_table(engine, tbl, expected_cols)
        if not tables[tbl]["exists"]:
            limitations.append(
                f"Table '{tbl}' not found - "
                + _limitation_for_missing(tbl)
            )

    has_log     = tables["log"]["exists"]
    has_api_log = tables["api_log"]["exists"]
    has_users   = tables["users"]["exists"]
    has_patient = tables["patient_data"]["exists"]
    has_cal     = tables["openemr_postcalendar_events"]["exists"]

    if has_log and has_api_log:
        strategy = "log_plus_api_log"
    elif has_log:
        strategy = "log_only"
        limitations.append(
            "api_log table not available - IP address and HTTP path will be "
            "missing from events; R-09 (new IP) may not evaluate."
        )
    else:
        strategy = "none"
        limitations.append(
            "Neither 'log' nor 'api_log' found - ingestion will not work until "
            "OPENEMR_DB_URL points to a real OpenEMR database."
        )

    if not has_users:
        limitations.append(
            "users table not found - user role and department cannot be enriched."
        )
    if not has_patient:
        limitations.append(
            "patient_data table not found - VIP patient lookup unavailable; "
            "R-05 will not be evaluated."
        )
    if not has_cal:
        limitations.append(
            "openemr_postcalendar_events not found - appointment context unavailable; "
            "R-05 (VIP/no-appointment rule) will not be evaluated."
        )

    return {
        "connected": True,
        "tables": tables,
        "selected_ingestion_strategy": strategy,
        "limitations": limitations,
    }


def _inspect_table(engine: Engine, table_name: str, expected: List[str]) -> Dict[str, Any]:
    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SELECT * FROM {table_name} LIMIT 0"))
            actual = list(result.keys())
        missing = [c for c in expected if c not in actual]
        return {
            "exists": True,
            "columns": actual,
            "expected_columns": expected,
            "missing_expected_columns": missing,
        }
    except Exception as exc:
        return {
            "exists": False,
            "error": str(exc),
            "columns": [],
            "expected_columns": expected,
            "missing_expected_columns": expected,
        }


def _limitation_for_missing(table: str) -> str:
    notes = {
        "log":   "primary audit source missing; no events will be ingested.",
        "api_log": "HTTP request details (IP, path) will not be available.",
        "users": "user role/department enrichment disabled.",
        "patient_data": "patient VIP lookup disabled.",
        "openemr_postcalendar_events": "appointment context unavailable.",
    }
    return notes.get(table, "some features may be unavailable.")
