"""
Document Extraction Worker - Background Job

Three modes:
1. On-upload: SQS message triggers extraction immediately after file upload
2. Failed retry: Runs every 4 hours to retry files with processing_status='failed'
3. Daily sweep: Runs at 2 AM to catch any files that were never processed at all
"""
import logging
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.core.database import get_db, SessionLocal
from app.services.document_extraction_service import DocumentExtractionService

logger = logging.getLogger(__name__)

# Target hour for daily sweep (2 AM local server time)
DAILY_SWEEP_HOUR = 2

# How often to retry failed extractions (in seconds)
FAILED_RETRY_INTERVAL_SECONDS = 4 * 60 * 60  # 4 hours


async def process_pending_extractions():
    """
    Daily sweep: extract text from all files that have never been successfully processed
    (processing_status is NULL or not 'ready', excluding YouTube).
    """
    logger.info("Starting daily document extraction sweep...")

    db: Session = next(get_db())

    try:
        service = DocumentExtractionService(db)

        # Process up to 50 files per run
        extracted_count = await service.extract_all_pending(limit=50)

        if extracted_count > 0:
            logger.info(f"Daily sweep complete: {extracted_count} files processed")
        else:
            logger.debug("No files pending extraction")

        return extracted_count

    except Exception as e:
        logger.error(f"Error in extraction worker: {e}")
        return 0
    finally:
        db.close()


async def retry_failed_extractions():
    """
    Retry files that previously failed extraction (processing_status='failed').
    Separate from the daily sweep so failures get a second chance within hours,
    not days.
    """
    logger.info("Retrying failed extractions...")

    db = SessionLocal()
    try:
        from app.models.file import File

        failed_files = (
            db.query(File)
            .filter(
                File.is_active == True,
                File.processing_status == "failed",
                or_(File.source_type == None, File.source_type != "youtube"),
            )
            .limit(20)
            .all()
        )

        if not failed_files:
            logger.debug("No failed extractions to retry")
            return 0

        logger.info(f"Found {len(failed_files)} failed files to retry")
        service = DocumentExtractionService(db)
        success_count = 0
        for file in failed_files:
            # force=True so extract_and_store doesn't short-circuit on 'failed' status
            if await service.extract_and_store(file, force=True):
                success_count += 1

        logger.info(f"Failed-extraction retry: {success_count}/{len(failed_files)} recovered")
        return success_count

    except Exception as e:
        logger.error(f"Error retrying failed extractions: {e}")
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
    Daily sweep at 2 AM — catches files that were never processed at all
    (NULL or non-ready status). SQS handles the on-upload path; this is the
    safety net.
    """
    while True:
        try:
            wait_seconds = _seconds_until_next_run()
            next_run = datetime.now() + timedelta(seconds=wait_seconds)
            logger.info(f"Next daily extraction sweep at {next_run.strftime('%Y-%m-%d %H:%M')} ({wait_seconds/3600:.1f}h)")
            await asyncio.sleep(wait_seconds)

            logger.info("Starting daily extraction sweep (2 AM)...")
            extracted = await process_pending_extractions()

            # If there were files, there might be more — run again immediately
            while extracted >= 50:
                logger.info("Batch was full, running another pass...")
                extracted = await process_pending_extractions()

        except asyncio.CancelledError:
            logger.info("🛑 Daily extraction worker cancelled")
            return
        except Exception as e:
            logger.error(f"Error in extraction loop: {e}")
            await asyncio.sleep(300)


async def run_failed_extraction_retry_worker():
    """
    Runs every 4 hours and retries files with processing_status='failed'.
    Gives failed files a second chance well before the daily sweep would catch them.
    """
    while True:
        try:
            logger.info(f"Next failed-extraction retry in {FAILED_RETRY_INTERVAL_SECONDS // 3600}h")
            await asyncio.sleep(FAILED_RETRY_INTERVAL_SECONDS)

            recovered = await retry_failed_extractions()
            if recovered > 0:
                logger.info(f"♻️  Recovered {recovered} previously-failed extractions")

        except asyncio.CancelledError:
            logger.info("🛑 Failed-extraction retry worker cancelled")
            return
        except Exception as e:
            logger.error(f"Error in failed-extraction retry loop: {e}")
            await asyncio.sleep(300)


# For manual testing/triggering
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(process_pending_extractions())
