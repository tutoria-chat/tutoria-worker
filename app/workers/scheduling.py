"""Shared scheduling utilities for background workers."""
from datetime import datetime, timedelta


def seconds_until_next_run(target_hour: int, target_minute: int = 0) -> float:
    """Calculate seconds until the next occurrence of target_hour:target_minute."""
    now = datetime.now()
    next_run = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()
