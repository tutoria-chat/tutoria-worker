"""app/tools/dynamo_to_postgres.py: DynamoDB items -> Postgres rows, re-runnable."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.tools import dynamo_to_postgres as tool

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
LIVE_TTL = Decimal(int((NOW + timedelta(days=30)).timestamp()))
DEAD_TTL = Decimal(int((NOW - timedelta(days=1)).timestamp()))


def chat_item(ts=1_763_400_000_000, ttl=LIVE_TTL, **extra):
    item = {
        "conversationId": "conv-1", "timestamp": Decimal(ts), "messageId": "m-1",
        "studentId": Decimal(7), "moduleId": Decimal(3), "question": "q", "response": "r",
        "modelUsed": "gpt", "provider": "openai", "hasFile": False,
        "createdAt": "2026-11-17T10:00:00", "ttl": ttl,
    }
    item.update(extra)
    return item


def quiz_item(ts=1_763_400_000_000, ttl=LIVE_TTL, question=1):
    return {
        "attemptId": "a-1", "moduleId": Decimal(3), "timestamp": Decimal(ts), "studentId": Decimal(7),
        "quizId": Decimal(11), "questionNumber": Decimal(question), "selectedAnswer": "B",
        "correctAnswer": "B", "isCorrect": True, "conceptsCovered": "x, y", "difficulty": "easy",
        "createdAt": "2026-11-17T10:00:00", "ttl": ttl,
    }


def test_chat_row_turns_decimals_into_ints_and_keeps_the_item_expiry():
    row = tool.chat_row(chat_item(tokenCount=Decimal(120)), NOW)
    assert row["Ts"] == 1_763_400_000_000 and isinstance(row["Ts"], int)
    assert row["StudentId"] == 7 and row["ModuleId"] == 3 and row["TokenCount"] == 120
    assert row["ExpiresAt"] == datetime.fromtimestamp(int(LIVE_TTL), tz=timezone.utc)
    assert row["CreatedAt"] == datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    assert row["ResponseTime"] is None and row["Matricula"] is None


def test_rows_without_ttl_get_90_days_and_bad_dates_fall_back_to_now():
    item = chat_item(createdAt="not a date")
    del item["ttl"]
    row = tool.chat_row(item, NOW)
    assert row["ExpiresAt"] == NOW + timedelta(days=90)
    assert row["CreatedAt"] == NOW


def test_quiz_row_shape():
    row = tool.quiz_row(quiz_item(), NOW)
    assert row["QuizId"] == 11 and row["QuestionNumber"] == 1 and row["IsCorrect"] is True
    assert set(row) == set(tool.quiz_attempts._COLUMNS.values()) | {"ExpiresAt"}


def test_insert_skips_rows_with_the_same_key():
    sql = tool._insert_sql("ChatHistory", ["ConversationId", "Ts", "MessageId", "Question"],
                           ("ConversationId", "Ts", "MessageId"))
    assert "WHERE NOT EXISTS" in sql
    assert '"ConversationId" = :ConversationId AND "Ts" = :Ts AND "MessageId" = :MessageId' in sql


def test_refuses_a_database_that_is_not_postgres():
    engine = create_engine("sqlite://")
    assert tool.run(apply=True, engine=engine, scanner=lambda _: []) == 2


# --- Postgres (real) ---------------------------------------------------------

PG_URL = os.environ.get("TEST_DATABASE_URL")
pg = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def pg_engine():
    from app.core.database import sqlalchemy_url
    from app.stores.schema import ensure_schema

    engine = create_engine(sqlalchemy_url(PG_URL))
    ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM "ChatHistory"'))
        conn.execute(text('DELETE FROM "QuizAttemptAnswers"'))
    yield engine
    engine.dispose()


@pg
def test_copy_is_safe_to_run_twice_and_skips_expired(pg_engine):
    chats = [chat_item(), chat_item(ts=1_763_400_000_001, messageId="m-2"), chat_item(ts=5, ttl=DEAD_TTL)]
    dry = tool.copy_items(pg_engine, "ChatHistory", chats, apply=False, now=NOW)
    assert dry == {"read": 3, "expired": 1, "copied": 2, "already": 0}

    first = tool.copy_items(pg_engine, "ChatHistory", chats, apply=True, now=NOW)
    again = tool.copy_items(pg_engine, "ChatHistory", chats, apply=True, now=NOW)
    assert first == {"read": 3, "expired": 1, "copied": 2, "already": 0}
    assert again == {"read": 3, "expired": 1, "copied": 0, "already": 2}

    quiz = [quiz_item(question=1), quiz_item(question=2)]
    assert tool.copy_items(pg_engine, "QuizAttemptAnswers", quiz, apply=True, now=NOW)["copied"] == 2
    assert tool.copy_items(pg_engine, "QuizAttemptAnswers", quiz, apply=True, now=NOW)["already"] == 2

    with pg_engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM "ChatHistory"')).scalar() == 2
        assert conn.execute(text('SELECT count(*) FROM "QuizAttemptAnswers"')).scalar() == 2


@pg
def test_copied_rows_read_back_through_the_postgres_store(pg_engine):
    from app.stores.chat_messages import PostgresChatMessages
    from app.stores.quiz_attempts import PostgresQuizAttempts

    tool.copy_items(pg_engine, "ChatHistory", [chat_item()], apply=True)
    tool.copy_items(pg_engine, "QuizAttemptAnswers", [quiz_item()], apply=True)
    [chat] = PostgresChatMessages(pg_engine).query("student", 7)
    assert chat["conversationId"] == "conv-1" and chat["question"] == "q" and chat["timestamp"] == 1_763_400_000_000
    [answer] = PostgresQuizAttempts(pg_engine).by_student_since(7, 0, 10)
    assert answer["attemptId"] == "a-1" and answer["isCorrect"] is True
