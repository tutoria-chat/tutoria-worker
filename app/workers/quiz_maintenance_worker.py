"""
Quiz Maintenance Worker - DISABLED

Quiz generation only happens when a professor explicitly requests it.
No automatic background generation — that wastes DB space on quizzes nobody asked for.

The SQS worker handles on-demand generation triggered by the .NET API.
"""
import logging
import asyncio

logger = logging.getLogger(__name__)


async def run_daily_maintenance():
    """No-op — quiz generation is on-demand only."""
    logger.info("ℹ️  Quiz maintenance worker is disabled — quizzes are generated on-demand only")
    # Yield control forever without doing anything
    while True:
        await asyncio.sleep(86400)
