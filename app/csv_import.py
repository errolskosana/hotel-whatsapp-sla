import csv
import uuid
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import GuestStay


def import_guest_stays_csv(db: Session, hotel_id: uuid.UUID, file_obj) -> int:
    """
    CSV headers required:
      guest_name, arrival_date, departure_date, room_number
    Optional:
      guest_phone, language, reservation_id
    Dates: YYYY-MM-DD
    """
    reader = csv.DictReader((line.decode("utf-8") for line in file_obj))
    count = 0
    for row in reader:
        stay = GuestStay(
            hotel_id=hotel_id,
            guest_name=row["guest_name"].strip(),
            arrival_date=datetime.strptime(row["arrival_date"], "%Y-%m-%d").date(),
            departure_date=datetime.strptime(row["departure_date"], "%Y-%m-%d").date(),
            room_number=row["room_number"].strip(),
            guest_phone=(row.get("guest_phone") or "").strip() or None,
            language=(row.get("language") or "").strip() or None,
            reservation_id=(row.get("reservation_id") or "").strip() or None,
        )
        db.add(stay)
        count += 1
    db.commit()
    return count
