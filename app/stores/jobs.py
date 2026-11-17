"""
Background jobs for tutoria-worker, behind JOB_QUEUE:

  sqs       a message on the kind's SQS queue (as before)
  postgres  a row in "WorkerJobs" (app/stores/schema.py), claimed by the
            worker with FOR UPDATE SKIP LOCKED

The payload is the same JSON either way, so the worker's handlers don't change.
"""

import json
import logging
from typing import Any, Dict

from sqlalchemy import text

from app.core.config import settings

logger = logging.getLogger(__name__)

# Kind -> the setting holding its SQS queue URL (docs/MULTICLOUD.md). The same
# file in tutoria-api and tutoria-worker; a repo without one of these settings
# just has no queue for that kind.
SQS_QUEUE_SETTINGS = {
    "extraction": "SQS_EXTRACTION_QUEUE_URL",
    "quiz_gen": "SQS_QUIZ_GEN_QUEUE_URL",
    "transcription": "SQS_TRANSCRIPTION_QUEUE_URL",
    "grading": "SQS_GRADING_QUEUE_URL",
    "quiz_upload": "SQS_QUIZ_UPLOAD_QUEUE_URL",
    "feedback": "SQS_FEEDBACK_QUEUE_URL",
}


def _queue_url(kind: str) -> str:
    return getattr(settings, SQS_QUEUE_SETTINGS[kind], "") or ""


def queue_available(kind: str) -> bool:
    """Whether jobs of this kind have somewhere durable to go."""
    if settings.JOB_QUEUE == "postgres":
        return True
    return bool(_queue_url(kind))


def enqueue(kind: str, payload: Dict[str, Any], engine=None) -> None:
    """Hand a job to the worker. Raises when it couldn't be queued."""
    body = json.dumps(payload)
    if settings.JOB_QUEUE == "postgres":
        if engine is None:
            from app.core.database import engine as default_engine

            engine = default_engine
        with engine.begin() as conn:
            conn.execute(
                text('INSERT INTO "WorkerJobs" ("Kind", "Payload") VALUES (:kind, :payload)'),
                {"kind": kind, "payload": body},
            )
        return
    from app.core.aws import sqs_client

    url = _queue_url(kind)
    if not url:
        raise RuntimeError(f"no SQS queue configured for {kind} jobs ({SQS_QUEUE_SETTINGS[kind]})")
    sqs_client().send_message(QueueUrl=url, MessageBody=body)
