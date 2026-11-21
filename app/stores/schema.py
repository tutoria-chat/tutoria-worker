"""
The Postgres tables that replace DynamoDB and SQS (docs/MULTICLOUD.md).

  "ChatHistory"         the DynamoDB ChatMessages table (CHAT_STORE=postgres)
  "QuizAttemptAnswers"  the DynamoDB QuizAttempts table (CHAT_STORE=postgres)
  "WorkerJobs"          the SQS queues to tutoria-worker (JOB_QUEUE=postgres)

They are shared: tutoria-api writes chat and quiz answers, tutoria-worker
writes feedback chat rows and reads both for analytics, TutoriaApi reads chat
for the dashboards, and all three put jobs in "WorkerJobs". Like every table,
their schema belongs to TutoriaApi's EF migrations (AddSharedPostgresStores):
this service never creates or alters them, it only checks at startup that they
exist (check_schema). tests/postgres_tables.sql is a copy of the migration for
the tests; a TutoriaApi test keeps it identical. This file is identical in
tutoria-worker (app/stores/schema.py).

DynamoDB's 90-day TTL becomes "ExpiresAt": reads skip expired rows and
cleanup_expired() deletes them in small batches (tutoria-api runs it).
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

_CLEANUP_LOCK = 734_331_002

_CLEANUP_BATCH = 5_000
_CLEANUP_MAX_BATCHES = 20

TABLES = ("ChatHistory", "QuizAttemptAnswers", "WorkerJobs")

# Expired rows go; finished jobs are kept 14 days for diagnosis.
_CLEANUP = (
    ('"ChatHistory"', '"ExpiresAt" < now()'),
    ('"QuizAttemptAnswers"', '"ExpiresAt" < now()'),
    ('"WorkerJobs"', "\"FinishedAt\" < now() - interval '14 days'"),
)


class SchemaMissing(RuntimeError):
    """The shared tables are not there yet: TutoriaApi has not run its migrations."""


def check_schema(engine) -> None:
    """Raises SchemaMissing unless every shared table exists (TutoriaApi creates them)."""
    with engine.connect() as conn:
        missing = [t for t in TABLES
                   if conn.execute(text("SELECT to_regclass(:t)"), {"t": f'"{t}"'}).scalar() is None]
    if missing:
        raise SchemaMissing(f"{', '.join(missing)} missing: deploy TutoriaApi first (its migrations create them)")
    logger.info("Postgres stores ready: %s", ", ".join(TABLES))


def cleanup_expired(engine) -> int | None:
    """Delete expired rows a batch at a time. None when another task holds the lock."""
    deleted = 0
    with engine.connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _CLEANUP_LOCK}).scalar()
        conn.commit()
        if not got:
            return None
        try:
            for table, condition in _CLEANUP:
                for _ in range(_CLEANUP_MAX_BATCHES):
                    result = conn.execute(text(
                        f"DELETE FROM {table} WHERE ctid IN "  # noqa: S608 — constants only
                        f"(SELECT ctid FROM {table} WHERE {condition} LIMIT {_CLEANUP_BATCH})"
                    ))
                    conn.commit()
                    deleted += result.rowcount or 0
                    if (result.rowcount or 0) < _CLEANUP_BATCH:
                        break
        finally:
            conn.rollback()
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _CLEANUP_LOCK})
            conn.commit()
    if deleted:
        logger.info("Postgres stores: %d expired rows deleted", deleted)
    return deleted
