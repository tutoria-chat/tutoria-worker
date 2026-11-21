"""
The switches that let the worker run on AWS, on Azure, or on AWS with the
Postgres stores (tutoria-api docs/MULTICLOUD.md), and the Postgres job runner.

The Postgres tests need TEST_DATABASE_URL (CI starts a Postgres); without it
they are skipped.
"""

import asyncio
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.core import aws, config_source
from app.core.config import Settings, settings


def test_both_config_sources_refuse_to_start(monkeypatch):
    monkeypatch.setenv("AWS_SSM_PREFIX", "/tutoria/dev/worker/")
    monkeypatch.setenv("AZURE_KEYVAULT_URL", "https://example.vault.azure.net/")
    with pytest.raises(config_source.ConfigSourceError):
        config_source.load_config_source()


def test_keyvault_names_map_dashes_to_underscores():
    assert config_source.env_name_for_secret("SQS-GRADING-QUEUE-URL") == "SQS_GRADING_QUEUE_URL"


@pytest.mark.parametrize("field,value", [("CHAT_STORE", "redis"), ("JOB_QUEUE", "rabbit")])
def test_unknown_store_values_are_refused(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_s3_endpoint_uses_its_own_keys(monkeypatch):
    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "r2-key")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "aws-key")
    kwargs = aws.s3_client_kwargs()
    assert kwargs["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"
    assert kwargs["aws_access_key_id"] == "r2-key"
    assert kwargs["region_name"] == "auto"


def test_s3_compatible_client_builds(monkeypatch):
    """The checksum options need botocore >= 1.36 (the pin in requirements.txt)."""
    import boto3

    monkeypatch.setattr(settings, "S3_ENDPOINT_URL", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", "k")
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", "s")
    client = boto3.client("s3", **aws.s3_client_kwargs())
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"


def test_postgres_switches_never_build_an_aws_client(monkeypatch):
    import boto3

    from app.stores import chat_messages, jobs, quiz_attempts

    def refuse(*args, **kwargs):
        raise AssertionError("an AWS client was built")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)
    monkeypatch.setattr(settings, "CHAT_STORE", "postgres")
    monkeypatch.setattr(settings, "JOB_QUEUE", "postgres")
    chat_messages.chat_messages.cache_clear()
    quiz_attempts.quiz_attempts.cache_clear()
    try:
        assert isinstance(chat_messages.chat_messages(), chat_messages.PostgresChatMessages)
        assert isinstance(quiz_attempts.quiz_attempts(), quiz_attempts.PostgresQuizAttempts)
        assert jobs.queue_available("quiz_upload")
    finally:
        chat_messages.chat_messages.cache_clear()
        quiz_attempts.quiz_attempts.cache_clear()


def test_every_queue_kind_has_a_handler():
    from app.stores.jobs import SQS_QUEUE_SETTINGS
    from app.workers.job_runner import HANDLERS

    assert set(HANDLERS) == set(SQS_QUEUE_SETTINGS)
    for name in SQS_QUEUE_SETTINGS.values():
        assert hasattr(settings, name), name


def test_shared_files_match_tutoria_api():
    """app/stores and the config/aws modules are copies of tutoria-api's."""
    api = os.path.join(os.path.dirname(__file__), "..", "..", "tutoria-api")
    if not os.path.isdir(api):
        pytest.skip("tutoria-api not checked out next to this repo")
    here = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("app/stores/schema.py", "app/stores/chat_messages.py", "app/stores/quiz_attempts.py",
                "app/stores/jobs.py", "app/core/config_source.py", "app/core/aws.py",
                "tests/postgres_tables.sql"):
        with open(os.path.join(here, rel)) as mine, open(os.path.join(api, rel)) as theirs:
            assert mine.read() == theirs.read(), rel


# --- Postgres (real) ---------------------------------------------------------

PG_URL = os.environ.get("TEST_DATABASE_URL")
pg = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")


def create_test_tables(engine):
    """The shared tables from tests/postgres_tables.sql (TutoriaApi's migration), as
    TutoriaApi would have created them; the services themselves only check."""
    from app.stores.schema import check_schema

    path = os.path.join(os.path.dirname(__file__), "postgres_tables.sql")
    with open(path) as f, engine.begin() as conn:
        conn.exec_driver_sql(f.read())
    check_schema(engine)


@pytest.fixture
def pg_engine():
    from app.core.database import sqlalchemy_url

    engine = create_engine(sqlalchemy_url(PG_URL))
    create_test_tables(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM "WorkerJobs"'))
    yield engine
    engine.dispose()


def _job_rows(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            'SELECT "Kind", "State", "Attempts", "Payload", "LastError" FROM "WorkerJobs" ORDER BY "Id"'
        )).all()


def _enqueue(engine, kind, payload):
    from app.stores import jobs

    original = settings.JOB_QUEUE
    settings.JOB_QUEUE = "postgres"
    try:
        jobs.enqueue(kind, payload, engine=engine)
    finally:
        settings.JOB_QUEUE = original


@pg
def test_runner_runs_a_job_with_the_sqs_body_and_wipes_the_payload(pg_engine):
    from app.workers.job_runner import JobRunner

    seen = []

    async def handler(body):
        seen.append(body)

    _enqueue(pg_engine, "feedback", {"submission_id": 5, "learning_note": "prefers examples"})
    runner = JobRunner(engine=pg_engine, handlers={"feedback": handler}, concurrency=1)
    [job] = runner.claim(5)
    assert asyncio.run(runner.run_one(job)) == "done"
    assert seen == [{"submission_id": 5, "learning_note": "prefers examples", "_receive_count": 1}]
    assert _job_rows(pg_engine) == [("feedback", "done", 1, None, None)]


@pg
def test_runner_retries_then_fails_like_the_dlq(pg_engine):
    from app.workers.job_runner import JobRunner

    async def boom(body):
        raise RuntimeError("provider down")

    _enqueue(pg_engine, "grading", {"job_id": 1, "course_id": 2})
    runner = JobRunner(engine=pg_engine, handlers={"grading": boom}, max_attempts=2)
    [job] = runner.claim(5)
    assert asyncio.run(runner.run_one(job)) == "retry"
    kind, state, attempts, payload, error = _job_rows(pg_engine)[0]
    assert (state, attempts, json.loads(payload)) == ("queued", 1, {"job_id": 1, "course_id": 2})
    assert "provider down" in error
    assert runner.claim(5) == []  # waiting for its retry time

    with pg_engine.begin() as conn:
        conn.execute(text('UPDATE "WorkerJobs" SET "AvailableAt" = now()'))
    [job] = runner.claim(5)
    assert job.attempts == 2
    assert asyncio.run(runner.run_one(job)) == "failed"
    assert _job_rows(pg_engine)[0][:4] == ("grading", "failed", 2, None)


@pg
def test_two_runners_never_take_the_same_job(pg_engine):
    from app.workers.job_runner import JobRunner

    for i in range(6):
        _enqueue(pg_engine, "extraction", {"file_id": i, "module_id": 1})

    async def noop(body):
        pass

    a = JobRunner(engine=pg_engine, handlers={"extraction": noop})
    b = JobRunner(engine=pg_engine, handlers={"extraction": noop})
    first, second = a.claim(4), b.claim(4)
    ids = [j.id for j in first + second]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert b.claim(4) == []


@pg
def test_an_expired_lease_is_claimed_again_and_the_last_one_fails(pg_engine):
    from app.workers.job_runner import JobRunner

    async def noop(body):
        pass

    _enqueue(pg_engine, "quiz_gen", {"module_id": 1, "count": 5})
    dead = JobRunner(engine=pg_engine, handlers={"quiz_gen": noop}, max_attempts=2)
    dead.claim(1)
    with pg_engine.begin() as conn:
        conn.execute(text('UPDATE "WorkerJobs" SET "LeaseUntil" = now() - interval \'1 second\''))
    alive = JobRunner(engine=pg_engine, handlers={"quiz_gen": noop}, max_attempts=2)
    [job] = alive.claim(1)
    assert job.attempts == 2
    with pg_engine.begin() as conn:
        conn.execute(text('UPDATE "WorkerJobs" SET "LeaseUntil" = now() - interval \'1 second\''))
    assert alive.claim(1) == []
    assert _job_rows(pg_engine)[0][:4] == ("quiz_gen", "failed", 2, None)


@pg
def test_shutdown_puts_the_job_back_uncounted(pg_engine):
    from app.workers.job_runner import JobRunner

    started = asyncio.Event()

    async def slow(body):
        started.set()
        await asyncio.sleep(60)

    _enqueue(pg_engine, "transcription", {"file_id": 1})
    runner = JobRunner(engine=pg_engine, handlers={"transcription": slow})
    [job] = runner.claim(1)

    async def scenario():
        task = asyncio.create_task(runner.run_one(job))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert _job_rows(pg_engine)[0][:3] == ("transcription", "queued", 0)


@pg
def test_worker_reads_module_chat_and_quiz_rows(pg_engine, monkeypatch):
    from app.stores.chat_messages import PostgresChatMessages
    from app.stores.quiz_attempts import PostgresQuizAttempts

    module = 900_000 + uuid.uuid4().int % 1000
    chat = PostgresChatMessages(pg_engine)
    quiz = PostgresQuizAttempts(pg_engine)
    for ts in (10, 20, 30):
        chat.put({"conversationId": f"c{ts}", "timestamp": ts, "messageId": "m", "studentId": 1,
                  "moduleId": module, "question": "q", "response": "r", "provider": "openai"})
        quiz.put({"moduleId": module, "timestamp": ts, "attemptId": "a", "studentId": 1, "quizId": 1,
                  "isCorrect": ts != 20, "conceptsCovered": "x"})
    assert [m["timestamp"] for m in chat.query("module", module, start_ts=15, end_ts=30)] == [20, 30]
    assert [q["isCorrect"] for q in quiz.by_module_between(module, 0, 100)] == [True, False, True]
