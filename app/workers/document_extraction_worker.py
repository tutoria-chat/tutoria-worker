"""
Document Extraction Worker - Background Job

Two modes:
1. On-upload: Triggered immediately by .NET FileService after file upload
2. Daily sweep: Runs at 2 AM to catch any files that weren't extracted on upload
"""
import logging
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.document_extraction_service import DocumentExtractionService

logger = logging.getLogger(__name__)

# Target hour for daily sweep (2 AM local server time)
DAILY_SWEEP_HOUR = 2


async def process_pending_extractions():
    """
    Check for files without extracted text and process them.
    """
    logger.info("Starting document extraction sweep...")

    db: Session = next(get_db())

    try:
        service = DocumentExtractionService(db)

        # Process up to 50 files per run
        extracted_count = await service.extract_all_pending(limit=50)

        if extracted_count > 0:
            logger.info(f"Extraction complete: {extracted_count} files processed")
        else:
            logger.debug("No files pending extraction")

        return extracted_count

    except Exception as e:
        logger.error(f"Error in extraction worker: {e}")
        return 0
    finally:
        db.close()


def _seconds_until_next_run(target_hour: int = DAILY_SWEEP_HOUR) -> float:
    """Calculate seconds until the next occurrence of target_hour (e.g. 2 AM)."""
    now = datetime.now()
    next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    return (next_run - now).total_seconds()


async def run_extraction_worker():
    """
    Run the extraction worker on a daily schedule (default 2 AM).

    Primary extraction happens on file upload (triggered by .NET FileService).
    This worker is a safety net that catches any missed files.
    """
    while True:
        try:
            wait_seconds = _seconds_until_next_run()
            next_run = datetime.now() + timedelta(seconds=wait_seconds)
            logger.info(f"Next extraction sweep at {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds/3600:.1f}h)")
            await asyncio.sleep(wait_seconds)

            logger.info("Starting daily extraction sweep (2 AM)...")
            extracted = await process_pending_extractions()

            # If there were files, there might be more — run again immediately
            while extracted >= 50:
                logger.info("Batch was full, running another pass...")
                extracted = await process_pending_extractions()

        except Exception as e:
            logger.error(f"Error in extraction loop: {e}")
            # Wait 5 minutes before retrying on error
            await asyncio.sleep(300)


# For manual testing/triggering
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(process_pending_extractions())
