"""Small password hashing helpers for the local teacher account flow."""
from __future__ import annotations

import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 and a random salt."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    """Compare a password against its stored hash."""
    try:
        algorithm, salt, expected = encoded.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 310_000).hex()
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False
