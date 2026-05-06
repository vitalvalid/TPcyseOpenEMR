"""
Tests for admin platform settings, including enforceable ingestion interval behavior.
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from api.admin import (
    DEFAULT_SETTINGS,
    SaveSettingsRequest,
    get_effective_ingestion_interval_seconds,
    save_settings,
)
from api.auth import hash_password
from db.models import PlatformSetting, TrustPulseUser


@pytest.fixture
def admin_user(db):
    user = TrustPulseUser(
        email="admin@clinic.test",
        hashed_password=hash_password("Password1!"),
        display_name="Admin User",
        role="TRUSTPULSE_ADMIN",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_effective_ingestion_interval_defaults_to_saved_default(db):
    assert get_effective_ingestion_interval_seconds(db) == int(DEFAULT_SETTINGS["ingestion_interval_seconds"])


def test_effective_ingestion_interval_uses_saved_setting(db, admin_user):
    req = SaveSettingsRequest(ingestion_interval_seconds="17")
    save_settings(req, db=db, admin=admin_user)

    assert get_effective_ingestion_interval_seconds(db) == 17
    row = db.get(PlatformSetting, "ingestion_interval_seconds")
    assert row.value == "17"


def test_effective_ingestion_interval_falls_back_when_saved_value_is_invalid(db):
    db.add(PlatformSetting(key="ingestion_interval_seconds", value="not-a-number", updated_by="seed"))
    db.commit()

    assert get_effective_ingestion_interval_seconds(db) == int(DEFAULT_SETTINGS["ingestion_interval_seconds"])


def test_save_settings_rejects_non_numeric_ingestion_interval(db, admin_user):
    req = SaveSettingsRequest(ingestion_interval_seconds="abc")

    with pytest.raises(HTTPException) as exc:
        save_settings(req, db=db, admin=admin_user)

    assert exc.value.status_code == 400
    assert "whole number" in exc.value.detail


def test_save_settings_rejects_out_of_range_ingestion_interval(db, admin_user):
    req = SaveSettingsRequest(ingestion_interval_seconds="0")

    with pytest.raises(HTTPException) as exc:
        save_settings(req, db=db, admin=admin_user)

    assert exc.value.status_code == 400
    assert "between" in exc.value.detail
