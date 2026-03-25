"""JWT-based authentication replacing global basic auth.

Tokens are stored in HTTP-only, SameSite=Strict cookies.
Token payload: {sub: str(staff_user_id), hotel_id: str, role: str, exp: int}
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import bcrypt
from jose import JWTError, jwt
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.config import settings
from app.db import db_session

ALGORITHM = "HS256"


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(
    staff_user_id: str,
    hotel_id: str,
    role: str,
    email: str = "",
    tenant_id: str | None = None,
    brand_name: str = "",
    brand_color_primary: str = "",
    brand_color_sidebar: str = "",
    brand_tagline: str = "",
) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expiry_hours)
    payload = {
        "sub": staff_user_id,
        "hotel_id": hotel_id,
        "role": role,
        "email": email,
        "exp": expire,
        "tenant_id": tenant_id,
        "brand_name": brand_name,
        "brand_color_primary": brand_color_primary,
        "brand_color_sidebar": brand_color_sidebar,
        "brand_tagline": brand_tagline,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

class CurrentUser:
    def __init__(
        self,
        staff_user_id: str,
        hotel_id: str,
        role: str,
        email: str = "",
        tenant_id: str | None = None,
        brand_name: str = "",
        brand_color_primary: str = "",
        brand_color_sidebar: str = "",
        brand_tagline: str = "",
    ):
        self.staff_user_id = staff_user_id
        self.hotel_id = hotel_id
        self.role = role
        self.email = email
        self.tenant_id = tenant_id
        self.brand_name = brand_name
        self.brand_color_primary = brand_color_primary
        self.brand_color_sidebar = brand_color_sidebar
        self.brand_tagline = brand_tagline

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"


def _get_token_from_cookie(request: Request) -> Optional[str]:
    return request.cookies.get("access_token")


def get_current_user(request: Request) -> CurrentUser:
    token = _get_token_from_cookie(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    return CurrentUser(
        staff_user_id=payload["sub"],
        hotel_id=payload["hotel_id"],
        role=payload["role"],
        email=payload.get("email", ""),
        tenant_id=payload.get("tenant_id"),
        brand_name=payload.get("brand_name", ""),
        brand_color_primary=payload.get("brand_color_primary", ""),
        brand_color_sidebar=payload.get("brand_color_sidebar", ""),
        brand_tagline=payload.get("brand_tagline", ""),
    )


def require_manager(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_manager:
        raise HTTPException(status_code=403, detail="Manager role required")
    return user


def authenticate_user(db: Session, hotel_id: str, email: str, password: str) -> Optional[object]:
    import uuid as _uuid
    from app.models import StaffUser
    try:
        hotel_uuid = _uuid.UUID(hotel_id)
    except ValueError:
        return None
    user = db.execute(
        select(StaffUser).where(
            StaffUser.hotel_id == hotel_uuid,
            StaffUser.email == email,
            StaffUser.is_active == True,
        )
    ).scalar_one_or_none()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
