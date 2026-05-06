from datetime import datetime, timedelta

from telemetry_health import summarize_manifest_telemetry
from db.models import IngestionManifest


def _manifest(**overrides):
    manifest = IngestionManifest(
        connector_name="openemr_real",
        source_system="openemr",
        status="SUCCESS",
        source_row_count=10,
        source_rows_seen=10,
        inserted_count=10,
        parse_error_count=0,
        coverage_warning=False,
        completed_at=datetime.utcnow(),
    )
    for key, value in overrides.items():
        setattr(manifest, key, value)
    return manifest


def test_summarize_manifest_telemetry_marks_first_class_coverage_warning():
    telemetry = summarize_manifest_telemetry(
        _manifest(coverage_warning=True),
        schema_info={"selected_ingestion_strategy": "log"},
        source_reachable=True,
    )
    assert telemetry["status"] == "COVERAGE_WARNING"
    assert telemetry["label"] == "Coverage Warning"


def test_summarize_manifest_telemetry_distinguishes_parser_errors_from_degraded():
    parser_only = summarize_manifest_telemetry(
        _manifest(parse_error_count=10, source_rows_seen=10),
        schema_info={"selected_ingestion_strategy": "log"},
        source_reachable=True,
    )
    degraded = summarize_manifest_telemetry(
        _manifest(parse_error_count=2, source_rows_seen=10),
        schema_info={"selected_ingestion_strategy": "log"},
        source_reachable=True,
    )
    assert parser_only["status"] == "PARSER_ERRORS"
    assert degraded["status"] == "DEGRADED"


def test_summarize_manifest_telemetry_detects_stale_successful_ingestion():
    telemetry = summarize_manifest_telemetry(
        _manifest(completed_at=datetime.utcnow() - timedelta(hours=25)),
        schema_info={"selected_ingestion_strategy": "log"},
        source_reachable=True,
    )
    assert telemetry["status"] == "STALE_TELEMETRY"


def test_summarize_manifest_telemetry_prioritizes_source_reachability_and_schema():
    unreachable = summarize_manifest_telemetry(
        _manifest(),
        schema_info={"selected_ingestion_strategy": "log"},
        source_reachable=False,
    )
    unsupported = summarize_manifest_telemetry(
        _manifest(),
        schema_info={"selected_ingestion_strategy": "none"},
        source_reachable=True,
    )
    assert unreachable["status"] == "SOURCE_UNREACHABLE"
    assert unsupported["status"] == "UNSUPPORTED_SCHEMA"
