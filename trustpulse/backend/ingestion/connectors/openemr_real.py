"""
Real OpenEMR connector - reads from OpenEMR log + api_log tables.

SELECT-only. Never writes to OpenEMR. Never falls back to fake data.
If the database is unreachable, returns an empty list and logs the error.
"""
import hashlib
import json
import logging
import re
from datetime import datetime
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("trustpulse.connector.openemr_real")

CONNECTOR_NAME = "openemr_real"
SOURCE_SYSTEM = "openemr"

ROLE_MAP = {
    "Internal Medicine":  "clinician",
    "Oncology":           "clinician",
    "Family Medicine":    "clinician",
    "Pediatrics":         "clinician",
    "Dermatology":        "clinician",
    "General Practice":   "nurse",
    "Nursing":            "nurse",
    "Billing":            "billing",
    "Administration":     "admin",
    "Information Technology": "it",
}

# Built-in system accounts that are not monitored clinical staff.
# Entries with these usernames are skipped during ingestion so they
# never appear as employees or generate cases in TrustPulse.
SKIP_USERNAMES: frozenset = frozenset({"admin"})

_user_cache: dict = {}


# ── SQL allowlist ─────────────────────────────────────────────────────────────

def _assert_select_only(sql: str) -> None:
    """Raise if the SQL is not a SELECT statement."""
    stripped = sql.strip().lstrip("(").strip().lower()
    if not stripped.startswith("select"):
        raise PermissionError(
            f"SQL allowlist violation: only SELECT is permitted. "
            f"Got: {sql[:80]!r}"
        )


# ── User enrichment ───────────────────────────────────────────────────────────

def _get_user_info(engine: Engine, username: str) -> dict:
    if username in _user_cache:
        return _user_cache[username]
    default = {"user_name": username, "user_role": "staff", "department": None}
    try:
        sql = "SELECT fname, lname, specialty, facility FROM users WHERE username = :u"
        _assert_select_only(sql)
        with engine.connect() as conn:
            row = conn.execute(text(sql), {"u": username}).fetchone()
            if row:
                fname, lname, specialty, facility = row
                name = f"{fname or ''} {lname or ''}".strip() or username
                role = ROLE_MAP.get(specialty or "", "staff")
                dept = facility or specialty or None
                info = {"user_name": name, "user_role": role, "department": dept}
                _user_cache[username] = info
                return info
    except Exception as exc:
        log.debug("user lookup failed for %s: %s", username, exc)
    _user_cache[username] = default
    return default


def invalidate_user_cache() -> None:
    _user_cache.clear()


# ── Event type mapping ────────────────────────────────────────────────────────

def _request_to_event_type(method: str, request: str) -> str:
    r = (request or "").lower()
    m = (method or "GET").upper()
    if "/patient/" in r and (
        "/encounter" in r or "/condition" in r or "/observation" in r
        or "/medication" in r
    ):
        return "record_modify" if m in ("POST", "PUT", "PATCH") else "patient_access"
    if "/patient" in r:
        return "patient_access"
    if "/facility" in r or "/practitioner" in r:
        return "admin_action"
    if "/allergy" in r or "/prescription" in r:
        return "patient_access"
    if "/document" in r or "/report" in r:
        return "report_export"
    if m in ("POST", "PUT", "PATCH", "DELETE"):
        return "record_modify"
    return "patient_access"


def _openemr_event_to_tp(event: str) -> str:
    ev = (event or "").lower()
    mapping = [
        ("patient-record",                  "patient_access"),
        ("patient-report",                  "report_export"),
        ("billing-",                        "billing_action"),
        ("login-failure",                   "failed_login"),
        ("failed-login",                    "failed_login"),
        ("login-success",                   "login"),
        ("login",                           "login"),
        ("logout",                          "logout"),
        ("security-administration-select",  "admin_action"),
        ("security-administration-insert",  "admin_action"),
        ("security-administration-update",  "admin_action"),
        ("security-administration-",        "admin_action"),
        ("api",                             "patient_access"),
    ]
    for prefix, tp in mapping:
        if ev.startswith(prefix):
            return tp
    return ev or "unknown"


def _parse_patient_id(request: str) -> Optional[str]:
    m = re.search(r"/patient/(\d+)", request or "")
    return m.group(1) if m else None


# ── Payload hashing (governance metadata only) ────────────────────────────────

def _minimize_payload(row: dict) -> dict:
    """Return only governance metadata, never clinical content."""
    return {
        "source_id":  row.get("id"),
        "event":      row.get("event"),
        "method":     row.get("method"),
        "request":    row.get("request"),
    }


def _hash_source_row(row: dict) -> str:
    canonical = json.dumps({
        "id":         row.get("id"),
        "date":       str(row.get("date")),
        "user":       row.get("user"),
        "event":      row.get("event"),
        "patient_id": row.get("log_patient_id"),
        "ip_address": row.get("ip_address"),
        "method":     row.get("method"),
        "request":    row.get("request"),
    }, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_source_batch_hash(events: List[dict]) -> str:
    """SHA256 over sorted concatenation of all source row hashes."""
    hashes = sorted(e["source_payload_hash"] for e in events)
    return hashlib.sha256("|".join(hashes).encode()).hexdigest()


# ── Table availability ────────────────────────────────────────────────────────

def check_available_tables(engine: Engine) -> dict:
    tables = ["log", "api_log", "users", "patient_data",
              "openemr_postcalendar_events"]
    result = {}
    for tbl in tables:
        try:
            _assert_select_only(f"SELECT 1 FROM {tbl} LIMIT 1")
            with engine.connect() as conn:
                conn.execute(text(f"SELECT 1 FROM {tbl} LIMIT 1"))
            result[tbl] = True
        except Exception:
            result[tbl] = False
    return result


# ── Main fetch functions ──────────────────────────────────────────────────────

def _fetch_log_plus_api_log(engine: Engine, last_id: int, limit: int) -> tuple:
    sql = """
        SELECT
            l.id,
            l.date,
            l.user,
            l.event,
            l.patient_id AS log_patient_id,
            al.method,
            al.request,
            al.ip_address
        FROM log l
        LEFT JOIN api_log al ON al.log_id = l.id
        WHERE l.id > :last_id
        ORDER BY l.id ASC
        LIMIT :lim
    """
    _assert_select_only(sql)
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in
                    conn.execute(text(sql), {"last_id": last_id, "lim": limit})]
    except Exception as exc:
        raise RuntimeError(f"OpenEMR log+api_log fetch failed: {exc}") from exc
    return _normalize_raw_rows(engine, rows)


def _fetch_log_only(engine: Engine, last_id: int, limit: int) -> tuple:
    sql = """
        SELECT id, date, user, event, patient_id AS log_patient_id
        FROM log
        WHERE id > :last_id
        ORDER BY id ASC
        LIMIT :lim
    """
    _assert_select_only(sql)
    try:
        with engine.connect() as conn:
            rows = [dict(r._mapping) for r in
                    conn.execute(text(sql), {"last_id": last_id, "lim": limit})]
    except Exception as exc:
        raise RuntimeError(f"OpenEMR log-only fetch failed: {exc}") from exc
    return _normalize_raw_rows(engine, rows)


def _normalize_raw_rows(engine: Engine, raw_rows: List[dict]) -> tuple:
    """Returns (normalized_events, parse_errors)."""
    normalized = []
    errors = []
    for r in raw_rows:
        try:
            username = r.get("user") or ""
            if not username or username in SKIP_USERNAMES:
                continue
            user_info = _get_user_info(engine, username)

            request_path = r.get("request") or ""
            method       = r.get("method") or "GET"
            ip           = r.get("ip_address") or None  # never fabricate an IP

            openemr_event = r.get("event") or ""
            if openemr_event.lower() == "api" and request_path:
                tp_event = _request_to_event_type(method, request_path)
            else:
                tp_event = _openemr_event_to_tp(openemr_event)

            pid = _parse_patient_id(request_path)
            if not pid:
                raw_pid = r.get("log_patient_id")
                pid = str(raw_pid) if raw_pid and raw_pid != 0 else None

            event_time = r.get("date")
            if not isinstance(event_time, datetime):
                event_time = datetime.fromisoformat(str(event_time))

            success = tp_event not in ("failed_login",)

            normalized.append({
                "source_system":          SOURCE_SYSTEM,
                "connector_name":         CONNECTOR_NAME,
                "source_log_id":          r["id"],
                "event_time":             event_time,
                "user_id":                username,
                "username":               user_info["user_name"],
                "raw_event_type":         openemr_event,
                "event_type":             tp_event,
                "patient_id":             pid,
                "ip_address":             ip,
                "role":                   user_info["user_role"],
                "department":             user_info["department"],
                "success":                success,
                "raw_payload_minimized":  _minimize_payload(r),
                "source_payload_hash":    _hash_source_row(r),
                # Legacy aliases for normalizer
                "id":                     r["id"],
                "date":                   event_time,
                "user_name":              user_info["user_name"],
                "user_role":              user_info["user_role"],
            })
        except Exception as exc:
            log.warning("Failed to normalize row id=%s: %s", r.get("id"), exc)
            errors.append({
                "source_log_id":          str(r.get("id", "")),
                "error_type":             "NORMALIZATION_ERROR",
                "error_message":          str(exc),
                "raw_payload_minimized":  _minimize_payload(r),
            })

    return normalized, errors


def fetch_new_events(
    engine: Engine,
    last_ingested_id: int = 0,
    limit: int = 5000,
) -> tuple:
    """
    Fetch new audit events from OpenEMR log (+ api_log if available).
    Returns (events, parse_errors). Raises RuntimeError if DB is unreachable.
    """
    available = check_available_tables(engine)

    if available.get("log") and available.get("api_log"):
        return _fetch_log_plus_api_log(engine, last_ingested_id, limit)
    if available.get("log"):
        log.info("api_log not available; falling back to log-only ingestion")
        return _fetch_log_only(engine, last_ingested_id, limit)

    raise RuntimeError(
        "No supported OpenEMR audit tables found in the connected database. "
        "Expected 'log' and/or 'api_log'. "
        "Verify OPENEMR_DB_URL points to a valid OpenEMR instance and that "
        "the trustpulse_ro user has SELECT on those tables."
    )


# ── Supplemental lookups ──────────────────────────────────────────────────────

def fetch_vip_patient_ids(engine: Engine) -> set:
    """Find patients marked VIP in OpenEMR's patient_data table."""
    try:
        sql = ("SELECT pid FROM patient_data "
               "WHERE lname LIKE 'VIP-%' OR fname LIKE 'VIP-%'")
        _assert_select_only(sql)
        with engine.connect() as conn:
            return {str(r[0]) for r in conn.execute(text(sql))}
    except Exception as exc:
        log.debug("VIP patient lookup unavailable: %s", exc)
        return set()


def check_appointment_context(
    engine: Engine, user_id: str, patient_id: str
) -> Optional[bool]:
    """
    Check if there is an appointment linking this user to this patient.
    Returns None when the calendar table is not accessible.
    """
    try:
        sql = """
            SELECT COUNT(*) FROM openemr_postcalendar_events
            WHERE pc_aid = (SELECT id FROM users WHERE username = :uid LIMIT 1)
              AND pc_pid = :pid
        """
        _assert_select_only(sql)
        with engine.connect() as conn:
            count = conn.execute(
                text(sql), {"uid": user_id, "pid": patient_id}
            ).scalar()
            return count > 0
    except Exception as exc:
        log.debug("Appointment context check unavailable: %s", exc)
        return None


def fetch_user_department(engine: Engine, user_id: str) -> Optional[str]:
    return _get_user_info(engine, user_id).get("department")
