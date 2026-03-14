from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "dev"
    database_url: str

    # WhatsApp / Meta
    meta_app_secret: str
    whatsapp_verify_token: str

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
