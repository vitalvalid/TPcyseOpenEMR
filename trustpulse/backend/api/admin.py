"""
TrustPulse admin API - user management, platform settings, data privacy.
All endpoints require TRUSTPULSE_ADMIN role.
"""
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import TrustPulseUser, PlatformSetting, DataPrivacyConfig, DataAccessRequest
from db.session import get_tp_session, OPENEMR_DB_URL, reconnect_openemr
from api.auth import get_current_user, hash_password, ROLE_PERMISSIONS

router = APIRouter(prefix="/api/admin", tags=["admin"])

VALID_ROLES = list(ROLE_PERMISSIONS.keys())

MASKABLE_FIELDS = {
    "patient_id": "Patient ID",
    "ip_address":  "IP Address",
    "user_name":   "User Full Name",
    "user_id":     "User Login ID",
}
MASK = "●●●●●●●●"

ACCESS_GRANT_ROLES = {"TRUSTPULSE_ADMIN", "COMPLIANCE_OFFICER"}
ACCESS_DURATION_HOURS = 24


# ── Auth guard ────────────────────────────────────────────────────────────────

def _require_admin(current: TrustPulseUser = Depends(get_current_user)):
    if current.role != "TRUSTPULSE_ADMIN":
        raise HTTPException(status_code=403, detail="Admin role required")
    return current


# ── Privacy helpers (used by other modules) ───────────────────────────────────

def get_privacy_state(db: Session) -> tuple[bool, list]:
    """Returns (module_enabled, obfuscated_fields)."""
    cfg = db.get(DataPrivacyConfig, 1)
    if not cfg or not cfg.module_enabled:
        return False, []
    return True, cfg.obfuscated_fields or []


def user_has_privacy_access(email: str, db: Session) -> bool:
    now = datetime.utcnow()
    return (
        db.query(DataAccessRequest)
        .filter(
            DataAccessRequest.requester_email == email,
            DataAccessRequest.status == "APPROVED",
            DataAccessRequest.expires_at > now,
        )
        .first()
    ) is not None


def mask_dict(data: dict, fields: list) -> dict:
    out = dict(data)
    for f in fields:
        if f in out and out[f] is not None:
            out[f] = MASK
    return out


# ── User management ───────────────────────────────────────────────────────────

def _user_out(u: TrustPulseUser) -> dict:
    return {
        "id":           u.id,
        "email":        u.email,
        "display_name": u.display_name,
        "role":         u.role,
        "is_active":    u.is_active,
        "created_at":   u.created_at.isoformat() if u.created_at else None,
        "last_login":   u.last_login.isoformat() if u.last_login else None,
        "permissions":  sorted(ROLE_PERMISSIONS.get(u.role, set())),
    }


@router.get("/users")
def list_users(
    db: Session = Depends(get_tp_session),
    _admin: TrustPulseUser = Depends(_require_admin),
):
    users = db.query(TrustPulseUser).order_by(TrustPulseUser.created_at).all()
    return {"users": [_user_out(u) for u in users]}


@router.get("/permission-groups")
def permission_groups(_admin: TrustPulseUser = Depends(_require_admin)):
    return {
        "groups": [
            {
                "role": role,
                "permissions": sorted(perms),
                "description": {
                    "TRUSTPULSE_ADMIN":   "Full platform access - user management, configuration, all case operations",
                    "COMPLIANCE_OFFICER": "Case review, disposition, breach assessment, evidence export",
                    "AUDITOR":            "Read-only case review and evidence export; no case disposition",
                    "SECURITY_ADMIN":     "Connector configuration and manual ingestion trigger; read-only case access",
                }.get(role, ""),
            }
            for role, perms in ROLE_PERMISSIONS.items()
        ]
    }


class CreateUserRequest(BaseModel):
    email:        str
    password:     str
    display_name: Optional[str] = None
    role:         str


@router.post("/users", status_code=201)
def create_user(
    req: CreateUserRequest,
    db: Session = Depends(get_tp_session),
    _admin: TrustPulseUser = Depends(_require_admin),
):
    if req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Valid: {VALID_ROLES}")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.query(TrustPulseUser).filter(TrustPulseUser.email == req.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = TrustPulseUser(
        email=req.email,
        hashed_password=hash_password(req.password),
        display_name=req.display_name or req.email.split("@")[0],
        role=req.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user)


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    role:         Optional[str] = None
    is_active:    Optional[bool] = None
    password:     Optional[str] = None


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    req: UpdateUserRequest,
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    user = db.get(TrustPulseUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and req.is_active is False:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    if req.role and req.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role. Valid: {VALID_ROLES}")
    if req.display_name is not None:
        user.display_name = req.display_name
    if req.role is not None:
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active
    if req.password:
        if len(req.password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
        user.hashed_password = hash_password(req.password)
    db.commit()
    db.refresh(user)
    return _user_out(user)


# ── Platform settings ─────────────────────────────────────────────────────────

SETTING_KEYS = {
    "openemr_db_url":             "OpenEMR Database URL",
    "clinic_name":                "Clinic Name",
    "ingestion_interval_seconds": "Ingestion Interval (seconds)",
}

DEFAULT_SETTINGS = {
    "openemr_db_url":             OPENEMR_DB_URL or "",
    "clinic_name":                "OpenEMR Lab Clinic",
    "ingestion_interval_seconds": "60",
}


def _get_setting(key: str, db: Session) -> str:
    row = db.get(PlatformSetting, key)
    return row.value if row else DEFAULT_SETTINGS.get(key, "")


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_tp_session),
    _admin: TrustPulseUser = Depends(_require_admin),
):
    return {
        k: {
            "label": SETTING_KEYS[k],
            "value": _get_setting(k, db),
            "is_sensitive": "url" in k or "password" in k,
        }
        for k in SETTING_KEYS
    }


class SaveSettingsRequest(BaseModel):
    openemr_db_url:             Optional[str] = None
    clinic_name:                Optional[str] = None
    ingestion_interval_seconds: Optional[str] = None


@router.put("/settings")
def save_settings(
    req: SaveSettingsRequest,
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    data = req.dict(exclude_none=True)
    for key, value in data.items():
        if key not in SETTING_KEYS:
            continue
        row = db.get(PlatformSetting, key)
        if row:
            row.value = value
            row.updated_at = datetime.utcnow()
            row.updated_by = admin.email
        else:
            db.add(PlatformSetting(key=key, value=value, updated_by=admin.email))
    db.commit()
    return {"saved": list(data.keys())}


@router.post("/settings/test-connection")
def test_connection(
    req: SaveSettingsRequest,
    _admin: TrustPulseUser = Depends(_require_admin),
):
    url = req.openemr_db_url
    if not url:
        raise HTTPException(status_code=400, detail="openemr_db_url is required")
    ok = reconnect_openemr(url)
    return {"connected": ok, "url": url}


@router.post("/settings/apply-connection")
def apply_connection(
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    url = _get_setting("openemr_db_url", db)
    if not url:
        raise HTTPException(status_code=400, detail="No openemr_db_url saved")
    ok = reconnect_openemr(url)
    return {"connected": ok, "url_applied": url}


# ── Data Privacy ──────────────────────────────────────────────────────────────

@router.get("/privacy/config")
def get_privacy_config(
    db: Session = Depends(get_tp_session),
    _admin: TrustPulseUser = Depends(_require_admin),
):
    cfg = db.get(DataPrivacyConfig, 1)
    if not cfg:
        return {
            "module_enabled": False,
            "obfuscated_fields": [],
            "maskable_fields": MASKABLE_FIELDS,
        }
    return {
        "module_enabled":    cfg.module_enabled,
        "obfuscated_fields": cfg.obfuscated_fields or [],
        "maskable_fields":   MASKABLE_FIELDS,
        "updated_at":        cfg.updated_at.isoformat() if cfg.updated_at else None,
        "updated_by":        cfg.updated_by,
    }


class PrivacyConfigRequest(BaseModel):
    module_enabled:    bool
    obfuscated_fields: List[str] = []


@router.put("/privacy/config")
def save_privacy_config(
    req: PrivacyConfigRequest,
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    invalid = [f for f in req.obfuscated_fields if f not in MASKABLE_FIELDS]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown fields: {invalid}")
    cfg = db.get(DataPrivacyConfig, 1)
    if cfg:
        cfg.module_enabled    = req.module_enabled
        cfg.obfuscated_fields = req.obfuscated_fields
        cfg.updated_at        = datetime.utcnow()
        cfg.updated_by        = admin.email
    else:
        db.add(DataPrivacyConfig(
            id=1,
            module_enabled=req.module_enabled,
            obfuscated_fields=req.obfuscated_fields,
            updated_by=admin.email,
        ))
    db.commit()
    return {"module_enabled": req.module_enabled, "obfuscated_fields": req.obfuscated_fields}


@router.get("/privacy/requests")
def list_access_requests(
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    now = datetime.utcnow()
    reqs = (
        db.query(DataAccessRequest)
        .order_by(DataAccessRequest.created_at.desc())
        .all()
    )
    def _req_out(r):
        expired = (r.status == "APPROVED" and r.expires_at and r.expires_at < now)
        return {
            "id":              r.id,
            "requester_email": r.requester_email,
            "requester_role":  r.requester_role,
            "reason":          r.reason,
            "status":          "EXPIRED" if expired else r.status,
            "granted_by":      r.granted_by,
            "granted_at":      r.granted_at.isoformat() if r.granted_at else None,
            "expires_at":      r.expires_at.isoformat() if r.expires_at else None,
            "denied_reason":   r.denied_reason,
            "created_at":      r.created_at.isoformat() if r.created_at else None,
        }
    return {"requests": [_req_out(r) for r in reqs]}


class DecideRequest(BaseModel):
    decision:      str    # APPROVED or DENIED
    denied_reason: Optional[str] = None
    duration_hours: int = ACCESS_DURATION_HOURS


@router.patch("/privacy/requests/{req_id}/decide")
def decide_request(
    req_id: int,
    body: DecideRequest,
    db: Session = Depends(get_tp_session),
    admin: TrustPulseUser = Depends(_require_admin),
):
    if body.decision not in ("APPROVED", "DENIED"):
        raise HTTPException(status_code=400, detail="decision must be APPROVED or DENIED")
    r = db.get(DataAccessRequest, req_id)
    if not r:
        raise HTTPException(status_code=404, detail="Request not found")
    if r.status != "PENDING":
        raise HTTPException(status_code=400, detail=f"Request is already {r.status}")
    r.status      = body.decision
    r.granted_by  = admin.email
    r.granted_at  = datetime.utcnow()
    r.denied_reason = body.denied_reason
    if body.decision == "APPROVED":
        r.expires_at = datetime.utcnow() + timedelta(hours=body.duration_hours)
    db.commit()
    return {"id": r.id, "status": r.status, "expires_at": r.expires_at.isoformat() if r.expires_at else None}


# ── User-facing privacy endpoints (any authenticated user) ────────────────────

privacy_user_router = APIRouter(prefix="/api/privacy", tags=["privacy"])


@privacy_user_router.get("/status")
def privacy_status(
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(get_current_user),
):
    enabled, fields = get_privacy_state(db)
    has_access = user_has_privacy_access(current.email, db) or current.role == "TRUSTPULSE_ADMIN"
    pending = (
        db.query(DataAccessRequest)
        .filter(
            DataAccessRequest.requester_email == current.email,
            DataAccessRequest.status == "PENDING",
        )
        .first()
    )
    return {
        "module_enabled":      enabled,
        "obfuscated_fields":   fields,
        "has_access":          has_access,
        "pending_request":     pending is not None,
        "field_labels":        MASKABLE_FIELDS,
    }


class AccessRequestBody(BaseModel):
    reason: str


@privacy_user_router.post("/request")
def submit_access_request(
    body: AccessRequestBody,
    db: Session = Depends(get_tp_session),
    current: TrustPulseUser = Depends(get_current_user),
):
    if not body.reason or len(body.reason.strip()) < 10:
        raise HTTPException(status_code=400, detail="Please provide a reason (at least 10 characters)")
    existing_pending = (
        db.query(DataAccessRequest)
        .filter(
            DataAccessRequest.requester_email == current.email,
            DataAccessRequest.status == "PENDING",
        )
        .first()
    )
    if existing_pending:
        raise HTTPException(status_code=409, detail="You already have a pending access request")
    r = DataAccessRequest(
        requester_email=current.email,
        requester_role=current.role,
        reason=body.reason.strip(),
        status="PENDING",
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "status": r.status}
