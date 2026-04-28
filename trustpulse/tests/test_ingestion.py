"""
Tests for ingestion manifest hashing, gap detection, and normalizer.
"""
import hashlib
import json
from datetime import datetime

import pytest
from api.ingestion import (
    _compute_manifest_hash, _detect_gaps, _compute_normalized_batch_hash,
)
from db.models import IngestionManifest, NormalizedEvent
from ingestion.normalizer import normalize_and_score


class TestManifestHashing:
    def _make_manifest(self):
        m = IngestionManifest(
            connector_name="openemr_real",
            source_system="openemr",
            source_min_id=1,
            source_max_id=10,
            source_row_count=10,
            inserted_count=8,
            duplicate_count=2,
            parse_error_count=0,
            source_batch_sha256="a" * 64,
            normalized_batch_sha256="b" * 64,
            started_at=datetime(2026, 1, 1, 12, 0, 0),
        )
        return m

    def test_manifest_hash_is_deterministic(self):
        m = self._make_manifest()
        h1 = _compute_manifest_hash(m, "0" * 64)
        h2 = _compute_manifest_hash(m, "0" * 64)
        assert h1 == h2

    def test_manifest_hash_changes_with_different_previous_hash(self):
        m = self._make_manifest()
        h1 = _compute_manifest_hash(m, "0" * 64)
        h2 = _compute_manifest_hash(m, "a" * 64)
        assert h1 != h2

    def test_manifest_hash_changes_when_count_changes(self):
        m = self._make_manifest()
        h1 = _compute_manifest_hash(m, "0" * 64)
        m.inserted_count = 9
        h2 = _compute_manifest_hash(m, "0" * 64)
        assert h1 != h2

    def test_manifest_hash_is_64_chars(self):
        m = self._make_manifest()
        h = _compute_manifest_hash(m, "0" * 64)
        assert len(h) == 64


class TestGapDetection:
    def test_no_gap_in_sequential_ids(self):
        gap, ranges = _detect_gaps(0, [1, 2, 3, 4, 5])
        assert gap is False
        assert ranges == []

    def test_gap_detected_within_batch(self):
        gap, ranges = _detect_gaps(0, [1, 2, 5, 6, 7])
        assert gap is True
        assert any(r == {"from": 3, "to": 4} for r in ranges)

    def test_gap_detected_between_last_and_batch(self):
        # last_id=100, batch starts at 105 → gap 101–104
        gap, ranges = _detect_gaps(100, [105, 106, 107])
        assert gap is True
        assert {"from": 101, "to": 104} in ranges

    def test_multiple_gaps(self):
        gap, ranges = _detect_gaps(0, [1, 3, 6, 8])
        assert gap is True
        assert len(ranges) >= 2

    def test_empty_ids(self):
        gap, ranges = _detect_gaps(0, [])
        assert gap is False

    def test_no_gap_when_batch_continues_last_id(self):
        gap, ranges = _detect_gaps(10, [11, 12, 13])
        assert gap is False
        assert ranges == []


class TestNormalizer:
    def test_no_events_returns_empty(self, db):
        result = normalize_and_score([], db)
        assert result == []

    def test_duplicate_source_id_skipped(self, db):
        """Already-ingested IDs must not be inserted again."""
        dt   = datetime(2026, 1, 15, 10, 0, 0)
        raw  = {
            "id": 42, "date": dt,
            "user_id": "test_user", "user_name": "Test", "user_role": "clinician",
            "event_type": "patient_access", "patient_id": None,
            "department": None, "ip_address": None,
            "source_log_id": 42, "source_payload_hash": "x" * 64,
            "raw_payload_minimized": {},
        }
        normalize_and_score([raw], db)
        count_before = db.query(NormalizedEvent).count()
        normalize_and_score([raw], db)
        count_after  = db.query(NormalizedEvent).count()
        assert count_before == count_after

    def test_no_logs_means_no_cases(self, db):
        """If no OpenEMR logs exist, no events or cases should be created."""
        result = normalize_and_score([], db)
        assert result == []
        assert db.query(NormalizedEvent).count() == 0
