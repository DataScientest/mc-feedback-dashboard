"""
Encryption at rest for stored credentials (Fernet: AES-128-CBC + HMAC).

The key comes from the CREDENTIALS_KEY environment variable. If it is
missing, a new key is generated and appended to the project's .env file so
that restarts and scheduled jobs keep working without manual setup.
"""

import os
import logging

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401  (InvalidToken re-exported)

log = logging.getLogger(__name__)

ENV_VAR = "CREDENTIALS_KEY"
ENV_FILE = ".env"


def _get_or_create_key() -> bytes:
    key = os.getenv(ENV_VAR)
    if key:
        return key.encode()

    key_bytes = Fernet.generate_key()
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{ENV_VAR}={key_bytes.decode()}\n")
    os.environ[ENV_VAR] = key_bytes.decode()
    log.warning("%s manquante : nouvelle clé générée et ajoutée à %s", ENV_VAR, ENV_FILE)
    return key_bytes


def encrypt(data: bytes) -> bytes:
    return Fernet(_get_or_create_key()).encrypt(data)


def decrypt(token: bytes) -> bytes:
    """Raises InvalidToken if the content is not valid Fernet ciphertext."""
    return Fernet(_get_or_create_key()).decrypt(token)
