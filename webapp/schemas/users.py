"""User domain schema. No broker credentials here -- those live on Account
(webapp/schemas/accounts.py), since a user may hold accounts on several
brokers with unrelated credential shapes.
"""
from __future__ import annotations

from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str
    password: str      # plaintext in; caller hashes before the ORM write
    is_admin: bool = False
