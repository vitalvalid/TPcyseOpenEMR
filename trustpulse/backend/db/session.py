import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

TRUSTPULSE_DB_URL           = os.environ.get("TRUSTPULSE_DB_URL", "sqlite:///./trustpulse.db")
OPENEMR_DB_URL              = os.environ.get("OPENEMR_DB_URL", "")
TRUSTPULSE_PATIENT_TOKEN_SECRET = os.environ.get("TRUSTPULSE_PATIENT_TOKEN_SECRET", "")
TRUSTPULSE_JWT_SECRET       = os.environ.get("TRUSTPULSE_JWT_SECRET", "change-me-in-production")
TRUSTPULSE_MODE             = os.environ.get("TRUSTPULSE_MODE", "openemr_real")

_sqlite_args = {"check_same_thread": False} if "sqlite" in TRUSTPULSE_DB_URL else {}

tp_engine = create_engine(TRUSTPULSE_DB_URL, connect_args=_sqlite_args)
TrustPulseSession = sessionmaker(bind=tp_engine, autocommit=False, autoflush=False)


def init_db():
    Base.metadata.create_all(bind=tp_engine)


def get_tp_session():
    db = TrustPulseSession()
    try:
        yield db
    finally:
        db.close()


_openemr_engine = None


def get_openemr_engine():
    global _openemr_engine
    if _openemr_engine is None and OPENEMR_DB_URL:
        _openemr_engine = create_engine(OPENEMR_DB_URL, pool_pre_ping=True)
    return _openemr_engine


def reconnect_openemr(url: str) -> bool:
    """Replace the OpenEMR engine with a new URL. Returns True if connection succeeds."""
    global _openemr_engine
    from sqlalchemy import text
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _openemr_engine = engine
        return True
    except Exception:
        return False
