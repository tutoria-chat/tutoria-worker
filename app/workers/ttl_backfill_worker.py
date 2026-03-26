"""
TTL Backfill Worker — One-time Job

Runs once on startup when TTL_BACKFILL_ENABLED=true.
Adds 'ttl' attribute to existing DynamoDB ChatMessages items that are missing it.
"""
import asyncio
import logging

from app.core.config import settings
from app.services.dynamodb_service import batch_add_ttl

logger = logging.getLogger(__name__)


async def run_ttl_backfill():
    """One-time TTL backfill for ChatMessages table."""
    if not settings.TTL_BACKFILL_ENABLED:
        logger.info("TTL backfill is disabled (TTL_BACKFILL_ENABLED=false)")
        return

    logger.info("🔧 Starting TTL backfill for ChatMessages...")

    try:
        updated = await asyncio.to_thread(batch_add_ttl, "chat")
        logger.info(f"✅ TTL backfill complete: {updated} ChatMessages items updated")
    except asyncio.CancelledError:
        logger.info("🛑 TTL backfill cancelled")
        return
    except Exception as e:
        logger.error(f"Error in TTL backfill: {e}")
