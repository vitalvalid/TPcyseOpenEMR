"""
TrustPulse authentication and RBAC.

Roles:
  COMPLIANCE_OFFICER - review/disposition/export
  AUDITOR            - read-only review and export
  SECURITY_ADMIN     - connector config and trigger ingestion
"""
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import TrustPulseUser
from db.session import get_tp_session, TRUSTPULSE_JWT_SECRET

router = APIRouter(prefix="/api/auth", tags=["auth"])

_pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer   = HTTPBearer(auto_error=False)
_ALGO     = "HS256"
_EXP_HRS  = 8

ROLE_PERMISSIONS = {
    "TRUSTPULSE_ADMIN":   {"review", "disposition", "export", "breach_assessment",
                           "configure", "trigger_ingestion"},
    "COMPLIANCE_OFFICER": {"review", "disposition", "export", "breach_assessment"},
    "AUDITOR":            {"review", "export"},
    "SECURITY_ADMIN":     {"configure", "trigger_ingestion", "review"},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


def create_token(user: TrustPulseUser) -> str:
    expire = datetime.utcnow() + timedelta(hours=_EXP_HRS)
    payload = {
        "sub":   str(user.id),
        "email": user.email,
        "role":  user.role,
        "name":  user.display_name or user.email,
        "exp":   expire,
    }
    return jwt.encode(payload, TRUSTPULSE_JWT_SECRET, algorithm=_ALGO)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, TRUSTPULSE_JWT_SECRET, algorithms=[_ALGO])


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_tp_session),
) -> TrustPulseUser:
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = _decode_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    user = db.get(TrustPulseUser, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def require_permission(permission: str):
    async def checker(current: TrustPulseUser = Depends(get_current_user)):
        if permission not in ROLE_PERMISSIONS.get(current.role, set()):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current.role}' does not have permission '{permission}'",
            )
        return current
    return checker


# ── Bootstrap ─────────────────────────────────────────────────────────────────

_SAMPLE_USERS = [
    ("compliance@trustpulse.local", "Comply@2026!",  "Dr. Sarah Chen",  "COMPLIANCE_OFFICER"),
    ("auditor@trustpulse.local",    "Audit@2026!",   "James Park",       "AUDITOR"),
    ("security@trustpulse.local",   "Secure@2026!",  "Alex Navarro",     "SECURITY_ADMIN"),
]


def bootstrap_sample_users(db: Session) -> None:
    for email, password, name, role in _SAMPLE_USERS:
        if not db.query(TrustPulseUser).filter(TrustPulseUser.email == email).first():
            db.add(TrustPulseUser(
                email=email,
                hashed_password=hash_password(password),
                display_name=name,
                role=role,
                is_active=True,
            ))
    db.commit()


def bootstrap_admin(db: Session) -> None:
    email    = os.environ.get("TRUSTPULSE_ADMIN_EMAIL", "")
    password = os.environ.get("TRUSTPULSE_ADMIN_PASSWORD", "")
    if not email or not password:
        return
    existing = db.query(TrustPulseUser).filter(TrustPulseUser.email == email).first()
    if existing:
        # Upgrade role if it was created with the old default
        if existing.role != "TRUSTPULSE_ADMIN":
            existing.role = "TRUSTPULSE_ADMIN"
            db.commit()
        return
    db.add(TrustPulseUser(
        email           = email,
        hashed_password = hash_password(password),
        display_name    = "Admin",
        role            = "TRUSTPULSE_ADMIN",
        is_active       = True,
    ))
    db.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email:    str
    password: str


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_tp_session)):
    user = db.query(TrustPulseUser).filter(TrustPulseUser.email == req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    user.last_login = datetime.utcnow()
    db.commit()
    return {
        "access_token": create_token(user),
        "token_type":   "bearer",
        "expires_in":   _EXP_HRS * 3600,
        "user": {
            "id":           user.id,
            "email":        user.email,
            "display_name": user.display_name,
            "role":         user.role,
            "permissions":  list(ROLE_PERMISSIONS.get(user.role, set())),
        },
    }


@router.get("/me")
def me(current: TrustPulseUser = Depends(get_current_user)):
    return {
        "id":           current.id,
        "email":        current.email,
        "display_name": current.display_name,
        "role":         current.role,
        "permissions":  list(ROLE_PERMISSIONS.get(current.role, set())),
        "last_login":   current.last_login.isoformat() if current.last_login else None,
    }
