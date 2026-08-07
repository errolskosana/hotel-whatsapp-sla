from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "dev"
    database_url: str

    # WhatsApp / Meta
    meta_app_secret: str
    whatsapp_verify_token: str
    whatsapp_api_version: str = "v25.0"
    # WhatsApp Business Account ID. Not used for sending (that keys off
    # phone_number_id) — needed for template management via
    # /{waba_id}/message_templates.
    whatsapp_business_account_id: str | None = None
    whatsapp_timeout_seconds: float = 15.0
    # Retries on transient Meta failures (429/5xx/network); 0 disables them.
    whatsapp_max_retries: int = 2
    # Send a read receipt for each inbound guest message.
    whatsapp_mark_read: bool = True
    # Approved template used to alert a manager whose 24h window has closed.
    # Must accept 3 body params: hotel name, room/guest label, message excerpt.
    whatsapp_escalation_template: str | None = None
    whatsapp_template_language: str = "en"

    # Encryption
    encryption_master_key: str

    # Web Push
    vapid_subject: str
    vapid_public_key: str
    vapid_private_key: str

    # JWT auth (replaces basic auth)
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_hours: int = 8

    # Redis (used by Celery)
    redis_url: str = "redis://localhost:6379/0"

    # Multi-tenant control plane (optional — omit for single-tenant mode)
    control_plane_db_url: str | None = None
    seed_superadmin_email: str | None = None
    seed_superadmin_password: str | None = None
    # Slug used to register the demo hotel in the control plane (e.g. "demo-hotel")
    seed_hotel_slug: str | None = None

    # Seed demo hotel
    seed_hotel_name: str | None = None
    seed_hotel_phone_number_id: str | None = None
    seed_hotel_business_e164: str | None = None
    seed_hotel_access_token: str | None = None
    seed_manager_e164: str | None = None

    seed_manager_email: str | None = None
    seed_manager_password: str | None = None

    class Config:
        env_file = ".env"


settings = Settings()

if settings.app_env == "prod" and settings.jwt_secret == "change-me-in-production":
    raise RuntimeError("JWT_SECRET must be set to a strong secret in production (app_env=prod)")
