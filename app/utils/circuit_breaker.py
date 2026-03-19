"""
Circuit Breaker Pattern for AI Provider Reliability

Tracks provider failures and temporarily stops sending requests to failing
providers, giving them time to recover.

States:
- CLOSED: Normal operation, requests pass through
- OPEN: Too many failures, requests blocked
- HALF_OPEN: Testing if provider has recovered
"""
import time
import logging
from enum import Enum
from typing import Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Provider is down, blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""
    failure_threshold: int = 5  # Open circuit after N failures
    success_threshold: int = 2  # Close circuit after N successes in HALF_OPEN
    timeout_seconds: int = 60  # Time to wait before trying HALF_OPEN


class CircuitBreaker:
    """
    Circuit breaker for a single AI provider.

    Usage:
        breaker = CircuitBreaker("openai")

        if breaker.can_request():
            try:
                result = call_api()
                breaker.record_success()
            except Exception as e:
                breaker.record_failure()
        else:
            # Circuit is open, use fallback
            pass
    """

    def __init__(self, provider_name: str, config: Optional[CircuitBreakerConfig] = None):
        """
        Initialize circuit breaker.

        Args:
            provider_name: Name of the provider (e.g., "openai", "anthropic")
            config: Circuit breaker configuration
        """
        self.provider_name = provider_name
        self.config = config or CircuitBreakerConfig()

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.opened_at: Optional[float] = None

    def can_request(self) -> bool:
        """
        Check if requests are allowed through the circuit.

        Returns:
            bool: True if requests should proceed
        """
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has elapsed
            if self.opened_at and (time.time() - self.opened_at) >= self.config.timeout_seconds:
                logger.info(f"🔄 Circuit breaker for {self.provider_name}: OPEN → HALF_OPEN (testing recovery)")
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                return True
            else:
                logger.warning(f"⛔ Circuit breaker for {self.provider_name}: OPEN - blocking request")
                return False

        if self.state == CircuitState.HALF_OPEN:
            # Allow request to test if provider has recovered
            return True

        return False

    def record_success(self):
        """Record a successful API call."""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            logger.info(f"✅ {self.provider_name} success in HALF_OPEN ({self.success_count}/{self.config.success_threshold})")

            if self.success_count >= self.config.success_threshold:
                logger.info(f"✅ Circuit breaker for {self.provider_name}: HALF_OPEN → CLOSED (recovered)")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                self.opened_at = None

        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            if self.failure_count > 0:
                logger.debug(f"✅ {self.provider_name} success - resetting failure count")
                self.failure_count = 0

    def record_failure(self):
        """Record a failed API call."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            logger.warning(f"❌ {self.provider_name} failed in HALF_OPEN - reopening circuit")
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            self.success_count = 0

        elif self.state == CircuitState.CLOSED:
            logger.warning(f"❌ {self.provider_name} failure {self.failure_count}/{self.config.failure_threshold}")

            if self.failure_count >= self.config.failure_threshold:
                logger.error(f"⛔ Circuit breaker for {self.provider_name}: CLOSED → OPEN (too many failures)")
                self.state = CircuitState.OPEN
                self.opened_at = time.time()

    def get_state(self) -> Dict[str, any]:
        """
        Get current circuit breaker state.

        Returns:
            dict: Current state information
        """
        return {
            "provider": self.provider_name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "opened_at": datetime.fromtimestamp(self.opened_at).isoformat() if self.opened_at else None,
            "can_request": self.can_request()
        }

    def reset(self):
        """Reset circuit breaker to initial state."""
        logger.info(f"🔄 Resetting circuit breaker for {self.provider_name}")
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.opened_at = None


class CircuitBreakerManager:
    """
    Manages circuit breakers for all AI providers.

    Singleton pattern for global state.
    """

    _instance = None
    _breakers: Dict[str, CircuitBreaker] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_breaker(self, provider_name: str) -> CircuitBreaker:
        """
        Get circuit breaker for a provider.

        Args:
            provider_name: Provider name

        Returns:
            CircuitBreaker: Circuit breaker instance
        """
        if provider_name not in self._breakers:
            self._breakers[provider_name] = CircuitBreaker(provider_name)
            logger.info(f"🔌 Created circuit breaker for {provider_name}")

        return self._breakers[provider_name]

    def get_all_states(self) -> Dict[str, Dict]:
        """
        Get states of all circuit breakers.

        Returns:
            dict: All circuit breaker states
        """
        return {
            name: breaker.get_state()
            for name, breaker in self._breakers.items()
        }

    def reset_all(self):
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            breaker.reset()
        logger.info("🔄 All circuit breakers reset")


# Global instance
def get_circuit_breaker_manager() -> CircuitBreakerManager:
    """Get global circuit breaker manager."""
    return CircuitBreakerManager()
