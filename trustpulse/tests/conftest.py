"""
Test fixtures - uses in-memory SQLite and dummy engine stubs.
No production OpenEMR connection required.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from db.models import Base, TrustPulseUser
from api.auth import hash_password

TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture
def db():
    engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def compliance_user(db):
    u = TrustPulseUser(
        email="co@clinic.test",
        hashed_password=hash_password("Password1!"),
        display_name="Compliance Officer",
        role="COMPLIANCE_OFFICER",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def auditor_user(db):
    u = TrustPulseUser(
        email="audit@clinic.test",
        hashed_password=hash_password("Password1!"),
        display_name="Auditor",
        role="AUDITOR",
        is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u
