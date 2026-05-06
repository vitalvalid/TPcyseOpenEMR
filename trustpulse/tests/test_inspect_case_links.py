from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, Case, CaseEvent, IngestionManifest, NormalizedEvent
from tools.inspect_case_links import run


def test_inspect_case_links_prints_summary_and_linked_events(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "trustpulse.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("TRUSTPULSE_DB_URL", db_url)

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    manifest = IngestionManifest(
        connector_name="openemr_real",
        source_system="openemr",
        source_name="lab-openemr",
        source_row_count=1,
        inserted_count=1,
        source_batch_sha256="a" * 64,
        normalized_batch_sha256="b" * 64,
        previous_manifest_hash="0" * 64,
        manifest_hash="c" * 64,
        status="SUCCESS",
        started_at=datetime(2026, 1, 2, 0, 0, 0),
        completed_at=datetime(2026, 1, 2, 0, 5, 0),
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)

    event = NormalizedEvent(
        source_log_id=1001,
        manifest_id=manifest.id,
        event_time=datetime(2026, 1, 2, 22, 0, 0),
        user_id="dr_case",
        user_name="Dr Case",
        user_role="PHYSICIAN",
        event_type="patient_access",
        patient_id="PAT-001",
        department="Cardiology",
        ip_address="10.0.0.10",
        hour_of_day=22,
        day_of_week=4,
        risk_score=42.0,
        risk_level="HIGH",
        triggered_rules=[],
        status="PENDING",
    )
    db.add(event)

    case = Case(
        case_id="e5cb8ae1-case-0001",
        title="David Ross After Hours",
        severity="P1_HIGH",
        pattern_type="after_hours_access",
        user_id="dr_case",
        user_name="Dr Case",
        event_count=1,
        date_start=event.event_time,
        date_end=event.event_time,
        risk_score=42.0,
        recommended_action="Review",
        breach_risk=False,
        status="OPEN",
        created_at=datetime(2026, 1, 2, 22, 5, 0),
    )
    db.add(case)
    db.commit()
    db.refresh(event)

    db.add(
        CaseEvent(
            case_id=case.case_id,
            normalized_event_id=event.id,
            linked_reason="After-Hours Access",
            rule_id="R-01",
        )
    )
    db.commit()
    db.close()

    rc = run(["--title", "David Ross"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Total cases: 1" in out
    assert "Total case_events: 1" in out
    assert "case_id=e5cb8ae1-case-0001" in out
    assert "linked_event_count=1" in out
    assert "source_log_id=1001" in out
    assert "linked_reason=After-Hours Access" in out
    assert "rule_id=R-01" in out
    assert "patient_token=PT-" in out
    assert "PAT-001" not in out


def test_inspect_case_links_fails_gracefully_when_case_events_table_missing(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "missing_case_events.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("TRUSTPULSE_DB_URL", db_url)

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[Case.__table__, NormalizedEvent.__table__],
    )

    rc = run([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "Required table(s) missing: case_events" in out
