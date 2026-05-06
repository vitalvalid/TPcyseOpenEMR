from datetime import datetime, timedelta

from sqlalchemy import inspect, text


TELEMETRY_STATUS_META = {
    "HEALTHY": {
        "label": "Healthy",
        "description": "TrustPulse successfully read and interpreted OpenEMR audit telemetry.",
    },
    "COVERAGE_WARNING": {
        "label": "Coverage Warning",
        "description": "Some OpenEMR rows were not included in monitored review events. This can happen when admin/system rows are excluded or required fields are missing.",
    },
    "PARSER_ERRORS": {
        "label": "Parser Errors",
        "description": "Some OpenEMR rows could not be interpreted by the current parser.",
    },
    "STALE_TELEMETRY": {
        "label": "Stale Telemetry",
        "description": "TrustPulse has not successfully ingested recent OpenEMR telemetry.",
    },
    "SOURCE_UNREACHABLE": {
        "label": "OpenEMR Source Unreachable",
        "description": "TrustPulse could not connect to OpenEMR.",
    },
    "UNSUPPORTED_SCHEMA": {
        "label": "Unsupported OpenEMR Schema",
        "description": "Required OpenEMR audit tables or columns were not available.",
    },
    "DEGRADED": {
        "label": "Degraded",
        "description": "Telemetry was partially available but has quality issues.",
    },
    "UNKNOWN": {
        "label": "Unknown",
        "description": "Telemetry status could not be determined.",
    },
}


def ensure_ingestion_manifest_schema(engine) -> None:
    inspector = inspect(engine)
    if "ingestion_manifests" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("ingestion_manifests")}
    additions = {
        "source_rows_seen": "INTEGER DEFAULT 0",
        "rows_inserted": "INTEGER DEFAULT 0",
        "rows_excluded_by_policy": "INTEGER DEFAULT 0",
        "rows_excluded_system_admin": "INTEGER DEFAULT 0",
        "rows_unsupported_event_type": "INTEGER DEFAULT 0",
        "rows_missing_required_fields": "INTEGER DEFAULT 0",
        "raw_source_gap_detected": "BOOLEAN DEFAULT 0",
        "raw_source_gap_ranges_json": "JSON",
        "coverage_warning": "BOOLEAN DEFAULT 0",
        "coverage_warning_reason": "TEXT",
        "last_source_id_seen": "BIGINT",
        "last_source_id_ingested": "BIGINT",
        "telemetry_status": "VARCHAR(50)",
        "telemetry_status_label": "VARCHAR(100)",
        "telemetry_status_description": "TEXT",
    }
    with engine.begin() as conn:
        for col, ddl in additions.items():
            if col not in cols:
                conn.execute(text(f"ALTER TABLE ingestion_manifests ADD COLUMN {col} {ddl}"))


def telemetry_meta(status: str) -> dict:
    return TELEMETRY_STATUS_META.get(status or "UNKNOWN", TELEMETRY_STATUS_META["UNKNOWN"])


def summarize_manifest_telemetry(manifest, *, schema_info: dict | None = None, source_reachable: bool | None = None) -> dict:
    status = "UNKNOWN"
    if manifest is None:
        return {
            "status": "UNKNOWN",
            "label": telemetry_meta("UNKNOWN")["label"],
            "description": telemetry_meta("UNKNOWN")["description"],
        }

    schema_strategy = (schema_info or {}).get("selected_ingestion_strategy")
    if source_reachable is False:
        status = "SOURCE_UNREACHABLE"
    elif schema_strategy == "none":
        status = "UNSUPPORTED_SCHEMA"
    elif getattr(manifest, "status", None) == "FAILED":
        status = "SOURCE_UNREACHABLE"
    elif getattr(manifest, "parse_error_count", 0) > 0:
        error_count = getattr(manifest, "parse_error_count", 0) or 0
        rows_seen = getattr(manifest, "source_rows_seen", None) or getattr(manifest, "source_row_count", 0) or 0
        status = "DEGRADED" if rows_seen and error_count < rows_seen else "PARSER_ERRORS"
    elif getattr(manifest, "coverage_warning", False):
        status = "COVERAGE_WARNING"
    elif _is_stale(getattr(manifest, "completed_at", None)):
        status = "STALE_TELEMETRY"
    elif getattr(manifest, "inserted_count", 0) >= 0:
        status = "HEALTHY"

    meta = telemetry_meta(status)
    return {"status": status, "label": meta["label"], "description": meta["description"]}


def _is_stale(completed_at: datetime | None) -> bool:
    if not completed_at:
        return False
    return (datetime.utcnow() - completed_at) > timedelta(hours=24)
