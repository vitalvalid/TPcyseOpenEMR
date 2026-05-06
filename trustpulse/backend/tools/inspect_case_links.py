#!/usr/bin/env python3
"""Inspect TrustPulse case-to-event linkage from the command line."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import create_engine, func, inspect
from sqlalchemy.orm import Session, sessionmaker


SCRIPT_DIR = Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from db.models import Case, CaseEvent, NormalizedEvent  # noqa: E402

try:
    from governance.evidence import tokenize_patient_id as _tokenize_patient_id  # noqa: E402
except Exception:
    _tokenize_patient_id = None


DEFAULT_DB_URL = "sqlite:////app/data/trustpulse.db"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect TrustPulse case-to-event linkage."
    )
    parser.add_argument(
        "--title",
        help="Case title search (case-insensitive substring match).",
    )
    parser.add_argument(
        "--case-prefix",
        help="Case ID prefix to inspect.",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("TRUSTPULSE_DB_URL", DEFAULT_DB_URL),
        help=f"Database URL. Defaults to TRUSTPULSE_DB_URL or {DEFAULT_DB_URL}.",
    )
    return parser.parse_args(argv)


def _sqlite_path_from_url(db_url: str) -> Optional[Path]:
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return None
    path_str = db_url[len(prefix):]
    if not path_str or path_str == ":memory:":
        return None
    return Path(path_str)


def _build_engine(db_url: str):
    connect_args = {"check_same_thread": False} if "sqlite" in db_url else {}
    return create_engine(db_url, connect_args=connect_args)


def _ensure_schema(engine) -> Optional[str]:
    sqlite_path = _sqlite_path_from_url(str(engine.url))
    if sqlite_path and not sqlite_path.exists():
        return f"Database file not found: {sqlite_path}"

    inspector = inspect(engine)
    required_tables = ("cases", "case_events", "normalized_events")
    missing = [table for table in required_tables if not inspector.has_table(table)]
    if missing:
        return (
            "Required table(s) missing: "
            + ", ".join(missing)
            + ". Has Phase 1 linkage been migrated yet?"
        )
    return None


def _tokenize(patient_id: Optional[str]) -> str:
    if _tokenize_patient_id is None:
        return "unavailable"
    return _tokenize_patient_id(patient_id)


def _print_case_summary(db: Session) -> None:
    total_cases = db.query(func.count(Case.case_id)).scalar() or 0
    total_case_events = db.query(func.count(CaseEvent.id)).scalar() or 0

    print(f"DB URL: {db.bind.url}")
    print(f"Total cases: {total_cases}")
    print(f"Total case_events: {total_case_events}")
    print()
    print("Cases:")

    rows = (
        db.query(
            Case.case_id,
            Case.title,
            Case.event_count,
            Case.status,
            func.count(CaseEvent.id).label("linked_event_count"),
        )
        .outerjoin(CaseEvent, CaseEvent.case_id == Case.case_id)
        .group_by(
            Case.case_id,
            Case.title,
            Case.event_count,
            Case.status,
            Case.created_at,
        )
        .order_by(Case.created_at.desc(), Case.case_id.asc())
        .all()
    )

    if not rows:
        print("  (no cases found)")
        return

    for row in rows:
        title = row.title or "-"
        print(
            f"  case_id={row.case_id} | title={title} | "
            f"event_count={row.event_count or 0} | linked_event_count={row.linked_event_count or 0} | "
            f"status={row.status or '-'}"
        )


def _matching_cases(
    db: Session,
    *,
    case_prefix: Optional[str],
    title: Optional[str],
) -> list[Case]:
    query = db.query(Case)
    if case_prefix:
        query = query.filter(Case.case_id.like(f"{case_prefix}%"))
    if title:
        query = query.filter(func.lower(Case.title).contains(title.lower()))
    return query.order_by(Case.created_at.desc(), Case.case_id.asc()).all()


def _print_case_details(db: Session, cases: Iterable[Case]) -> None:
    printed_any = False
    for case in cases:
        printed_any = True
        print()
        print(f"Linked events for case_id={case.case_id} | title={case.title or '-'}")
        rows = (
            db.query(
                NormalizedEvent.source_log_id,
                NormalizedEvent.event_time,
                NormalizedEvent.event_type,
                NormalizedEvent.patient_id,
                CaseEvent.linked_reason,
                CaseEvent.rule_id,
            )
            .join(CaseEvent, CaseEvent.normalized_event_id == NormalizedEvent.id)
            .filter(CaseEvent.case_id == case.case_id)
            .order_by(NormalizedEvent.event_time.asc(), NormalizedEvent.id.asc())
            .limit(10)
            .all()
        )
        if not rows:
            print("  (no linked events found)")
            continue

        for row in rows:
            event_time = row.event_time.isoformat() if row.event_time else "-"
            print(
                f"  source_log_id={row.source_log_id} | event_time={event_time} | "
                f"event_type={row.event_type or '-'} | patient_token={_tokenize(row.patient_id)} | "
                f"linked_reason={row.linked_reason or '-'} | rule_id={row.rule_id or '-'}"
            )

    if not printed_any:
        print()
        print("No cases matched the supplied filters.")


def run(argv: Optional[list[str]] = None, *, stdout: Callable[[str], None] = print) -> int:
    args = parse_args(argv)
    engine = _build_engine(args.db_url)
    error = _ensure_schema(engine)
    if error:
        stdout(f"ERROR: {error}")
        return 1

    session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = session_factory()
    try:
        _print_case_summary(db)
        if args.case_prefix or args.title:
            cases = _matching_cases(db, case_prefix=args.case_prefix, title=args.title)
            _print_case_details(db, cases)
        return 0
    finally:
        db.close()


def main(argv: Optional[list[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
