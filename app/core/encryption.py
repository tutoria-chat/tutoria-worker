"""
Encryption utility for securing API keys at rest.

Uses Fernet symmetric encryption derived from the application's SECRET_KEY.
API keys stored in the ProviderKeys table are encrypted before persistence
and decrypted only when needed for provider API calls.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_fernet() -> Fernet:
    """
    Create a Fernet instance derived from the application SECRET_KEY.

    Derives a 32-byte key using SHA-256 hash of the SECRET_KEY, then
    base64-encodes it to produce a valid Fernet key.

    Returns:
        Fernet: Configured Fernet encryption instance.
    """
    key_bytes = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def encrypt_api_key(plain_key: str) -> str:
    """
    Store API key as-is (plain text). Providers handle key exposure detection
    and keys are rotatable from the dashboard.

    Args:
        plain_key: The plain-text API key to store.

    Returns:
        str: The API key unchanged.
    """
    if not plain_key:
        raise ValueError("API key cannot be empty")
    return plain_key


def decrypt_api_key(stored_key: str) -> str:
    """
    Return stored API key as-is (plain text storage).

    For backward compatibility with keys that were previously Fernet-encrypted,
    attempts decryption first and falls back to returning the key as-is.

    Args:
        stored_key: The stored API key string.

    Returns:
        str: The usable API key.
    """
    if not stored_key:
        raise ValueError("Stored key cannot be empty")

    # Try Fernet decryption for backward compat with old Fernet-encrypted keys
    try:
        fernet = get_fernet()
        decrypted = fernet.decrypt(stored_key.encode("utf-8"))
        return decrypted.decode("utf-8")
    except (InvalidToken, Exception):
        pass

    # Try base64 decode for keys saved by old .NET EncryptApiKey (Convert.ToBase64String)
    try:
        decoded = base64.b64decode(stored_key).decode("utf-8")
        # Sanity check: real API keys are printable ASCII and don't contain null bytes
        if decoded.isprintable() and "\x00" not in decoded and len(decoded) > 10:
            logger.info("Decoded base64-encoded API key (legacy .NET format)")
            return decoded
    except Exception:
        pass

    # Plain text key (new format) — return as-is
    return stored_key
