"""
Converts raw OpenEMR log rows into NormalizedEvent records and scores them.
Appointment context is reported honestly - None means "not evaluated".
"""
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Optional

from sqlalchemy.orm import Session

from db.models import NormalizedEvent
from engine.scorer import compute_risk_score
from engine.baseline import get_baseline_dict
from ingestion.log_reader import (
    get_vip_patient_ids,
    get_user_department,
    get_appointment_context,
)


def _build_context(raw: dict, all_raw: List[dict], vip_ids: set, user_depts: dict) -> dict:
    uid = raw["user_id"]
    event_time: datetime = (
        raw["date"] if isinstance(raw["date"], datetime)
        else datetime.fromisoformat(str(raw["date"]))
    )
    day_key      = event_time.date()
    window_start = event_time - timedelta(minutes=10)
    five_min_ago = event_time - timedelta(minutes=5)

    def _t(e):
        d = e["date"]
        return d if isinstance(d, datetime) else datetime.fromisoformat(str(d))

    same_user_today = [e for e in all_raw if e["user_id"] == uid and _t(e).date() == day_key]
    recent_window   = [e for e in all_raw if e["user_id"] == uid and window_start <= _t(e) <= event_time]
    recent_five     = [e for e in all_raw if e["user_id"] == uid and five_min_ago <= _t(e) < event_time]

    daily_patients   = {e["patient_id"] for e in same_user_today if e.get("patient_id")}
    daily_access_ct  = len(same_user_today)
    recent_failed    = sum(
        1 for e in recent_window
        if e.get("event_type") == "failed_login" and not e.get("success", True)
    )

    had_modify       = any(e["event_type"] == "record_modify" for e in recent_five)
    modify_then_exp  = had_modify and raw.get("event_type") == "report_export"

    # Appointment context: None means the table is not available (not evaluated)
    pid = raw.get("patient_id")
    has_appointment: Optional[bool] = None
    if pid:
        has_appointment = get_appointment_context(uid, pid)

    return {
        "daily_unique_patients":       len(daily_patients),
        "daily_access_count":          daily_access_ct,
        "recent_failed_logins":        recent_failed,
        "patient_is_vip":              raw.get("patient_id") in vip_ids,
        "has_appointment":             has_appointment,   # None = not evaluated
        "modify_then_export_within_5min": modify_then_exp,
        "user_department":             user_depts.get(uid),
    }


def normalize_and_score(
    raw_rows: List[dict],
    db: Session,
    manifest_id: Optional[int] = None,
) -> List[NormalizedEvent]:
    if not raw_rows:
        return []

    vip_ids   = get_vip_patient_ids()
    user_ids  = {r["user_id"] for r in raw_rows}
    user_depts = {uid: get_user_department(uid) for uid in user_ids}

    existing_ids = {r[0] for r in db.query(NormalizedEvent.source_log_id).all()}

    normalized = []
    for raw in raw_rows:
        src_id = raw["id"]
        if src_id in existing_ids:
            continue

        event_time = (
            raw["date"] if isinstance(raw["date"], datetime)
            else datetime.fromisoformat(str(raw["date"]))
        )
        event_dict = {
            "hour_of_day": event_time.hour,
            "day_of_week": event_time.weekday(),
            "event_type":  raw.get("event_type", ""),
            "patient_id":  raw.get("patient_id"),
            "department":  raw.get("department"),
            "ip_address":  raw.get("ip_address"),
            "user_role":   raw.get("user_role", ""),
        }
        baseline = get_baseline_dict(db, raw["user_id"])
        context  = _build_context(raw, raw_rows, vip_ids, user_depts)

        score, level, fired_rules = compute_risk_score(event_dict, baseline, context)

        ev = NormalizedEvent(
            source_log_id  = src_id,
            manifest_id    = manifest_id,
            event_time     = event_time,
            user_id        = raw["user_id"],
            user_name      = raw.get("user_name"),
            user_role      = raw.get("user_role"),
            event_type     = raw.get("event_type"),
            patient_id     = raw.get("patient_id"),
            department     = raw.get("department"),
            ip_address     = raw.get("ip_address"),
            hour_of_day    = event_time.hour,
            day_of_week    = event_time.weekday(),
            risk_score     = score,
            risk_level     = level,
            triggered_rules = fired_rules,
            status         = "PENDING",
        )
        db.add(ev)
        normalized.append(ev)

    db.commit()
    return normalized
