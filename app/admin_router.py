"""Super-admin portal routes (/admin/*)."""
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.admin_auth import CurrentAdmin, create_admin_token, get_current_admin
from app.auth import hash_password, verify_password
from app.control_plane import SuperAdmin, TenantHotel, cp_session
from app.crypto import encrypt_str, decrypt_str
from app.db_provisioner import provision_tenant_db
from app.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

_ADMIN_COOKIE = "admin_token"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    if request.cookies.get(_ADMIN_COOKIE):
        return RedirectResponse("/admin/hotels", status_code=302)
    return templates.TemplateResponse("admin/login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def admin_login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    cp=Depends(cp_session),
):
    if cp is None:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Super-admin portal is not configured."},
            status_code=503,
        )

    admin = cp.execute(
        select(SuperAdmin).where(SuperAdmin.email == email.strip(), SuperAdmin.is_active == True)
    ).scalar_one_or_none()

    if not admin or not verify_password(password, admin.password_hash):
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "error": "Invalid email or password."},
            status_code=401,
        )

    from app.config import settings
    token = create_admin_token(str(admin.id), admin.email)
    response = RedirectResponse("/admin/hotels", status_code=302)
    response.set_cookie(
        _ADMIN_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings.app_env != "dev",
        max_age=settings.jwt_expiry_hours * 3600,
    )
    return response


@router.post("/logout")
def admin_logout():
    from app.config import settings
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(_ADMIN_COOKIE, httponly=True, samesite="strict",
                           secure=settings.app_env != "dev")
    return response


# ---------------------------------------------------------------------------
# Hotel list
# ---------------------------------------------------------------------------

@router.get("/hotels", response_class=HTMLResponse)
def admin_hotels(
    request: Request,
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503, "Control plane not configured.")
    tenants = cp.execute(
        select(TenantHotel).order_by(TenantHotel.display_name)
    ).scalars().all()
    return templates.TemplateResponse("admin/hotels.html", {
        "request": request,
        "admin": admin,
        "tenants": tenants,
    })


# ---------------------------------------------------------------------------
# Create hotel
# ---------------------------------------------------------------------------

@router.get("/hotels/new", response_class=HTMLResponse)
def admin_hotel_new_page(
    request: Request,
    admin: CurrentAdmin = Depends(get_current_admin),
):
    return templates.TemplateResponse("admin/hotel_form.html", {
        "request": request,
        "admin": admin,
        "tenant": None,
        "error": None,
    })


@router.post("/hotels/new", response_class=HTMLResponse)
def admin_hotel_new_submit(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(...),
    db_url: str = Form(...),
    whatsapp_phone_number_id: str = Form(...),
    brand_name: str = Form(""),
    brand_color_primary: str = Form("#1d4ed8"),
    brand_color_sidebar: str = Form("#0f2340"),
    brand_tagline: str = Form(""),
    brand_logo_url: str = Form(""),
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503, "Control plane not configured.")

    slug = slug.strip().lower().replace(" ", "-")

    # Check uniqueness
    existing = cp.execute(
        select(TenantHotel).where(TenantHotel.slug == slug)
    ).scalar_one_or_none()
    if existing:
        return templates.TemplateResponse("admin/hotel_form.html", {
            "request": request,
            "admin": admin,
            "tenant": None,
            "error": f"Slug '{slug}' is already taken.",
        }, status_code=400)

    try:
        provision_tenant_db(db_url)
    except Exception as exc:
        log.error("provision_tenant_db_failed", slug=slug, error=str(exc))
        return templates.TemplateResponse("admin/hotel_form.html", {
            "request": request,
            "admin": admin,
            "tenant": None,
            "error": f"DB provisioning failed: {exc}",
        }, status_code=500)

    tenant = TenantHotel(
        slug=slug,
        display_name=display_name.strip(),
        db_url_enc=encrypt_str(db_url),
        whatsapp_phone_number_id=whatsapp_phone_number_id.strip(),
        brand_name=brand_name.strip() or None,
        brand_color_primary=brand_color_primary.strip() or None,
        brand_color_sidebar=brand_color_sidebar.strip() or None,
        brand_tagline=brand_tagline.strip() or None,
        brand_logo_url=brand_logo_url.strip() or None,
    )
    cp.add(tenant)
    cp.commit()
    log.info("tenant_created", slug=slug, tenant_id=str(tenant.id))
    return RedirectResponse(f"/admin/hotels/{tenant.id}", status_code=302)


# ---------------------------------------------------------------------------
# Hotel detail + edit
# ---------------------------------------------------------------------------

@router.get("/hotels/{tenant_id}", response_class=HTMLResponse)
def admin_hotel_detail(
    tenant_id: str,
    request: Request,
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503)
    tenant = cp.get(TenantHotel, uuid.UUID(tenant_id))
    if not tenant:
        raise HTTPException(404)
    return templates.TemplateResponse("admin/hotel_detail.html", {
        "request": request,
        "admin": admin,
        "tenant": tenant,
        "msg": None,
    })


@router.post("/hotels/{tenant_id}/edit", response_class=HTMLResponse)
def admin_hotel_edit(
    tenant_id: str,
    request: Request,
    display_name: str = Form(...),
    brand_name: str = Form(""),
    brand_color_primary: str = Form("#1d4ed8"),
    brand_color_sidebar: str = Form("#0f2340"),
    brand_tagline: str = Form(""),
    brand_logo_url: str = Form(""),
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503)
    tenant = cp.get(TenantHotel, uuid.UUID(tenant_id))
    if not tenant:
        raise HTTPException(404)

    tenant.display_name = display_name.strip()
    tenant.brand_name = brand_name.strip() or None
    tenant.brand_color_primary = brand_color_primary.strip() or None
    tenant.brand_color_sidebar = brand_color_sidebar.strip() or None
    tenant.brand_tagline = brand_tagline.strip() or None
    tenant.brand_logo_url = brand_logo_url.strip() or None
    cp.commit()

    # Evict cached engine so next login picks up potential branding changes
    from app.tenant_db import evict_tenant_cache
    evict_tenant_cache(tenant_id)

    return templates.TemplateResponse("admin/hotel_detail.html", {
        "request": request,
        "admin": admin,
        "tenant": tenant,
        "msg": "Hotel updated successfully.",
    })


@router.post("/hotels/{tenant_id}/toggle")
def admin_hotel_toggle(
    tenant_id: str,
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503)
    tenant = cp.get(TenantHotel, uuid.UUID(tenant_id))
    if not tenant:
        raise HTTPException(404)
    tenant.is_active = not tenant.is_active
    cp.commit()
    return RedirectResponse(f"/admin/hotels/{tenant_id}", status_code=302)


# ---------------------------------------------------------------------------
# Super-admin user management
# ---------------------------------------------------------------------------

@router.get("/admins", response_class=HTMLResponse)
def admin_admins_page(
    request: Request,
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503)
    admins = cp.execute(select(SuperAdmin).order_by(SuperAdmin.email)).scalars().all()
    return templates.TemplateResponse("admin/admins.html", {
        "request": request,
        "admin": admin,
        "admins": admins,
        "msg": None,
    })


@router.post("/admins/add", response_class=HTMLResponse)
def admin_add_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    admin: CurrentAdmin = Depends(get_current_admin),
    cp=Depends(cp_session),
):
    if cp is None:
        raise HTTPException(503)
    existing = cp.execute(
        select(SuperAdmin).where(SuperAdmin.email == email.strip())
    ).scalar_one_or_none()
    if not existing:
        cp.add(SuperAdmin(
            email=email.strip(),
            password_hash=hash_password(password),
        ))
        cp.commit()
    return RedirectResponse("/admin/admins", status_code=302)
