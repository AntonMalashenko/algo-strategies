"""Password hashing for the multi-user login (stdlib PBKDF2-HMAC-SHA256).

No third-party dependency; format: pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITER = 200_000


def hash_password(password: str, iterations: int = _ITER) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False
