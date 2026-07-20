"""Symmetric encryption for secrets at rest (cTrader tokens/secrets).

Fernet (AES-128-CBC + HMAC) keyed by APP_SECRET_KEY from the environment. Only
ciphertext is ever stored in the DB. Rotating APP_SECRET_KEY invalidates stored
secrets (re-enter them), which is the intended fail-safe.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.environ.get("APP_SECRET_KEY")
    if not key:
        raise RuntimeError("APP_SECRET_KEY is not set — required to (de)crypt secrets")
    digest = hashlib.sha256(key.encode()).digest()          # 32 bytes
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    if not token:
        return None
    return _fernet().decrypt(token.encode()).decode()
