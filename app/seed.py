from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.crypto import encrypt_str
from app.models import Hotel, Room, StaffUser
from app.auth import hash_password


def seed_demo(db: Session) -> None:
    if not settings.seed_hotel_phone_number_id:
        return

    hotel = db.execute(
        select(Hotel).where(Hotel.whatsapp_phone_number_id == settings.seed_hotel_phone_number_id)
    ).scalar_one_or_none()

    if not hotel:
        hotel = Hotel(
            name=settings.seed_hotel_name or "Demo Hotel",
            whatsapp_phone_number_id=settings.seed_hotel_phone_number_id,
            whatsapp_business_phone_e164=settings.seed_hotel_business_e164 or "",
            whatsapp_access_token_enc=encrypt_str(settings.seed_hotel_access_token or ""),
            manager_wa_e164=settings.seed_manager_e164 or "",
            sla_seconds=20,
            created_at=datetime.utcnow(),
        )
        db.add(hotel)
        db.commit()

    # Seed 5 rooms
    existing_rooms = db.execute(select(Room).where(Room.hotel_id == hotel.id)).scalars().all()
    if not existing_rooms:
        for rn in ["101", "102", "103", "104", "105"]:
            db.add(Room(hotel_id=hotel.id, room_number=rn, room_type="standard", status="active"))
        db.commit()

    # Seed manager staff user (optional)
    if settings.seed_manager_email and settings.seed_manager_password:
        existing_user = db.execute(
            select(StaffUser).where(StaffUser.hotel_id == hotel.id, StaffUser.email == settings.seed_manager_email)
        ).scalar_one_or_none()
        if not existing_user:
            db.add(StaffUser(
                hotel_id=hotel.id,
                email=settings.seed_manager_email,
                password_hash=hash_password(settings.seed_manager_password),
                role="manager",
                is_active=True,
            ))
            db.commit()
