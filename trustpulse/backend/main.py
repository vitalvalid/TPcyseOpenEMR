"""
TrustPulse v3 - FastAPI entry point.
Reads real OpenEMR audit logs only. No mock data, no auto-seeding.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db.session import init_db, get_openemr_engine, TrustPulseSession
from engine.baseline import compute_baselines, save_baselines
from engine.case_engine import generate_cases
from engine.compliance import seed_compliance_calendar, save_trust_scores
from api.auth import router as auth_router, bootstrap_admin, bootstrap_sample_users
from api.system import router as system_router
from api.ingestion import router as ingestion_router, run_ingestion_cycle
from api.cases import router as cases_router
from api.evidence import router as evidence_router
from api.compliance_api import router as compliance_router
from api.dashboard import router as dashboard_router
from api.reports import router as reports_router, run_due_scheduled_reports
from api.admin import router as admin_router, privacy_user_router
from api.users import router as users_router

INGESTION_INTERVAL = int(os.environ.get("INGESTION_INTERVAL_SECONDS", "60"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("trustpulse")


def _rescore_events(db) -> int:
    """
    Rescore every NormalizedEvent using current baselines.
    Call this after save_baselines() so cold-start events get recalibrated scores.
    Returns the number of events rescored.
    """
    from datetime import timedelta
    from db.models import NormalizedEvent
    from engine.scorer import compute_risk_score
    from engine.baseline import get_baseline_dict

    events = db.query(NormalizedEvent).all()
    for ev in events:
        baseline = get_baseline_dict(db, ev.user_id)
        event_dict = {
            "hour_of_day": ev.hour_of_day,
            "day_of_week": ev.day_of_week,
            "event_type":  ev.event_type or "",
            "patient_id":  ev.patient_id,
            "department":  ev.department,
            "ip_address":  ev.ip_address,
            "user_role":   ev.user_role or "",
        }
        # Rebuild context from DB (best-effort; appointment/VIP context not available post-hoc)
        day_start = ev.event_time.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end   = day_start + timedelta(days=1)
        w_start   = ev.event_time - timedelta(minutes=10)
        five_ago  = ev.event_time - timedelta(minutes=5)

        same_day = db.query(NormalizedEvent).filter(
            NormalizedEvent.user_id == ev.user_id,
            NormalizedEvent.event_time >= day_start,
            NormalizedEvent.event_time < day_end,
        ).all()
        daily_pts = len({e.patient_id for e in same_day if e.patient_id})
        daily_ct  = len(same_day)
        failed    = db.query(NormalizedEvent).filter(
            NormalizedEvent.user_id == ev.user_id,
            NormalizedEvent.event_type == "failed_login",
            NormalizedEvent.event_time >= w_start,
            NormalizedEvent.event_time <= ev.event_time,
        ).count()
        had_mod = db.query(NormalizedEvent).filter(
            NormalizedEvent.user_id == ev.user_id,
            NormalizedEvent.event_type == "record_modify",
            NormalizedEvent.event_time >= five_ago,
            NormalizedEvent.event_time < ev.event_time,
        ).count() > 0

        context = {
            "daily_unique_patients":          daily_pts,
            "daily_access_count":             daily_ct,
            "recent_failed_logins":           failed,
            "patient_is_vip":                 False,
            "has_appointment":                None,
            "modify_then_export_within_5min": had_mod and ev.event_type == "report_export",
            "user_department":                ev.department,
        }
        score, level, fired_rules = compute_risk_score(event_dict, baseline, context)
        ev.risk_score      = score
        ev.risk_level      = level
        ev.triggered_rules = fired_rules
    db.commit()
    return len(events)


async def background_poller():
    while True:
        await asyncio.sleep(INGESTION_INTERVAL)
        db = TrustPulseSession()
        try:
            result = run_ingestion_cycle(db)
            if result["events_ingested"] > 0:
                log.info("Ingested %d events (manifest %s)",
                         result["events_ingested"], result.get("manifest_hash", "")[:12])
                baselines = compute_baselines(db)
                save_baselines(db, baselines)
                _rescore_events(db)
                generate_cases(db)
                save_trust_scores(db)
            n_reports = run_due_scheduled_reports(db)
            if n_reports:
                log.info("Scheduled reports: ran %d report(s)", n_reports)
        except Exception as exc:
            log.warning("Background poll error: %s", exc)
        finally:
            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("TrustPulse v3 starting up...")
    init_db()
    log.info("TrustPulse DB initialised")

    db = TrustPulseSession()
    try:
        bootstrap_admin(db)
        bootstrap_sample_users(db)

        engine = get_openemr_engine()
        if engine:
            try:
                from sqlalchemy import text
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                log.info("OpenEMR DB connected (read-only)")
            except Exception as exc:
                log.warning("OpenEMR DB unreachable: %s", exc)
        else:
            log.warning("OPENEMR_DB_URL not set - no events will be ingested")

        result = run_ingestion_cycle(db)
        log.info("Initial ingestion: %d events ingested, %d scored",
                 result["events_ingested"], result["events_scored"])

        # Compute baselines AFTER ingestion, then rescore so events use real baselines
        baselines = compute_baselines(db)
        save_baselines(db, baselines)
        log.info("Baselines computed for %d users", len(baselines))

        n_cases = generate_cases(db)
        log.info("Case engine: %d new cases", n_cases)

        save_trust_scores(db)
        seed_compliance_calendar(db)

    except Exception as exc:
        log.warning("Startup pipeline error: %s", exc)
    finally:
        db.close()

    task = asyncio.create_task(background_poller())
    async def _deferred_rescore():
        await asyncio.sleep(5)
        _db = TrustPulseSession()
        try:
            baselines = compute_baselines(_db)
            save_baselines(_db, baselines)
            n = _rescore_events(_db)
            generate_cases(_db)
            log.info("Deferred rescore complete: %d events rescored", n)
        except Exception as exc:
            log.warning("Deferred rescore error: %s", exc)
        finally:
            _db.close()
    asyncio.create_task(_deferred_rescore())
    log.info("Background poller started (interval=%ds)", INGESTION_INTERVAL)
    yield
    task.cancel()
    log.info("TrustPulse v3 shutting down")


app = FastAPI(title="TrustPulse", version="0.3.0", lifespan=lifespan)

# Restrict CORS to local deployment origins only
_ALLOWED_ORIGINS = os.environ.get(
    "TRUSTPULSE_ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,http://localhost:8080,http://127.0.0.1:8080"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# Auth + system (system status is read-only but still requires auth - see api/system.py)
app.include_router(auth_router)
app.include_router(system_router)

# Protected v3 routers
app.include_router(dashboard_router)
app.include_router(cases_router)
app.include_router(evidence_router)
app.include_router(compliance_router)
app.include_router(ingestion_router)
app.include_router(reports_router)
app.include_router(admin_router)
app.include_router(privacy_user_router)
app.include_router(users_router)


@app.get("/", include_in_schema=False)
async def serve_index():
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "TrustPulse v3 API", "docs": "/docs"}


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
