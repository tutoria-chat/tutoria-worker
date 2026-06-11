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
    from app.models.file import File

    file_id: Optional[int] = body.get("file_id")
    module_id: Optional[int] = body.get("module_id")

    if not file_id:
        raise ValueError(f"Missing file_id in extraction message: {body}")

    db = SessionLocal()
    try:
        file = db.query(File).filter(File.id == file_id).first()
        if not file:
            raise ValueError(f"File {file_id} not found in database")

        service = DocumentExtractionService(db)
        await service.extract_and_store(file)
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
        from app.models import Module, File as FileModel
        module = (
            db.query(Module)
            .options(joinedload(Module.files))
            .filter(Module.id == module_id, Module.is_active == True)
            .first()
        )
        if not module:
            raise ValueError(f"Module {module_id} not found or has been deleted")

        # If any files are still being extracted, defer — re-raise so SQS redelivers
        # after the visibility timeout instead of silently deleting the message.
        in_progress = [
            f for f in (module.files or [])
            if f.is_active and f.processing_status in ("pending", "processing")
        ]
        if in_progress:
            names = [f.name for f in in_progress]
            raise RuntimeError(
                f"Quiz gen deferred for module_id={module_id} — "
                f"{len(in_progress)} file(s) still extracting: {names}"
            )

        service = QuizGeneratorService(module, db)
        try:
            quizzes = await service.generate_quiz_bank(count=count)
            logger.info(
                "✅ Quiz gen complete — module_id=%s generated=%s upsert=%s",
                module_id, len(quizzes), upsert,
            )
        except ValueError as exc:
            # Permanent data errors (no files, no extractable content after extraction
            # finished). Retrying will never fix this — delete the message.
            logger.error(
                "⛔ Quiz gen aborted for module_id=%s (permanent error, message will be deleted): %s",
                module_id, exc,
            )
    finally:
        db.close()


async def _process_transcription_message(body: dict) -> None:
    """Transcribe a YouTube video for an existing pending File record."""
    from app.models.file import File
    from app.services.transcription_service import TranscriptionService

    file_id: Optional[int] = body.get("file_id")
    youtube_url: Optional[str] = body.get("youtube_url")
    language: str = body.get("language", "pt-br")

    if not file_id or not youtube_url:
        raise ValueError(f"Missing file_id or youtube_url in transcription message: {body}")

    db = SessionLocal()
    try:
        file = db.query(File).filter(File.id == file_id).first()
        if not file:
            raise ValueError(f"File {file_id} not found in database")

        service = TranscriptionService(db)
        result = service.transcribe_existing_file(file_id=file_id, youtube_url=youtube_url, language=language)
        logger.info(
            "✅ Transcription complete — file_id=%s words=%s source=%s",
            file_id, result.get("word_count"), result.get("source"),
        )
    finally:
        db.close()


async def _process_quiz_upload_message(body: dict) -> None:
    """Extract quiz questions from an uploaded file and store in QuizUploadJob."""
    job_id: Optional[int] = body.get("job_id")
    module_id: Optional[int] = body.get("module_id")

    if not job_id:
        raise ValueError(f"Missing job_id in quiz-upload message: {body}")
    if not module_id:
        raise ValueError(f"Missing module_id in quiz-upload message: {body}")

    db = SessionLocal()
    try:
        from app.models import QuizUploadJob, Module as ModuleModel
        from app.services.quiz_extractor import extract_text_from_file, QuizExtractorService
        import json as _json
        import boto3
        import asyncio

        job = db.query(QuizUploadJob).filter(QuizUploadJob.id == job_id).first()
        if not job:
            raise ValueError(f"QuizUploadJob {job_id} not found in database")
        if not job.input_s3_key:
            raise ValueError(f"QuizUploadJob {job_id} has no input_s3_key")

        module = db.query(ModuleModel).filter(ModuleModel.id == module_id, ModuleModel.is_active == True).first()
        if not module:
            raise ValueError(f"Module {module_id} not found or inactive")

        # Mark processing
        from datetime import datetime, timezone
        job.status = "processing"
        db.commit()

        # Download file from S3
        from app.core.config import settings as _settings
        s3 = boto3.client(
            "s3",
            region_name=_settings.AWS_REGION,
            aws_access_key_id=_settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=_settings.AWS_SECRET_ACCESS_KEY,
        )
        response = await asyncio.to_thread(
            s3.get_object, Bucket=_settings.S3_BUCKET_NAME, Key=job.input_s3_key
        )
        content = response["Body"].read()

        filename = job.original_filename or job.input_s3_key.rsplit("/", 1)[-1]

        # Extract text + questions
        text_content = extract_text_from_file(content, filename)
        if not text_content.strip():
            raise ValueError("Could not extract text from file. File may be empty or corrupted.")

        extractor = QuizExtractorService(module, db)
        questions = await extractor.extract_from_text(text_content, filename)

        # Store extracted questions as JSON
        job.extracted_questions_json = _json.dumps(questions, ensure_ascii=False)
        job.extracted_count = len(questions)
        job.status = "completed"
        job.processed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info("✅ Quiz upload extraction complete — job_id=%s module_id=%s extracted=%s",
                    job_id, module_id, len(questions))
    except Exception as exc:
        from app.models import QuizUploadJob as _QUJ
        from datetime import datetime, timezone
        job_fail = db.query(_QUJ).filter(_QUJ.id == job_id).first()
        if job_fail:
            job_fail.status = "failed"
            job_fail.error_message = str(exc)[:2000]
            job_fail.processed_at = datetime.now(timezone.utc)
            db.commit()
        raise
    finally:
        db.close()


async def _process_feedback_message(body: dict) -> None:
    """
    Generate AI feedback for a student's assignment submission (companion widget).
    Message: { "submission_id": 1, "assignment_id": 2, "module_id": 3,
               "conversation_id": "uuid", "student_id": 42, "matricula": "MAT123" }
    """
    submission_id: Optional[int] = body.get("submission_id")
    conversation_id: Optional[str] = body.get("conversation_id")

    if not submission_id:
        raise ValueError(f"Missing submission_id in feedback message: {body}")
    if not conversation_id:
        raise ValueError(f"Missing conversation_id in feedback message: {body}")

    db = SessionLocal()
    try:
        from app.services.feedback_service import process_feedback_job

        try:
            await process_feedback_job(
                db,
                submission_id=submission_id,
                conversation_id=conversation_id,
                matricula=body.get("matricula"),
            )
        except Exception:
            # Mark failed when this was the last delivery attempt — afterwards the
            # message goes to the DLQ and the widget would otherwise poll forever.
            receive_count = body.get("_receive_count", 1)
            if receive_count >= 3:
                from app.models import AssignmentSubmission as _Sub
                db.rollback()
                sub = db.query(_Sub).filter(_Sub.id == submission_id).first()
                if sub and sub.status == "processing":
                    sub.status = "failed"
                    db.commit()
                    logger.error("⛔ Feedback marked failed after %s attempts — submission_id=%s",
                                 receive_count, submission_id)
            raise
    finally:
        db.close()


async def _process_grading_message(body: dict) -> None:
    """Grade a batch of student answers and upload a CSV result to S3."""
    job_id: Optional[int] = body.get("job_id")
    course_id: Optional[int] = body.get("course_id")

    if not job_id:
        raise ValueError(f"Missing job_id in grading message: {body}")
    if not course_id:
        raise ValueError(f"Missing course_id in grading message: {body}")

    db = SessionLocal()
    try:
        from app.services.grading_service import GradingService
        service = GradingService(db)
        await service.process_job(job_id, course_id)
        logger.info("✅ Grading complete — job_id=%s course_id=%s", job_id, course_id)
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

                # Let handlers know which delivery attempt this is (e.g. to mark
                # a job failed on the final attempt before the DLQ swallows it)
                body["_receive_count"] = receive_count

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
    Start all SQS consumer loops concurrently.
    Skips any queue whose URL is not configured (e.g. local dev without SQS).
    """
    extraction_url = settings.SQS_EXTRACTION_QUEUE_URL
    quiz_gen_url = settings.SQS_QUIZ_GEN_QUEUE_URL
    transcription_url = settings.SQS_TRANSCRIPTION_QUEUE_URL
    grading_url = settings.SQS_GRADING_QUEUE_URL
    quiz_upload_url = settings.SQS_QUIZ_UPLOAD_QUEUE_URL
    feedback_url = settings.SQS_FEEDBACK_QUEUE_URL

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

    if transcription_url:
        tasks.append(asyncio.create_task(
            _poll_queue(transcription_url, "transcription", _process_transcription_message),
            name="sqs-transcription",
        ))
        logger.info("  ✅ SQS transcription worker started")
    else:
        logger.warning("  ⚠️  SQS_TRANSCRIPTION_QUEUE_URL not set — transcription SQS worker disabled")

    if grading_url:
        tasks.append(asyncio.create_task(
            _poll_queue(grading_url, "grading", _process_grading_message),
            name="sqs-grading",
        ))
        logger.info("  ✅ SQS grading worker started")
    else:
        logger.warning("  ⚠️  SQS_GRADING_QUEUE_URL not set — grading SQS worker disabled")

    if quiz_upload_url:
        tasks.append(asyncio.create_task(
            _poll_queue(quiz_upload_url, "quiz-upload", _process_quiz_upload_message),
            name="sqs-quiz-upload",
        ))
        logger.info("  ✅ SQS quiz-upload worker started")
    else:
        logger.warning("  ⚠️  SQS_QUIZ_UPLOAD_QUEUE_URL not set — quiz-upload SQS worker disabled")

    if feedback_url:
        tasks.append(asyncio.create_task(
            _poll_queue(feedback_url, "feedback", _process_feedback_message),
            name="sqs-feedback",
        ))
        logger.info("  ✅ SQS feedback worker started")
    else:
        logger.warning("  ⚠️  SQS_FEEDBACK_QUEUE_URL not set — feedback SQS worker disabled")

    if tasks:
        await asyncio.gather(*tasks)
