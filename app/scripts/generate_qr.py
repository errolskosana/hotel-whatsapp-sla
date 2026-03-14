import os
from urllib.parse import quote
import qrcode

from sqlalchemy import select
from app.db import SessionLocal
from app.models import Hotel, Room


def build_wa_link(hotel_id: str, room_number: str, hotel_business_e164: str) -> str:
    prefilled = f"START HOTEL_ID={hotel_id} ROOM={room_number}"
    return f"https://wa.me/{hotel_business_e164}?text={quote(prefilled)}"


def main():
    out_dir = os.path.join(os.getcwd(), "demo_qr")
    os.makedirs(out_dir, exist_ok=True)

    db = SessionLocal()
    try:
        hotel = db.execute(select(Hotel).limit(1)).scalar_one_or_none()
        if not hotel:
            raise SystemExit("No hotel found. Set SEED_* vars and start the app once, or run migrations + seed.")

        rooms = db.execute(select(Room).where(Room.hotel_id == hotel.id).order_by(Room.room_number)).scalars().all()
        if not rooms:
            raise SystemExit("No rooms found in DB (seed should have created 5).")

        for r in rooms:
            link = build_wa_link(str(hotel.id), r.room_number, hotel.whatsapp_business_phone_e164)
            img = qrcode.make(link)
            path = os.path.join(out_dir, f"room_{r.room_number}.png")
            img.save(path)
            print("Wrote", path, "->", link)
    finally:
        db.close()


if __name__ == "__main__":
    main()
