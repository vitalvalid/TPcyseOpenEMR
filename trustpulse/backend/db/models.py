from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Text,
    ForeignKey, JSON, Boolean, BigInteger,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


# ── Provenance models (defined first - FK targets) ────────────────────────────

class IngestionManifest(Base):
    __tablename__ = "ingestion_manifests"

    id                       = Column(Integer, primary_key=True, index=True)
    connector_name           = Column(String(50), nullable=False)
    source_system            = Column(String(50), nullable=False, default="openemr")
    source_name              = Column(String(200))
    source_min_id            = Column(BigInteger)
    source_max_id            = Column(BigInteger)
    source_row_count         = Column(Integer, default=0)
    inserted_count           = Column(Integer, default=0)
    duplicate_count          = Column(Integer, default=0)
    parse_error_count        = Column(Integer, default=0)
    source_batch_sha256      = Column(String(64))
    normalized_batch_sha256  = Column(String(64))
    previous_manifest_hash   = Column(String(64))
    manifest_hash            = Column(String(64))
    gap_detected             = Column(Boolean, default=False)
    gap_ranges_json          = Column(JSON)
    started_at               = Column(DateTime, default=datetime.utcnow)
    completed_at             = Column(DateTime, nullable=True)
    status                   = Column(String(20), default="IN_PROGRESS")
    error_message            = Column(Text, nullable=True)


class RawAuditEvent(Base):
    __tablename__ = "raw_audit_events"

    id                       = Column(Integer, primary_key=True, index=True)
    manifest_id              = Column(Integer, ForeignKey("ingestion_manifests.id"), nullable=False)
    source_system            = Column(String(50), nullable=False)
    connector_name           = Column(String(50), nullable=False)
    source_log_id            = Column(String(100), nullable=False)
    event_time               = Column(DateTime, nullable=False)
    source_payload_hash      = Column(String(64), nullable=False)
    source_payload_minimized = Column(JSON)
    received_at              = Column(DateTime, default=datetime.utcnow)


class IngestionError(Base):
    __tablename__ = "ingestion_errors"

    id                     = Column(Integer, primary_key=True, index=True)
    manifest_id            = Column(Integer, ForeignKey("ingestion_manifests.id"), nullable=False)
    source_log_id          = Column(String(100))
    error_type             = Column(String(50))
    error_message          = Column(Text)
    raw_payload_minimized  = Column(JSON)
    created_at             = Column(DateTime, default=datetime.utcnow)


# ── Auth models ───────────────────────────────────────────────────────────────

class TrustPulseUser(Base):
    __tablename__ = "trustpulse_users"

    id              = Column(Integer, primary_key=True, index=True)
    email           = Column(String(200), unique=True, nullable=False, index=True)
    hashed_password = Column(String(200), nullable=False)
    display_name    = Column(String(100))
    # COMPLIANCE_OFFICER / AUDITOR / SECURITY_ADMIN
    role            = Column(String(50), nullable=False)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    last_login      = Column(DateTime, nullable=True)


# ── Core event and baseline models ───────────────────────────────────────────

class NormalizedEvent(Base):
    __tablename__ = "normalized_events"

    id              = Column(Integer, primary_key=True, index=True)
    source_log_id   = Column(Integer, unique=True, nullable=False)
    manifest_id     = Column(Integer, ForeignKey("ingestion_manifests.id"), nullable=True)
    ingested_at     = Column(DateTime, default=datetime.utcnow)
    event_time      = Column(DateTime, nullable=False)
    user_id         = Column(String(50), nullable=False, index=True)
    user_name       = Column(String(100))
    user_role       = Column(String(50))
    event_type      = Column(String(50))
    patient_id      = Column(String(50), nullable=True)
    department      = Column(String(100), nullable=True)
    ip_address      = Column(String(45), nullable=True)
    hour_of_day     = Column(Integer)
    day_of_week     = Column(Integer)
    risk_score      = Column(Float, default=0.0)
    risk_level      = Column(String(10), default="LOW")
    triggered_rules = Column(JSON, default=list)
    status          = Column(String(20), default="PENDING", index=True)


class Disposition(Base):
    __tablename__ = "dispositions"

    id          = Column(Integer, primary_key=True, index=True)
    event_id    = Column(Integer, ForeignKey("normalized_events.id"), nullable=False)
    reviewer    = Column(String(100))
    action      = Column(String(20))
    notes       = Column(Text)
    reviewed_at = Column(DateTime, default=datetime.utcnow)


class UserBaseline(Base):
    __tablename__ = "user_baselines"

    user_id              = Column(String(50), primary_key=True)
    avg_daily_accesses   = Column(Float, default=0.0)
    std_daily_accesses   = Column(Float, default=0.0)
    typical_hours_start  = Column(Integer, default=8)
    typical_hours_end    = Column(Integer, default=18)
    avg_unique_patients  = Column(Float, default=0.0)
    departments_seen     = Column(JSON, default=list)
    known_ips            = Column(JSON, default=list)
    last_updated         = Column(DateTime, default=datetime.utcnow)
    # COLD_START / TRAINING / ACTIVE / DEGRADED / LOCKED
    maturity             = Column(String(20), default="COLD_START")
    training_event_count = Column(Integer, default=0)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id              = Column(Integer, primary_key=True, index=True)
    run_at          = Column(DateTime, default=datetime.utcnow)
    events_ingested = Column(Integer, default=0)
    events_scored   = Column(Integer, default=0)
    highest_risk    = Column(Float, default=0.0)
    status          = Column(String(20), default="SUCCESS")
    error_message   = Column(Text, nullable=True)


# ── Case models ───────────────────────────────────────────────────────────────

class Case(Base):
    __tablename__ = "cases"

    case_id             = Column(String(36), primary_key=True)
    title               = Column(String(200))
    severity            = Column(String(20), index=True)
    pattern_type        = Column(String(50))
    user_id             = Column(String(50), index=True)
    user_name           = Column(String(100))
    event_count         = Column(Integer, default=0)
    date_start          = Column(DateTime)
    date_end            = Column(DateTime)
    risk_score          = Column(Float, default=0.0)
    recommended_action  = Column(String(50))
    breach_risk         = Column(Boolean, default=False)
    breach_deadline     = Column(DateTime, nullable=True)
    status              = Column(String(20), default="OPEN", index=True)
    suppression_reason  = Column(Text, nullable=True)
    suppression_expires = Column(DateTime, nullable=True)
    hipaa_provisions    = Column(JSON, default=list)
    snoozed_until       = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    resolved_at         = Column(DateTime, nullable=True)
    is_demo             = Column(Boolean, default=False)


class CaseAction(Base):
    """Append-only audit trail of every action taken on a case."""
    __tablename__ = "case_actions"

    id              = Column(Integer, primary_key=True, index=True)
    case_id         = Column(String(36), ForeignKey("cases.case_id"), nullable=False, index=True)
    actor_user_id   = Column(String(50), nullable=False)
    actor_email     = Column(String(200), nullable=False)
    actor_role      = Column(String(50), nullable=False)
    action          = Column(String(50), nullable=False)
    previous_status = Column(String(20))
    new_status      = Column(String(20))
    reason_code     = Column(String(50))
    notes           = Column(Text)
    source_ip       = Column(String(45))
    user_agent      = Column(String(500))
    created_at      = Column(DateTime, default=datetime.utcnow)
    previous_hash   = Column(String(64), nullable=False, default="0" * 64)
    record_hash     = Column(String(64))


class BreachAssessment(Base):
    __tablename__ = "breach_assessments"

    id                = Column(Integer, primary_key=True, index=True)
    case_id           = Column(String(36), ForeignKey("cases.case_id"), nullable=False)
    q1_unauthorized   = Column(String(20))
    q2_acquired       = Column(String(20))
    q3_disclosed      = Column(String(20))
    factor1_score     = Column(Integer, default=3)
    factor2_score     = Column(Integer, default=3)
    factor4_mitigated = Column(Boolean, default=False)
    determination     = Column(String(20))
    ocr_deadline      = Column(DateTime, nullable=True)
    completed_by      = Column(String(100))
    completed_at      = Column(DateTime, default=datetime.utcnow)
    notes             = Column(Text, nullable=True)


class UserTrustScore(Base):
    __tablename__ = "user_trust_scores"

    user_id              = Column(String(50), primary_key=True)
    trust_score          = Column(Float, default=75.0)
    score_history        = Column(JSON, default=list)
    last_computed        = Column(DateTime, default=datetime.utcnow)
    confirmed_violations = Column(Integer, default=0)


class KnownPattern(Base):
    __tablename__ = "known_patterns"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(String(50), index=True)
    pattern_type  = Column(String(50))
    approved_by   = Column(String(100))
    approval_date = Column(DateTime, default=datetime.utcnow)
    reason        = Column(Text)
    expires_at    = Column(DateTime)
    active        = Column(Boolean, default=True)


class ComplianceCalendarItem(Base):
    __tablename__ = "compliance_calendar"

    id              = Column(Integer, primary_key=True, index=True)
    item_type       = Column(String(50))
    hipaa_provision = Column(String(100))
    last_completed  = Column(DateTime, nullable=True)
    next_due        = Column(DateTime)
    status          = Column(String(20))
    notes           = Column(Text, nullable=True)


class ScheduledReport(Base):
    __tablename__ = "scheduled_reports"

    id                 = Column(Integer, primary_key=True, index=True)
    name               = Column(String(200), nullable=False)
    frequency          = Column(String(20), nullable=False)   # WEEKLY / MONTHLY / QUARTERLY / CUSTOM
    frequency_days     = Column(Integer, default=7)
    recipient_email    = Column(String(200), nullable=True)
    is_active          = Column(Boolean, default=True)
    created_by         = Column(String(200))
    created_at         = Column(DateTime, default=datetime.utcnow)
    last_run_at        = Column(DateTime, nullable=True)
    next_run_at        = Column(DateTime, nullable=True)
    include_period_days = Column(Integer, default=7)


class ReportRun(Base):
    __tablename__ = "report_runs"

    id             = Column(Integer, primary_key=True, index=True)
    schedule_id    = Column(Integer, ForeignKey("scheduled_reports.id"), nullable=True)
    schedule_name  = Column(String(200))
    report_type    = Column(String(50))   # WEEKLY / MONTHLY / QUARTERLY / ADHOC
    period_days    = Column(Integer, default=7)
    generated_at   = Column(DateTime, default=datetime.utcnow)
    generated_by   = Column(String(200))
    status         = Column(String(20), default="SUCCESS")
    html_content   = Column(Text, nullable=True)
    file_size_bytes = Column(Integer, default=0)
    error_message  = Column(Text, nullable=True)
    email_sent_to  = Column(String(200), nullable=True)
    email_status   = Column(String(20), default="SKIPPED")   # SENT / SKIPPED / FAILED


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key        = Column(String(100), primary_key=True)
    value      = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(200))


class DataPrivacyConfig(Base):
    __tablename__ = "data_privacy_config"

    id                = Column(Integer, primary_key=True)
    module_enabled    = Column(Boolean, default=False)
    obfuscated_fields = Column(JSON, default=list)
    updated_at        = Column(DateTime, default=datetime.utcnow)
    updated_by        = Column(String(200))


class DataAccessRequest(Base):
    __tablename__ = "data_access_requests"

    id              = Column(Integer, primary_key=True, index=True)
    requester_email = Column(String(200), nullable=False, index=True)
    requester_role  = Column(String(50))
    reason          = Column(Text, nullable=False)
    status          = Column(String(20), default="PENDING")  # PENDING / APPROVED / DENIED
    granted_by      = Column(String(200))
    granted_at      = Column(DateTime, nullable=True)
    expires_at      = Column(DateTime, nullable=True)
    denied_reason   = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)


class DemoScenarioRun(Base):
    """Metadata about a demo scenario generator run. NOT case evidence."""
    __tablename__ = "demo_scenario_runs"

    id                        = Column(Integer, primary_key=True, index=True)
    scenario_id               = Column(String(50), nullable=False)
    scenario_name             = Column(String(100))
    run_by                    = Column(String(100))
    started_at                = Column(DateTime, default=datetime.utcnow)
    completed_at              = Column(DateTime, nullable=True)
    openemr_actions_attempted = Column(Integer, default=0)
    expected_log_types        = Column(JSON)
    status                    = Column(String(20), default="RUNNING")
    notes                     = Column(Text, nullable=True)
