"""
SQS Worker — listens for document extraction and quiz generation jobs.

Message formats:
  Extraction queue: { "file_id": 123, "module_id": 456 }
  Quiz gen queue:   { "module_id": 789, "count": 50, "upsert": true }

Both queues are polled in separate async loops using long-polling (20s).
A message is only deleted from SQS after successful processing; on error
it returns to the queue (or dead-letter queue after max retries).
"""

import asyncio
import json
import logging
from typing import Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from app.core.config import settings
from app.core.database import SessionLocal
from app.services.document_extraction_service import DocumentExtractionService
from app.services.quiz_generator import QuizGeneratorService

logger = logging.getLogger(__name__)


def _get_sqs_client():
    return boto3.client(
        "sqs",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )


async def _process_extraction_message(body: dict) -> None:
    """Extract text from a single file and persist it to the DB."""
    file_id: Optional[int] = body.get("file_id")
    module_id: Optional[int] = body.get("module_id")

    if not file_id:
        raise ValueError(f"Missing file_id in extraction message: {body}")

    db = SessionLocal()
    try:
        service = DocumentExtractionService(db)
        await service.extract_and_store(file_id)
        logger.info("✅ Extraction complete — file_id=%s module_id=%s", file_id, module_id)
    finally:
        db.close()


async def _process_quiz_gen_message(body: dict) -> None:
    """Generate quizzes for a module — skip if already generated and upsert=False."""
    from app.models import Quiz

    module_id: Optional[int] = body.get("module_id")
    count: int = body.get("count", 50)
    upsert: bool = body.get("upsert", False)

    if not module_id:
        raise ValueError(f"Missing module_id in quiz-gen message: {body}")

    db = SessionLocal()
    try:
        # Skip generation if quizzes already exist and upsert was not requested
        if not upsert:
            existing = (
                db.query(Quiz)
                .filter(Quiz.module_id == module_id, Quiz.source == "ai_generated")
                .count()
            )
            if existing > 0:
                logger.info(
                    "⏭️  Quiz gen skipped — module_id=%s already has %s ai_generated quizzes",
                    module_id, existing,
                )
                return

        from sqlalchemy.orm import joinedload
        from app.models import Module
        module = (
            db.query(Module)
            .options(joinedload(Module.files))
            .filter(Module.id == module_id, Module.is_active == True)
            .first()
        )
        if not module:
            raise ValueError(f"Module {module_id} not found or has been deleted")

        service = QuizGeneratorService(module, db)
        quizzes = await service.generate_quiz_bank(count=count)
        logger.info(
            "✅ Quiz gen complete — module_id=%s generated=%s upsert=%s",
            module_id, len(quizzes), upsert,
        )
    finally:
        db.close()


async def _poll_queue(queue_url: str, queue_name: str, handler) -> None:
    """
    Long-poll a single SQS queue forever.
    Deletes the message only after the handler succeeds.
    On handler error the message becomes visible again after the visibility timeout.

    NOTE: boto3 is synchronous. All network calls are dispatched via asyncio.to_thread()
    so they never block the event loop — keeping the /health endpoint responsive at all times.
    """
    sqs = _get_sqs_client()
    logger.info("🔁 SQS worker started — queue: %s", queue_name)

    while True:
        try:
            # Run the blocking long-poll (WaitTimeSeconds=20) in a thread so the event
            # loop (and uvicorn's HTTP server, including /health) stays unblocked.
            response = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,        # one at a time — memory safety
                WaitTimeSeconds=20,           # long-polling, reduces empty-receive cost
                AttributeNames=["ApproximateReceiveCount"],
            )

            messages = response.get("Messages", [])
            if not messages:
                continue

            msg = messages[0]
            receipt = msg["ReceiptHandle"]
            receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", 1))

            try:
                body = json.loads(msg["Body"])
                logger.info("📨 Received %s message (attempt %s): %s", queue_name, receive_count, body)

                await handler(body)

                # Delete only after success (also non-blocking)
                await asyncio.to_thread(sqs.delete_message, QueueUrl=queue_url, ReceiptHandle=receipt)
                logger.info("🗑️  Deleted %s message after success", queue_name)

            except Exception as exc:
                logger.error(
                    "❌ Failed to process %s message (attempt %s): %s — returning to queue",
                    queue_name, receive_count, exc,
                    exc_info=True,
                )
                # Don't delete — SQS will re-deliver after visibility timeout.
                # After max retries it moves to the dead-letter queue automatically.

        except (ClientError, NoCredentialsError) as exc:
            logger.error("SQS client error on %s: %s — retrying in 30s", queue_name, exc)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            logger.info("🛑 SQS worker for %s cancelled", queue_name)
            return
        except Exception as exc:
            logger.error("Unexpected SQS error on %s: %s — retrying in 10s", queue_name, exc, exc_info=True)
            await asyncio.sleep(10)


async def run_sqs_workers() -> None:
    """
    Start both SQS consumer loops concurrently.
    Skips any queue whose URL is not configured (e.g. local dev without SQS).
    """
    extraction_url = settings.SQS_EXTRACTION_QUEUE_URL
    quiz_gen_url = settings.SQS_QUIZ_GEN_QUEUE_URL

    tasks = []

    if extraction_url:
        tasks.append(asyncio.create_task(
            _poll_queue(extraction_url, "extraction", _process_extraction_message),
            name="sqs-extraction",
        ))
        logger.info("  ✅ SQS extraction worker started")
    else:
        logger.warning("  ⚠️  SQS_EXTRACTION_QUEUE_URL not set — extraction SQS worker disabled")

    if quiz_gen_url:
        tasks.append(asyncio.create_task(
            _poll_queue(quiz_gen_url, "quiz-gen", _process_quiz_gen_message),
            name="sqs-quiz-gen",
        ))
        logger.info("  ✅ SQS quiz-gen worker started")
    else:
        logger.warning("  ⚠️  SQS_QUIZ_GEN_QUEUE_URL not set — quiz-gen SQS worker disabled")

    if tasks:
        await asyncio.gather(*tasks)
