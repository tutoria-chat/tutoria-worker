"""
KeyManager Service - Provider API key rotation and failover

Manages multiple API keys per AI provider with round-robin rotation,
automatic failure tracking, cooldown periods, and in-memory caching
to minimize database queries.
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple

from sqlalchemy.orm import Session

from app.core.encryption import decrypt_api_key
from app.models.provider_key import ProviderKey

logger = logging.getLogger(__name__)

# Cache TTL in seconds
CACHE_TTL_SECONDS = 60

# Cooldown duration after failures
COOLDOWN_SECONDS = 300  # 5 minutes

# Max failures before cooldown
MAX_FAILURES_BEFORE_COOLDOWN = 3


class KeyManager:
    """
    Service for managing provider API keys with round-robin rotation and failover.

    Features:
        - Round-robin key rotation across active keys for a provider
        - Automatic failure tracking with cooldown periods
        - In-memory cache to avoid hitting the database on every request
        - Cache auto-refresh after configurable TTL (default 60 seconds)

    Usage:
        key_manager = KeyManager(db)
        api_key = key_manager.get_next_key("openai")
        if api_key:
            try:
                # Use the key for API call
                ...
                key_manager.mark_key_success("openai", key_id)
            except Exception:
                key_manager.mark_key_failed("openai", key_id)
    """

    def __init__(self, db: Session):
        self._db = db
        self._cache: Dict[str, List[Tuple[int, str]]] = {}  # Provider -> [(key_id, decrypted_key)]
        self._cache_time: Optional[float] = None
        self._round_robin_index: Dict[str, int] = {}  # Provider -> current index

    def get_next_key(self, provider: str) -> Optional[Tuple[int, str]]:
        """
        Get the next available API key for a provider using round-robin rotation.

        Keys in cooldown or inactive are skipped. Returns None if no keys
        are available.

        Args:
            provider: The AI provider name (e.g., 'openai', 'anthropic', 'bedrock').

        Returns:
            Optional[Tuple[int, str]]: A tuple of (key_id, decrypted_api_key),
            or None if no active keys are available.
        """
        self._refresh_cache_if_stale()

        keys = self._cache.get(provider, [])
        if not keys:
            logger.warning(f"No active API keys found for provider: {provider}")
            return None

        # Get the current round-robin index
        current_index = self._round_robin_index.get(provider, 0)

        # Try each key starting from current index
        for i in range(len(keys)):
            idx = (current_index + i) % len(keys)
            key_id, decrypted_key = keys[idx]

            # Check if key is in cooldown
            if self._is_in_cooldown(key_id):
                continue

            # Advance round-robin index for next call
            self._round_robin_index[provider] = (idx + 1) % len(keys)

            # Update last_used_at in database
            self._update_last_used(key_id)

            logger.debug(f"Selected key id={key_id} for provider={provider}")
            return key_id, decrypted_key

        logger.warning(f"All keys for provider '{provider}' are in cooldown")
        return None

    def get_next_aws_credentials(self) -> Optional[Tuple[int, str, str, Optional[str]]]:
        """
        Get the next available AWS credentials for Bedrock.

        Bedrock keys are stored as JSON: {"accessKeyId": "...", "secretAccessKey": "..."}
        Region is stored in the ProviderKey.region column.

        Returns:
            Optional[Tuple[int, str, str, Optional[str]]]:
            (key_id, access_key_id, secret_access_key, region) or None
        """
        import json

        result = self.get_next_key("bedrock")
        if not result:
            return None

        key_id, raw_key = result

        # Try parsing as JSON (new format with access_key_id + secret_access_key)
        try:
            creds = json.loads(raw_key)
            access_key_id = creds.get("accessKeyId", "")
            secret_access_key = creds.get("secretAccessKey", "")
            if access_key_id and secret_access_key:
                # Get region from the ProviderKey record
                pk = self._db.query(ProviderKey).filter(ProviderKey.id == key_id).first()
                region = pk.region if pk else None
                logger.info(f"Using DB-managed AWS credentials (key_id={key_id}, region={region})")
                return key_id, access_key_id, secret_access_key, region
        except (json.JSONDecodeError, AttributeError):
            pass

        logger.warning(f"Bedrock key id={key_id} is not in JSON credentials format")
        return None

    def mark_key_failed(self, provider: str, key_id: int) -> None:
        """
        Mark a key as failed and apply cooldown if threshold is exceeded.

        Increments the failure count and sets a cooldown period if the key
        has failed too many times consecutively.

        Args:
            provider: The AI provider name.
            key_id: The database ID of the failed key.
        """
        now = datetime.now(timezone.utc)
        key = self._db.query(ProviderKey).filter(ProviderKey.id == key_id).first()
        if not key:
            logger.error(f"ProviderKey id={key_id} not found for failure marking")
            return

        key.failure_count = (key.failure_count or 0) + 1
        key.last_failure_at = now

        if key.failure_count >= MAX_FAILURES_BEFORE_COOLDOWN:
            key.cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)
            logger.warning(
                f"Key id={key_id} for provider={provider} placed in cooldown "
                f"until {key.cooldown_until} after {key.failure_count} failures"
            )

        self._db.commit()

        # Invalidate cache so next call picks up updated state
        self._invalidate_cache()

    def mark_key_success(self, provider: str, key_id: int) -> None:
        """
        Reset failure count on successful API call.

        Clears the failure counter and cooldown for a key after a
        successful use.

        Args:
            provider: The AI provider name.
            key_id: The database ID of the successful key.
        """
        key = self._db.query(ProviderKey).filter(ProviderKey.id == key_id).first()
        if not key:
            logger.error(f"ProviderKey id={key_id} not found for success marking")
            return

        if key.failure_count > 0 or key.cooldown_until is not None:
            key.failure_count = 0
            key.cooldown_until = None
            key.last_failure_at = None
            self._db.commit()
            logger.debug(f"Key id={key_id} for provider={provider} failure state reset")

    def _refresh_cache_if_stale(self) -> None:
        """Reload keys from the database if cache is stale (older than CACHE_TTL_SECONDS)."""
        now = time.monotonic()
        if self._cache_time is not None and (now - self._cache_time) < CACHE_TTL_SECONDS:
            return

        self._refresh_cache()

    def _refresh_cache(self) -> None:
        """
        Reload all active keys from the database, decrypt them, and populate the cache.

        Keys are sorted by priority (ascending) so higher-priority keys are tried first.
        """
        logger.debug("Refreshing provider key cache from database")

        keys = (
            self._db.query(ProviderKey)
            .filter(ProviderKey.is_active == True)  # noqa: E712
            .order_by(ProviderKey.provider, ProviderKey.priority)
            .all()
        )

        new_cache: Dict[str, List[Tuple[int, str]]] = {}
        for key in keys:
            try:
                decrypted = decrypt_api_key(key.encrypted_api_key)
                provider = key.provider
                if provider not in new_cache:
                    new_cache[provider] = []
                new_cache[provider].append((key.id, decrypted))
            except ValueError as e:
                logger.error(f"Failed to decrypt key id={key.id} ({key.key_name}): {e}")
                continue

        self._cache = new_cache
        self._cache_time = time.monotonic()

        for provider, provider_keys in new_cache.items():
            logger.info(f"Cached {len(provider_keys)} active key(s) for provider={provider}")

    def _invalidate_cache(self) -> None:
        """Force cache refresh on the next get_next_key call."""
        self._cache_time = None

    def _is_in_cooldown(self, key_id: int) -> bool:
        """
        Check if a key is currently in cooldown.

        Args:
            key_id: The database ID of the key to check.

        Returns:
            bool: True if the key is in cooldown and should be skipped.
        """
        key = self._db.query(ProviderKey).filter(ProviderKey.id == key_id).first()
        if not key or not key.cooldown_until:
            return False

        now = datetime.now(timezone.utc)
        if key.cooldown_until > now:
            return True

        # Cooldown has expired - clear it
        key.cooldown_until = None
        self._db.commit()
        return False

    def _update_last_used(self, key_id: int) -> None:
        """
        Update the last_used_at timestamp for a key.

        Args:
            key_id: The database ID of the key being used.
        """
        key = self._db.query(ProviderKey).filter(ProviderKey.id == key_id).first()
        if key:
            key.last_used_at = datetime.now(timezone.utc)
            self._db.commit()
