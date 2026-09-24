"""Symmetric encryption for the few secrets that must live in the database.

The key stays in the environment (ENGINE_ENCRYPTION_KEY, a Fernet key); only
ciphertext is stored. Nothing here is logged. Credentials that CAN stay in
the environment still do — this is for the desk-managed vendor registry,
where an operator adds a provider from a screen.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from engine.settings import get_settings


class SecretsUnavailable(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise SecretsUnavailable("ENGINE_ENCRYPTION_KEY is not set; stored secrets cannot be used")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise SecretsUnavailable("stored secret does not decrypt with the configured key") from e
