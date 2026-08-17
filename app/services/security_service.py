import base64
import hashlib
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from app.config import Config

class SecurityService:
    _cipher = None

    @classmethod
    def _get_cipher(cls):
        if cls._cipher is None:
            # Derive a 32-byte URL-safe base64 key from SECRET_KEY. The salt
            # comes from ENCRYPTION_SALT (defaults to the historical value so
            # existing rows keep decrypting — set a real one on fresh DBs).
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=Config.ENCRYPTION_SALT.encode(),
                iterations=100000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(Config.SECRET_KEY.encode()))
            cls._cipher = Fernet(key)
        return cls._cipher

    @classmethod
    def encrypt(cls, plain_text):
        if not plain_text: return None
        return cls._get_cipher().encrypt(plain_text.encode()).decode()

    @classmethod
    def decrypt(cls, cipher_text):
        if not cipher_text: return None
        return cls._get_cipher().decrypt(cipher_text.encode()).decode()

    @classmethod
    def generate_blind_index(cls, text):
        """
        Creates a deterministic hash for searching.
        Even if data is encrypted differently every time, this hash remains the same
        so you can run queries like find_by_email.
        """
        if not text: return None
        return hashlib.sha256((text + Config.BLIND_INDEX_PEPPER).encode()).hexdigest()