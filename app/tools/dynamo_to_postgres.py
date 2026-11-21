"""
Copies the DynamoDB chat and quiz tables into their Postgres tables
(app/stores/schema.py), so CHAT_STORE can move from dynamodb to postgres
without losing history.

    python -m app.tools.dynamo_to_postgres            counts only, writes nothing
    python -m app.tools.dynamo_to_postgres --apply    copies

Reads the tables this deployment is configured for (AWS_DYNAMODB_CHAT_TABLE,
AWS_DYNAMODB_QUIZ_ATTEMPTS_TABLE) and writes to DATABASE_URL. Safe to run any
number of times: a row already in Postgres (same DynamoDB key) is skipped, so
the usual sequence is copy -> flip CHAT_STORE -> copy again for whatever was
written in between. Items past their DynamoDB TTL are skipped, and each row
keeps the item's own expiry instead of starting a new 90 days.

Prints counts only, never message contents. On ECS it runs as a one-off task
of the worker's task definition, so it uses the task role and the worker's
SSM config.
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, Optional

from sqlalchemy import text

from app.stores import chat_messages, quiz_attempts

logger = logging.getLogger("dynamo_to_postgres")

_FALLBACK_DAYS = 90


def _plain(value: Any) -> Any:
    """boto3 returns every number as Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _created(value: Any, now: datetime) -> datetime:
    if isinstance(value, str) and value:
        try:
            created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return now
        return created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return now


def _expires(item: Dict[str, Any], now: datetime) -> datetime:
    ttl = _plain(item.get("ttl"))
    if isinstance(ttl, (int, float)) and ttl > 0:
        return datetime.fromtimestamp(ttl, tz=timezone.utc)
    return now + timedelta(days=_FALLBACK_DAYS)


def chat_row(item: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    row = {column: _plain(item.get(attribute)) for attribute, column in chat_messages._COLUMNS.items()}
    row.update({
        "Ts": int(row["Ts"] or 0),
        "MessageId": str(row["MessageId"] or ""),
        "StudentId": int(row["StudentId"] or 0),
        "ModuleId": int(row["ModuleId"] or 0),
        "Question": row["Question"] or "",
        "Response": row["Response"] or "",
        "HasFile": bool(row["HasFile"]),
        "TokenCount": int(row["TokenCount"]) if row["TokenCount"] is not None else None,
        "ResponseTime": int(row["ResponseTime"]) if row["ResponseTime"] is not None else None,
        "Matricula": str(row["Matricula"]) if row["Matricula"] is not None else None,
        "CreatedAt": _created(row["CreatedAt"], now),
        "ExpiresAt": _expires(item, now),
    })
    return chat_messages.clip(row)


def quiz_row(item: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    row = {column: _plain(item.get(attribute)) for attribute, column in quiz_attempts._COLUMNS.items()}
    row.update({
        "AttemptId": str(row["AttemptId"] or ""),
        "ModuleId": int(row["ModuleId"] or 0),
        "Ts": int(row["Ts"] or 0),
        "StudentId": int(row["StudentId"] or 0),
        "QuizId": int(row["QuizId"]) if row["QuizId"] is not None else None,
        "QuestionNumber": int(row["QuestionNumber"] or 0),
        "SelectedAnswer": str(row["SelectedAnswer"] or ""),
        "CorrectAnswer": str(row["CorrectAnswer"] or ""),
        "IsCorrect": bool(row["IsCorrect"]),
        "ConceptsCovered": str(row["ConceptsCovered"] or ""),
        "Difficulty": str(row["Difficulty"] or "medium"),
        "CreatedAt": _created(row["CreatedAt"], now),
        "ExpiresAt": _expires(item, now),
    })
    return chat_messages.clip(row, quiz_attempts._LIMITS)


# Table -> (row builder, the DynamoDB key as columns: a row with the same
# values is the same item).
TABLES = {
    "ChatHistory": (chat_row, ("ConversationId", "Ts", "MessageId")),
    "QuizAttemptAnswers": (quiz_row, ("ModuleId", "Ts", "AttemptId", "QuestionNumber")),
}


def _insert_sql(table: str, columns: Iterable[str], key: Iterable[str]) -> str:
    columns = list(columns)
    names = ", ".join(f'"{c}"' for c in columns)
    values = ", ".join(f":{c}" for c in columns)
    match = " AND ".join(f'"{c}" = :{c}' for c in key)
    # Constants only: table and column names come from TABLES and _COLUMNS.
    return (
        f'INSERT INTO "{table}" ({names}) SELECT {values} '  # noqa: S608
        f'WHERE NOT EXISTS (SELECT 1 FROM "{table}" WHERE {match})'
    )


def copy_items(engine, table: str, items: Iterable[Dict[str, Any]], apply: bool,
               now: Optional[datetime] = None) -> Dict[str, int]:
    """Copies DynamoDB-shaped items into `table`. Returns the counts."""
    build, key = TABLES[table]
    now = now or datetime.now(timezone.utc)
    counts = {"read": 0, "expired": 0, "copied": 0, "already": 0}
    sql = None
    with engine.begin() as conn:
        for item in items:
            counts["read"] += 1
            row = build(item, now)
            if row["ExpiresAt"] <= now:
                counts["expired"] += 1
                continue
            if not apply:
                exists = conn.execute(text(
                    f'SELECT 1 FROM "{table}" WHERE '  # noqa: S608
                    + " AND ".join(f'"{c}" = :{c}' for c in key)
                ), {c: row[c] for c in key}).first()
                counts["already" if exists else "copied"] += 1
                continue
            sql = sql or _insert_sql(table, row.keys(), key)
            inserted = conn.execute(text(sql), row).rowcount
            counts["copied" if inserted else "already"] += 1
    return counts


def scan(table_name: str) -> Iterable[Dict[str, Any]]:
    """Every item of a DynamoDB table, page by page."""
    from app.core.aws import dynamodb_resource

    table = dynamodb_resource().Table(table_name)
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        if not response.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def run(apply: bool, engine=None, scanner: Callable[[str], Iterable[Dict[str, Any]]] = scan) -> int:
    from app.core.config import settings
    from app.stores.schema import ensure_schema

    if engine is None:
        from app.core.database import engine as default_engine

        engine = default_engine
    if engine.dialect.name != "postgresql":
        logger.error("DATABASE_URL is not Postgres (%s): nothing copied", engine.dialect.name)
        return 2
    ensure_schema(engine)
    sources = {
        "ChatHistory": settings.AWS_DYNAMODB_CHAT_TABLE,
        "QuizAttemptAnswers": settings.AWS_DYNAMODB_QUIZ_ATTEMPTS_TABLE,
    }
    for table, source in sources.items():
        started = time.monotonic()
        counts = copy_items(engine, table, scanner(source), apply)
        with engine.connect() as conn:
            total = conn.execute(text(f'SELECT count(*) FROM "{table}" WHERE "ExpiresAt" > now()')).scalar()  # noqa: S608
        verb = "copied" if apply else "would copy"
        logger.info(
            "%s -> %s: read %d, %s %d, already there %d, expired %d; %s now holds %d live rows (%.1fs)",
            source, table, counts["read"], verb, counts["copied"], counts["already"], counts["expired"],
            table, total, time.monotonic() - started,
        )
    if not apply:
        logger.info("Nothing written. Run with --apply to copy.")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("botocore", "boto3", "urllib3", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write (default: counts only)")
    return run(parser.parse_args(argv).apply)


if __name__ == "__main__":
    sys.exit(main())
