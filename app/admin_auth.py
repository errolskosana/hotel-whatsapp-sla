"""JWT authentication for the super-admin portal.

Uses a separate HTTP-only cookie ('admin_token') with a 'scope: superadmin'
claim so that super-admin sessions cannot be confused with staff sessions.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = "HS256"
_ADMIN_COOKIE = "admin_token"


def create_admin_token(admin_id: str, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    payload = {
        "sub": admin_id,
        "email": email,
        "scope": "superadmin",
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_admin_token(token: str) -> Optional[dict]:
    try:
        p = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
        if p.get("scope") != "superadmin":
            return None
        return p
    except JWTError:
        return None


class CurrentAdmin:
    def __init__(self, admin_id: str, email: str):
        self.admin_id = admin_id
        self.email = email


def get_current_admin(request: Request) -> CurrentAdmin:
    token = request.cookies.get(_ADMIN_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/admin/login"},
        )
    payload = decode_admin_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/admin/login"},
        )
    return CurrentAdmin(admin_id=payload["sub"], email=payload["email"])
