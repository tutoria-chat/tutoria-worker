"""
Calendar import processing (service-to-service).

.NET stores the uploaded file in S3 and calls this endpoint; the worker downloads
it, extracts candidate events with AI, and writes them back onto the
CalendarImportJob row for professor review. Mirrors the quiz-upload SQS handler.
"""
import asyncio
import json as _json
import logging
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.modules import _require_professor_or_internal_key
from app.core.config import settings
from app.core.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/calendar", tags=["calendar"])


class ProcessCalendarRequest(BaseModel):
    job_id: int
    course_id: int
    s3_key: str | None = None
    filename: str | None = None


@router.post("/process")
async def process_calendar_import(
    payload: ProcessCalendarRequest,
    db: Session = Depends(get_db),
    current_user=Depends(_require_professor_or_internal_key),
):
    """Download the uploaded file, AI-extract events, store them on the job row."""
    from app.models import CalendarImportJob, Course
    from app.services.quiz_extractor import extract_text_from_file
    from app.services.calendar_extractor import CalendarExtractorService

    job = db.query(CalendarImportJob).filter(CalendarImportJob.id == payload.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"CalendarImportJob {payload.job_id} not found")

    s3_key = payload.s3_key or job.input_s3_key
    if not s3_key:
        raise HTTPException(status_code=400, detail="No input file for this job")

    try:
        job.status = "processing"
        db.commit()

        s3 = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
        response = await asyncio.to_thread(
            s3.get_object, Bucket=settings.S3_BUCKET_NAME, Key=s3_key
        )
        content = response["Body"].read()

        filename = payload.filename or job.original_filename or s3_key.rsplit("/", 1)[-1]

        text_content = extract_text_from_file(content, filename)
        if not text_content.strip():
            raise ValueError("Could not extract text from file. It may be empty, scanned, or corrupted.")

        course = db.query(Course).filter(Course.id == payload.course_id).first()
        extractor = CalendarExtractorService(
            db,
            course_name=getattr(course, "name", "") or "",
            year_hint=datetime.now(timezone.utc).year,
        )
        events = await extractor.extract_from_text(text_content, filename)

        job.extracted_events_json = _json.dumps(events, ensure_ascii=False)
        job.extracted_count = len(events)
        job.status = "completed"
        job.processed_at = datetime.now(timezone.utc)
        db.commit()

        logger.info("✅ Calendar extraction complete — job_id=%s course_id=%s events=%s",
                    payload.job_id, payload.course_id, len(events))
        return {"status": "completed", "extracted_count": len(events)}

    except Exception as exc:
        logger.error("Calendar extraction failed for job %s: %s", payload.job_id, exc)
        job.status = "failed"
        job.error_message = str(exc)[:2000]
        job.processed_at = datetime.now(timezone.utc)
        db.commit()
        # Return 200 with a failed status so .NET reloads the (failed) job cleanly.
        return {"status": "failed", "extracted_count": 0, "error": str(exc)[:500]}
