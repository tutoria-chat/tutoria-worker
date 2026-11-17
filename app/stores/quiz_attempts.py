"""
Quiz answers (one item per answered question), behind CHAT_STORE:

  dynamodb  the QuizAttempts table: key (moduleId, timestamp), GSI StudentQuizIndex
  postgres  "QuizAttemptAnswers" (app/stores/schema.py)

Items in and out are shaped like the DynamoDB ones (camelCase keys).
"""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.core.config import settings

RETENTION_DAYS = 90

_COLUMNS = {
    "attemptId": "AttemptId",
    "moduleId": "ModuleId",
    "timestamp": "Ts",
    "studentId": "StudentId",
    "quizId": "QuizId",
    "questionNumber": "QuestionNumber",
    "selectedAnswer": "SelectedAnswer",
    "correctAnswer": "CorrectAnswer",
    "isCorrect": "IsCorrect",
    "conceptsCovered": "ConceptsCovered",
    "difficulty": "Difficulty",
    "createdAt": "CreatedAt",
}


class DynamoQuizAttempts:
    def __init__(self):
        from app.core.aws import dynamodb_resource

        self.table = dynamodb_resource().Table(settings.AWS_DYNAMODB_QUIZ_ATTEMPTS_TABLE)

    def put(self, item: Dict[str, Any]) -> None:
        item = dict(item)
        item.setdefault("ttl", int(datetime.now(timezone.utc).timestamp()) + RETENTION_DAYS * 86400)
        self.table.put_item(Item=item)

    def by_student_since(self, student_id: int, since_ms: int, limit: int) -> List[Dict[str, Any]]:
        """Newest first."""
        from boto3.dynamodb.conditions import Key

        response = self.table.query(
            IndexName="StudentQuizIndex",
            KeyConditionExpression=Key("studentId").eq(student_id) & Key("timestamp").gte(since_ms),
            ScanIndexForward=False,
            Limit=limit,
        )
        return response.get("Items", [])

    def by_module_between(self, module_id: int, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """Oldest first, every page."""
        from boto3.dynamodb.conditions import Key

        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("moduleId").eq(module_id) & Key("timestamp").between(start_ts, end_ts),
        }
        items: List[Dict[str, Any]] = []
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


class PostgresQuizAttempts:
    def __init__(self, engine=None):
        if engine is None:
            from app.core.database import engine as default_engine

            engine = default_engine
        self.engine = engine

    def put(self, item: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        row = {column: item.get(attribute) for attribute, column in _COLUMNS.items()}
        created = row["CreatedAt"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        row.update({
            "Ts": int(row["Ts"] or now.timestamp() * 1000),
            "StudentId": int(row["StudentId"] or 0),
            "QuestionNumber": int(row["QuestionNumber"] or 0),
            "SelectedAnswer": str(row["SelectedAnswer"] or ""),
            "CorrectAnswer": str(row["CorrectAnswer"] or ""),
            "IsCorrect": bool(row["IsCorrect"]),
            "ConceptsCovered": str(row["ConceptsCovered"] or ""),
            "Difficulty": str(row["Difficulty"] or "medium"),
            "CreatedAt": created or now,
            "ExpiresAt": now + timedelta(days=RETENTION_DAYS),
        })
        columns = ", ".join(f'"{c}"' for c in row)
        values = ", ".join(f":{c}" for c in row)
        with self.engine.begin() as conn:
            conn.execute(text(f'INSERT INTO "QuizAttemptAnswers" ({columns}) VALUES ({values})'), row)  # noqa: S608

    def by_student_since(self, student_id: int, since_ms: int, limit: int) -> List[Dict[str, Any]]:
        """Newest first."""
        return self._select('"StudentId" = :key AND "Ts" >= :start', {"key": student_id, "start": since_ms},
                            newest_first=True, limit=limit)

    def by_module_between(self, module_id: int, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """Oldest first."""
        return self._select('"ModuleId" = :key AND "Ts" BETWEEN :start AND :end',
                            {"key": module_id, "start": start_ts, "end": end_ts})

    def _select(self, where: str, params: Dict[str, Any], newest_first: bool = False,
                limit: Optional[int] = None) -> List[Dict[str, Any]]:
        direction = "DESC" if newest_first else "ASC"
        sql = (
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in _COLUMNS.values())} FROM "QuizAttemptAnswers" '  # noqa: S608
            f'WHERE {where} AND "ExpiresAt" > now() '
            f'ORDER BY "Ts" {direction}, "Id" {direction}'
        )
        if limit is not None:
            sql += " LIMIT :limit"
            params = {**params, "limit": limit}
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        items = []
        for row in rows:
            item = {}
            for attribute, column in _COLUMNS.items():
                value = row[column]
                if value is None:
                    continue
                if attribute == "createdAt":
                    value = value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
                item[attribute] = value
            items.append(item)
        return items


@lru_cache
def quiz_attempts():
    """The store for this deployment (CHAT_STORE)."""
    if settings.CHAT_STORE == "postgres":
        return PostgresQuizAttempts()
    return DynamoQuizAttempts()
