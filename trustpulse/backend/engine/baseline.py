"""
Per-user behavioral baselines from real NormalizedEvent history.
Maturity levels reflect how much history is available.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict

import numpy as np
from sqlalchemy.orm import Session

from db.models import UserBaseline, NormalizedEvent

# Small-clinic tuning: lower thresholds so baselines activate faster
_TRAINING_THRESHOLD = 50
_ACTIVE_THRESHOLD   = 200
_STALE_DAYS         = 30


def _compute_maturity(event_count: int, last_event_time: datetime) -> str:
    now   = datetime.utcnow()
    stale = (now - last_event_time).days > _STALE_DAYS if last_event_time else True
    if stale and event_count > 0:
        return "DEGRADED"
    if event_count < _TRAINING_THRESHOLD:
        return "COLD_START"
    if event_count < _ACTIVE_THRESHOLD:
        return "TRAINING"
    return "ACTIVE"


def compute_baselines(db: Session) -> Dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=30)
    events = (
        db.query(NormalizedEvent)
        .filter(NormalizedEvent.event_time >= cutoff)
        .all()
    )

    per_user: Dict[str, dict] = defaultdict(lambda: {
        "daily_counts":   defaultdict(int),
        "daily_patients": defaultdict(set),
        "hours":          [],
        "departments":    set(),
        "ips":            set(),
        "last_event":     None,
    })

    for ev in events:
        uid = ev.user_id
        day = ev.event_time.date().isoformat()
        per_user[uid]["daily_counts"][day] += 1
        if ev.patient_id:
            per_user[uid]["daily_patients"][day].add(ev.patient_id)
        per_user[uid]["hours"].append(ev.hour_of_day)
        if ev.department:
            per_user[uid]["departments"].add(ev.department)
        if ev.ip_address:
            per_user[uid]["ips"].add(ev.ip_address)
        if (per_user[uid]["last_event"] is None
                or ev.event_time > per_user[uid]["last_event"]):
            per_user[uid]["last_event"] = ev.event_time

    baselines: Dict[str, Any] = {}
    for uid, data in per_user.items():
        counts         = list(data["daily_counts"].values())
        patient_counts = [len(v) for v in data["daily_patients"].values()]
        hours          = data["hours"] or [12]
        total_events   = sum(counts)

        avg_daily  = float(np.mean(counts)) if counts else 0.0
        std_daily  = float(np.std(counts)) if len(counts) > 1 else 1.0
        avg_pts    = float(np.mean(patient_counts)) if patient_counts else 0.0
        p10        = int(np.percentile(hours, 10))
        p90        = int(np.percentile(hours, 90))

        maturity = _compute_maturity(total_events, data["last_event"])

        baselines[uid] = {
            "avg_daily_accesses":  avg_daily,
            "std_daily_accesses":  std_daily,
            "avg_unique_patients": avg_pts,
            "typical_hours_start": p10,
            "typical_hours_end":   p90,
            "departments_seen":    list(data["departments"]),
            "known_ips":           list(data["ips"]),
            "maturity":            maturity,
            "training_event_count": total_events,
        }

    return baselines


def save_baselines(db: Session, baselines: Dict[str, Any]) -> None:
    for uid, b in baselines.items():
        existing = db.get(UserBaseline, uid)
        if existing:
            # Do not downgrade LOCKED maturity
            if existing.maturity == "LOCKED":
                b["maturity"] = "LOCKED"
            for k, v in b.items():
                setattr(existing, k, v)
            existing.last_updated = datetime.utcnow()
        else:
            db.add(UserBaseline(user_id=uid, last_updated=datetime.utcnow(), **b))
    db.commit()


def get_baseline_dict(db: Session, user_id: str) -> dict:
    bl = db.get(UserBaseline, user_id)
    if not bl:
        return {}
    return {
        "avg_daily_accesses":   bl.avg_daily_accesses,
        "std_daily_accesses":   bl.std_daily_accesses,
        "avg_unique_patients":  bl.avg_unique_patients,
        "typical_hours_start":  bl.typical_hours_start,
        "typical_hours_end":    bl.typical_hours_end,
        "departments_seen":     bl.departments_seen or [],
        "known_ips":            bl.known_ips or [],
        "maturity":             bl.maturity or "COLD_START",
        "training_event_count": bl.training_event_count or 0,
    }
