"""
Rate Limiting - Prevent abuse and manage load

Implements sliding window rate limiting per module and per student.
"""
import time
import logging
from typing import Dict, Tuple
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Sliding window rate limiter.

    Tracks requests per key (module_id or student_id) and enforces limits.
    """

    def __init__(self, requests_per_minute: int = 100):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests allowed per minute
        """
        self.requests_per_minute = requests_per_minute
        self.window_seconds = 60
        self.requests: Dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str) -> Tuple[bool, int]:
        """
        Check if a request is allowed.

        Args:
            key: Unique identifier (e.g., "module:123" or "student:456")

        Returns:
            Tuple[bool, int]: (is_allowed, retry_after_seconds)
        """
        now = time.time()
        window_start = now - self.window_seconds

        # Get request timestamps for this key
        timestamps = self.requests[key]

        # Remove timestamps outside the window
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()

        # Check if limit exceeded
        if len(timestamps) >= self.requests_per_minute:
            # Calculate when the oldest request will expire
            oldest_timestamp = timestamps[0]
            retry_after = int(oldest_timestamp + self.window_seconds - now) + 1
            logger.warning(f"🚫 Rate limit exceeded for {key}: {len(timestamps)}/{self.requests_per_minute} requests")
            return (False, retry_after)

        # Allow the request
        timestamps.append(now)
        return (True, 0)

    def get_stats(self, key: str) -> Dict[str, any]:
        """
        Get rate limit stats for a key.

        Args:
            key: Unique identifier

        Returns:
            dict: Stats including current count and limit
        """
        now = time.time()
        window_start = now - self.window_seconds
        timestamps = self.requests[key]

        # Count requests in current window
        current_count = sum(1 for ts in timestamps if ts >= window_start)

        return {
            "key": key,
            "current_count": current_count,
            "limit": self.requests_per_minute,
            "window_seconds": self.window_seconds,
            "remaining": max(0, self.requests_per_minute - current_count)
        }

    def reset(self, key: str):
        """
        Reset rate limit for a key.

        Args:
            key: Unique identifier to reset
        """
        if key in self.requests:
            del self.requests[key]
            logger.info(f"🔄 Reset rate limit for {key}")


# Global rate limiters
_module_limiter = None
_student_limiter = None


def get_module_rate_limiter() -> RateLimiter:
    """Get rate limiter for modules."""
    global _module_limiter
    if _module_limiter is None:
        from app.core.config import settings
        limit = settings.RATE_LIMIT_PER_MODULE if hasattr(settings, 'RATE_LIMIT_PER_MODULE') else 100
        _module_limiter = RateLimiter(requests_per_minute=limit)
    return _module_limiter


def get_student_rate_limiter() -> RateLimiter:
    """Get rate limiter for students."""
    global _student_limiter
    if _student_limiter is None:
        _student_limiter = RateLimiter(requests_per_minute=30)  # Students get lower limit
    return _student_limiter
