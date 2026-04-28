"""
Tests for authentication, RBAC, and permission enforcement.
"""
import pytest
from api.auth import (
    hash_password, verify_password, create_token,
    ROLE_PERMISSIONS,
)
from db.models import TrustPulseUser


class TestPasswordHashing:
    def test_hash_verify_roundtrip(self):
        plain  = "SuperSecret42!"
        hashed = hash_password(plain)
        assert verify_password(plain, hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)

    def test_hash_is_not_plaintext(self):
        plain  = "mypassword"
        hashed = hash_password(plain)
        assert plain not in hashed


class TestRolePermissions:
    def test_compliance_officer_can_disposition(self):
        assert "disposition" in ROLE_PERMISSIONS["COMPLIANCE_OFFICER"]

    def test_auditor_cannot_disposition(self):
        assert "disposition" not in ROLE_PERMISSIONS["AUDITOR"]

    def test_auditor_can_export(self):
        assert "export" in ROLE_PERMISSIONS["AUDITOR"]

    def test_security_admin_can_trigger_ingestion(self):
        assert "trigger_ingestion" in ROLE_PERMISSIONS["SECURITY_ADMIN"]

    def test_security_admin_cannot_disposition(self):
        assert "disposition" not in ROLE_PERMISSIONS["SECURITY_ADMIN"]


class TestTokenGeneration:
    def test_token_is_string(self, compliance_user):
        token = create_token(compliance_user)
        assert isinstance(token, str)
        assert len(token) > 20

    def test_different_users_get_different_tokens(self, compliance_user, auditor_user):
        t1 = create_token(compliance_user)
        t2 = create_token(auditor_user)
        assert t1 != t2
