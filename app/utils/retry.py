"""
Retry decorator with exponential backoff and jitter.

This module provides retry logic for handling transient failures in API calls,
especially useful for dealing with rate limits, network issues, and temporary
service unavailability.
"""
import asyncio
import random
from functools import wraps
from typing import Callable, Tuple, Type
import logging

logger = logging.getLogger(__name__)


def retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[int, Exception], None] = None
):
    """
    Retry decorator with exponential backoff and jitter.

    Retries the decorated function on specified exceptions with:
    - Exponential backoff: delay = base_delay * (2 ** retry_count)
    - Jitter: random 0-10% added to delay to avoid thundering herd
    - Max delay cap to prevent excessive waiting

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay in seconds (default: 10.0)
        exceptions: Tuple of exception types to catch and retry (default: all Exception)
        on_retry: Optional callback function(retry_count, exception) called on each retry

    Returns:
        Decorated async function with retry logic

    Usage:
        @retry_with_backoff(max_retries=3, base_delay=1.0, exceptions=(RateLimitError, APITimeoutError))
        async def my_api_call():
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0

            while retries < max_retries:
                try:
                    # Attempt the function call
                    return await func(*args, **kwargs)

                except exceptions as e:
                    retries += 1

                    # Check if this is a rate limit error (429)
                    is_429 = is_rate_limit_error(e)

                    # If we've exhausted all retries, re-raise the exception
                    if retries >= max_retries:
                        if is_429:
                            logger.error(
                                f"❌ Max retries ({max_retries}) exceeded for {func.__name__} due to RATE LIMITING. "
                                f"Consider fallback or queueing."
                            )
                        else:
                            logger.error(
                                f"❌ Max retries ({max_retries}) exceeded for {func.__name__} "
                                f"after {retries} attempts. Last error: {type(e).__name__}: {e}"
                            )
                        raise

                    # Calculate delay with exponential backoff
                    # Use LONGER delays for 429 errors (they need more time to recover)
                    if is_429:
                        delay = min(base_delay * 2 * (2 ** (retries - 1)), max_delay * 2)  # 2x longer for 429
                        logger.warning(f"🚦 RATE LIMIT detected - using extended backoff")
                    else:
                        delay = min(base_delay * (2 ** (retries - 1)), max_delay)

                    # Add jitter (0-10% of delay) to avoid thundering herd
                    jitter = random.uniform(0, delay * 0.1)
                    total_delay = delay + jitter

                    # Log the retry attempt
                    if is_429:
                        logger.warning(
                            f"🚦 Retry {retries}/{max_retries} for {func.__name__} "
                            f"after {total_delay:.2f}s delay (RATE LIMIT). "
                        )
                    else:
                        logger.warning(
                            f"⚠️ Retry {retries}/{max_retries} for {func.__name__} "
                            f"after {total_delay:.2f}s delay. "
                            f"Error: {type(e).__name__}: {str(e)[:100]}"
                        )

                    # Call optional callback
                    if on_retry:
                        try:
                            on_retry(retries, e)
                        except Exception as callback_error:
                            logger.error(f"Error in retry callback: {callback_error}")

                    # Wait before retrying
                    await asyncio.sleep(total_delay)

            # This should never be reached, but just in case
            raise Exception(f"Unexpected: retry loop exited for {func.__name__}")

        return wrapper
    return decorator


def is_rate_limit_error(exception: Exception) -> bool:
    """
    Detect if an exception is a 429 rate limit error.

    Args:
        exception: The exception to check

    Returns:
        bool: True if this is a 429 rate limit error
    """
    error_message = str(exception).lower()
    error_type = type(exception).__name__.lower()

    # Check error message
    if "429" in error_message:
        return True

    if "rate" in error_message and "limit" in error_message:
        return True

    if "ratelimit" in error_type:
        return True

    # Check for provider-specific rate limit exceptions
    if "ratelimiterror" in error_type:
        return True

    return False


def should_retry(exception: Exception) -> bool:
    """
    Determine if an exception is retryable.

    Retryable errors are typically transient:
    - Rate limiting (429)
    - Server errors (5xx)
    - Network timeouts
    - Connection errors

    Non-retryable errors are permanent:
    - Authentication failures (401, 403)
    - Invalid requests (400, 422)
    - Not found (404)

    Args:
        exception: The exception to check

    Returns:
        bool: True if the exception should trigger a retry
    """
    error_message = str(exception).lower()

    # Rate limit errors are retryable
    if is_rate_limit_error(exception):
        return True

    # Check for timeout errors
    if "timeout" in error_message:
        return True

    # Check for connection errors
    if "connection" in error_message:
        return True

    # Check for server errors (5xx)
    if "500" in error_message or "502" in error_message or "503" in error_message or "504" in error_message:
        return True

    # Check for temporary unavailability
    if "unavailable" in error_message or "overloaded" in error_message:
        return True

    # Non-retryable errors
    if "401" in error_message or "403" in error_message:
        return False

    if "400" in error_message or "404" in error_message or "422" in error_message:
        return False

    # Default: retry on unknown errors (conservative approach)
    return True


class RetryableError(Exception):
    """Exception that indicates a retryable error occurred."""
    pass


class NonRetryableError(Exception):
    """Exception that indicates a non-retryable error occurred."""
    pass
