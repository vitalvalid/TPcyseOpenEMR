from datetime import datetime, timedelta

from starlette.requests import Request

from api.cases import get_case
from api.evidence import export_case_evidence
from db.models import Case, CaseEvent, IngestionManifest, NormalizedEvent
from engine.case_engine import generate_auth_cases, generate_cases


def _make_manifest(db) -> IngestionManifest:
    manifest = IngestionManifest(
        connector_name="openemr_real",
        source_system="openemr",
        source_name="lab-openemr",
        source_row_count=3,
        inserted_count=3,
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
    return manifest


def _after_hours_rule():
    return [{
        "rule_id": "R-01",
        "rule_name": "After-Hours Access",
        "fired": True,
        "score_contribution": 20.0,
        "description": "Access outside business hours",
        "hipaa_ref": "HIPAA 45 CFR §164.312(a)(2)(i)",
    }]


def _make_event(db, *, source_log_id: int, event_time: datetime, patient_id: str, manifest_id: int):
    event = NormalizedEvent(
        source_log_id=source_log_id,
        manifest_id=manifest_id,
        event_time=event_time,
        user_id="dr_case",
        user_name="Dr Case",
        user_role="PHYSICIAN",
        event_type="patient_access",
        patient_id=patient_id,
        department="Cardiology",
        ip_address="10.0.0.10",
        hour_of_day=event_time.hour,
        day_of_week=event_time.weekday(),
        risk_score=42.0,
        risk_level="HIGH",
        triggered_rules=_after_hours_rule(),
        status="PENDING",
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/evidence/test",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def test_case_event_linkage_is_stable_and_evidence_uses_only_linked_events(db, compliance_user, auditor_user):
    manifest = _make_manifest(db)
    base_time = datetime.utcnow().replace(microsecond=0, second=0) - timedelta(days=1, hours=2)
    source_ids = [1001, 1002, 1003]
    for idx, source_id in enumerate(source_ids):
        _make_event(
            db,
            source_log_id=source_id,
            event_time=base_time + timedelta(minutes=idx),
            patient_id=f"PAT-00{idx + 1}",
            manifest_id=manifest.id,
        )

    created = generate_cases(db)
    assert created == 1

    case = db.query(Case).one()
    linked_rows = (
        db.query(CaseEvent)
        .filter(CaseEvent.case_id == case.case_id)
        .order_by(CaseEvent.normalized_event_id.asc())
        .all()
    )
    assert len(linked_rows) == 3
    assert {(row.rule_id, row.linked_reason) for row in linked_rows} == {
        ("R-01", "After-Hours Access")
    }

    created_again = generate_cases(db)
    assert created_again == 0
    assert db.query(CaseEvent).filter(CaseEvent.case_id == case.case_id).count() == 3

    _make_event(
        db,
        source_log_id=9999,
        event_time=base_time + timedelta(minutes=30),
        patient_id="PAT-999",
        manifest_id=manifest.id,
    )

    case_detail = get_case(case.case_id, db=db, _user=compliance_user)
    assert case_detail["linked_event_count"] == 3
    assert [event["source_log_id"] for event in case_detail["events"]] == source_ids
    assert case_detail["case_type"] == "PATIENT_ACCESS_REVIEW"
    assert case_detail["patient_access_assessment"]["linked_event_count"] == 3
    assert case_detail["patient_access_assessment"]["unique_patient_token_count"] == 3
    assert case_detail["event_type_breakdown"]["patient_access"] == 3
    assert "patient_id" not in case_detail["events"][0]
    assert case_detail["events"][0]["patient_token"].startswith("PT-")
    assert "PAT-001" not in case_detail["events"][0]["patient_token"]
    assert case_detail["events"][0]["event_type"] == "patient_access"
    assert case_detail["events"][0]["linked_reason"] == "After-Hours Access"
    assert case_detail["events"][0]["rule_id"] == "R-01"
    assert case_detail["events"][0]["manifest_id"] == manifest.id
    assert case_detail["events"][0]["source_username"] == "dr_case"
    assert case_detail["events"][0]["source_connector_name"] == "openemr_real"
    assert case_detail["events"][0]["source_name"] == "lab-openemr"
    assert case_detail["events"][0]["source_manifest_hash"] == "c" * 64
    assert case_detail["events"][0]["triggered_rules"][0]["rule_id"] == "R-01"
    assert case_detail["source_traceability"]["manifest_ids"] == [manifest.id]
    assert case_detail["source_traceability"]["connector_names"] == ["openemr_real"]

    response = export_case_evidence(
        case.case_id,
        http_req=_request(),
        db=db,
        current=auditor_user,
    )
    html = response.body.decode()
    for source_id in source_ids:
        assert str(source_id) in html
    assert f"Manifest #{manifest.id} via openemr_real" in html
    assert "Source Username" in html
    assert "9999" not in html


def test_auth_evidence_export_uses_only_linked_cluster_events(db, auditor_user):
    base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=21) - timedelta(days=14)
    old_ids = [2101, 2102, 2103, 2104]
    new_ids = [2201, 2202, 2203]
    for idx, source_id in enumerate(old_ids):
        db.add(
            NormalizedEvent(
                source_log_id=source_id,
                event_time=base + timedelta(minutes=idx),
                user_id="nurse_chen",
                user_name="Linda Chen",
                user_role="NURSING",
                event_type="failed_login",
                patient_id=None,
                department="ED",
                ip_address="10.0.0.11",
                hour_of_day=(base + timedelta(minutes=idx)).hour,
                day_of_week=(base + timedelta(minutes=idx)).weekday(),
                risk_score=20.0,
                risk_level="LOW",
                triggered_rules=[],
                status="PENDING",
            )
        )
    new_base = datetime.utcnow().replace(microsecond=0, second=0, minute=0, hour=23)
    for idx, source_id in enumerate(new_ids):
        db.add(
            NormalizedEvent(
                source_log_id=source_id,
                event_time=new_base + timedelta(minutes=idx),
                user_id="nurse_chen",
                user_name="Linda Chen",
                user_role="NURSING",
                event_type="failed_login",
                patient_id=None,
                department="ED",
                ip_address="10.0.0.11",
                hour_of_day=(new_base + timedelta(minutes=idx)).hour,
                day_of_week=(new_base + timedelta(minutes=idx)).weekday(),
                risk_score=20.0,
                risk_level="LOW",
                triggered_rules=[],
                status="PENDING",
            )
        )
    db.commit()

    assert generate_auth_cases(db) == 2
    latest_case = (
        db.query(Case)
        .filter(Case.user_id == "nurse_chen", Case.case_type == "AUTHENTICATION_REVIEW")
        .order_by(Case.date_start.desc())
        .first()
    )
    response = export_case_evidence(
        latest_case.case_id,
        http_req=_request(),
        db=db,
        current=auditor_user,
    )
    html = response.body.decode()
    for source_id in new_ids:
        assert str(source_id) in html
    for source_id in old_ids:
        assert str(source_id) not in html
