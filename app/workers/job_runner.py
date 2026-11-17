"""
The Postgres job runner (JOB_QUEUE=postgres): the SQS consumer loops, with
"WorkerJobs" rows instead of messages (tutoria-api docs/MULTICLOUD.md).

  claim     one UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED): two
            replicas never take the same row. A row whose lease ran out (the
            replica died) is claimed again, like an SQS message coming back
            after its visibility timeout.
  run       the same handler the SQS loop calls, with the same JSON body and
            "_receive_count" = this attempt. The lease is renewed every minute
            while it runs.
  success   State 'done'.
  failure   back to 'queued' with a later AvailableAt; after JOB_MAX_ATTEMPTS
            'failed' (the DLQ).
  either    Payload set to NULL: a feedback payload can carry the student's
            learning note, which is never kept.
"""

import asyncio
import json
import logging
import socket
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List

from sqlalchemy import text

from app.core.config import settings
from app.workers import sqs_worker

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]

# Kind (the old queue) -> its handler.
HANDLERS: Dict[str, Handler] = {
    "extraction": sqs_worker._process_extraction_message,
    "quiz_gen": sqs_worker._process_quiz_gen_message,
    "transcription": sqs_worker._process_transcription_message,
    "grading": sqs_worker._process_grading_message,
    "quiz_upload": sqs_worker._process_quiz_upload_message,
    "feedback": sqs_worker._process_feedback_message,
}

HEARTBEAT_SECONDS = 60
# A failed job waits RETRY_BASE_SECONDS × attempts before its next try (2 min,
# 4 min...): quiz generation defers itself this way while files are still
# being extracted.
RETRY_BASE_SECONDS = 120
_ERROR_CHARS = 1000


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: str | None
    attempts: int


_FAIL_ABANDONED = text("""
UPDATE "WorkerJobs"
SET "State" = 'failed', "FinishedAt" = now(), "Payload" = NULL, "LeaseUntil" = NULL,
    "LastError" = 'the runner stopped during the last attempt'
WHERE "State" = 'processing' AND "LeaseUntil" < now() AND "Attempts" >= :max_attempts
""")

_CLAIM = text("""
WITH pick AS (
    SELECT "Id" FROM "WorkerJobs"
    WHERE "Kind" = ANY(:kinds)
      AND (("State" = 'queued' AND "AvailableAt" <= now())
           OR ("State" = 'processing' AND "LeaseUntil" < now()))
    ORDER BY "AvailableAt", "Id"
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
)
UPDATE "WorkerJobs" w
SET "State" = 'processing', "Attempts" = w."Attempts" + 1, "ClaimedBy" = :me,
    "ClaimedAt" = now(), "LeaseUntil" = now() + make_interval(secs => :lease)
FROM pick WHERE w."Id" = pick."Id"
RETURNING w."Id", w."Kind", w."Payload", w."Attempts"
""")

_RENEW = text("""
UPDATE "WorkerJobs" SET "LeaseUntil" = now() + make_interval(secs => :lease)
WHERE "Id" = :id AND "ClaimedBy" = :me AND "State" = 'processing'
""")

_DONE = text("""
UPDATE "WorkerJobs"
SET "State" = 'done', "FinishedAt" = now(), "Payload" = NULL, "LeaseUntil" = NULL, "LastError" = NULL
WHERE "Id" = :id AND "ClaimedBy" = :me
""")

_FAILED = text("""
UPDATE "WorkerJobs"
SET "State" = 'failed', "FinishedAt" = now(), "Payload" = NULL, "LeaseUntil" = NULL, "LastError" = :error
WHERE "Id" = :id AND "ClaimedBy" = :me
""")

_RETRY = text("""
UPDATE "WorkerJobs"
SET "State" = 'queued', "ClaimedBy" = NULL, "LeaseUntil" = NULL, "LastError" = :error,
    "AvailableAt" = now() + make_interval(secs => :delay)
WHERE "Id" = :id AND "ClaimedBy" = :me
""")

# Shutting down mid-job: back to the queue at once, the attempt not counted.
_RELEASE = text("""
UPDATE "WorkerJobs"
SET "State" = 'queued', "ClaimedBy" = NULL, "LeaseUntil" = NULL, "Attempts" = greatest("Attempts" - 1, 0)
WHERE "Id" = :id AND "ClaimedBy" = :me AND "State" = 'processing'
""")


class JobRunner:
    def __init__(self, engine=None, handlers: Dict[str, Handler] | None = None,
                 concurrency: int | None = None, max_attempts: int | None = None,
                 lease_seconds: int | None = None, poll_seconds: float | None = None):
        if engine is None:
            from app.core.database import engine as default_engine

            engine = default_engine
        self.engine = engine
        self.handlers = handlers or HANDLERS
        self.concurrency = concurrency or settings.JOB_CONCURRENCY
        self.max_attempts = max_attempts or settings.JOB_MAX_ATTEMPTS
        self.lease_seconds = lease_seconds or settings.JOB_LEASE_SECONDS
        self.poll_seconds = settings.JOB_POLL_SECONDS if poll_seconds is None else poll_seconds
        self.me = f"{socket.gethostname()[:60]}:{uuid.uuid4().hex[:8]}"
        self.running: set[asyncio.Task] = set()

    # ── database (sync, run in threads) ─────────────────────────────────────
    def _execute(self, statement, params: dict):
        with self.engine.begin() as conn:
            return conn.execute(statement, params)

    def claim(self, limit: int) -> List[Job]:
        with self.engine.begin() as conn:
            conn.execute(_FAIL_ABANDONED, {"max_attempts": self.max_attempts})
            rows = conn.execute(_CLAIM, {
                "kinds": list(self.handlers), "limit": limit, "me": self.me, "lease": self.lease_seconds,
            }).all()
        return [Job(r[0], r[1], r[2], r[3]) for r in rows]

    def finish(self, job: Job, error: BaseException | None) -> str:
        if error is None:
            self._execute(_DONE, {"id": job.id, "me": self.me})
            return "done"
        message = f"{type(error).__name__}: {error}"[:_ERROR_CHARS]
        if job.attempts >= self.max_attempts:
            self._execute(_FAILED, {"id": job.id, "me": self.me, "error": message})
            return "failed"
        self._execute(_RETRY, {
            "id": job.id, "me": self.me, "error": message, "delay": RETRY_BASE_SECONDS * job.attempts,
        })
        return "retry"

    # ── one job ─────────────────────────────────────────────────────────────
    async def _heartbeat(self, job: Job) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                await asyncio.to_thread(self._execute, _RENEW, {"id": job.id, "me": self.me, "lease": self.lease_seconds})
            except Exception:
                logger.warning("Could not renew the lease of job %s", job.id, exc_info=True)

    async def run_one(self, job: Job) -> str:
        heartbeat = asyncio.create_task(self._heartbeat(job))
        error: BaseException | None = None
        try:
            handler = self.handlers[job.kind]
            body = json.loads(job.payload or "null")
            if not isinstance(body, dict):
                raise ValueError("job payload is not a JSON object")
            logger.info("📨 %s job %s (attempt %s): %s", job.kind, job.id, job.attempts, sqs_worker._loggable(body))
            body["_receive_count"] = job.attempts
            await handler(body)
        except asyncio.CancelledError:
            heartbeat.cancel()
            await asyncio.shield(asyncio.to_thread(self._execute, _RELEASE, {"id": job.id, "me": self.me}))
            raise
        except Exception as exc:  # noqa: BLE001 — every failure is a retry or the DLQ
            error = exc
            logger.error("❌ %s job %s failed (attempt %s): %s", job.kind, job.id, job.attempts, exc, exc_info=True)
        finally:
            heartbeat.cancel()
        outcome = await asyncio.to_thread(self.finish, job, error)
        logger.info("%s job %s: %s", job.kind, job.id, outcome)
        return outcome

    # ── the loop ────────────────────────────────────────────────────────────
    async def run_forever(self) -> None:
        logger.info("🔁 Postgres job runner started — %s, %d at a time, kinds: %s",
                    self.me, self.concurrency, ", ".join(self.handlers))
        try:
            while True:
                free = self.concurrency - len(self.running)
                jobs: List[Job] = []
                if free > 0:
                    try:
                        jobs = await asyncio.to_thread(self.claim, free)
                    except Exception:
                        logger.warning("Job claim failed; retrying", exc_info=True)
                        await asyncio.sleep(max(self.poll_seconds, 5))
                        continue
                for job in jobs:
                    task = asyncio.create_task(self.run_one(job), name=f"job-{job.id}")
                    self.running.add(task)
                    task.add_done_callback(self.running.discard)
                if not jobs:
                    await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            for task in list(self.running):
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
            logger.info("🛑 Postgres job runner stopped")
            raise


async def run_job_runner() -> None:
    await JobRunner().run_forever()
