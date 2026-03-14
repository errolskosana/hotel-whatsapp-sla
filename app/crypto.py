from cryptography.fernet import Fernet
from app.config import settings


def _fernet() -> Fernet:
    return Fernet(settings.encryption_master_key.encode())


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
