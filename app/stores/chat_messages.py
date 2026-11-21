"""
Chat history (one item per question + answer), behind CHAT_STORE:

  dynamodb  the ChatMessages table: key (conversationId, timestamp), GSIs
            StudentActivityIndex, ModuleAnalyticsIndex, ProviderUsageIndex
  postgres  "ChatHistory" (app/stores/schema.py)

Both take and return items shaped like the DynamoDB ones (camelCase keys,
"timestamp" in epoch ms), so app/services/dynamodb_chat_service.py and its
callers don't care which one runs. Queries are always paginated to the end or
to `limit`; `newest_first` orders by timestamp.
"""

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.core.config import settings

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90

# DynamoDB attribute -> "ChatHistory" column.
_COLUMNS = {
    "conversationId": "ConversationId",
    "timestamp": "Ts",
    "messageId": "MessageId",
    "studentId": "StudentId",
    "moduleId": "ModuleId",
    "question": "Question",
    "response": "Response",
    "modelUsed": "ModelUsed",
    "provider": "Provider",
    "hasFile": "HasFile",
    "fileName": "FileName",
    "tokenCount": "TokenCount",
    "responseTime": "ResponseTime",
    "matricula": "Matricula",
    "createdAt": "CreatedAt",
}

# Our own short values with a column limit: trimmed so a surprise never fails the
# write. (FileName, the student's upload name, is text: any length is kept.)
_LIMITS = {"ModelUsed": 200, "Provider": 100, "Matricula": 100}


def clip(row: Dict[str, Any], limits: Dict[str, int] = _LIMITS) -> Dict[str, Any]:
    """Trims the string columns in `limits` to their size, in place."""
    for column, size in limits.items():
        value = row.get(column)
        if isinstance(value, str) and len(value) > size:
            row[column] = value[:size]
    return row


# Which key each query filters on, and its DynamoDB index.
_FILTERS = {
    "conversation": ("conversationId", "ConversationId", None),
    "student": ("studentId", "StudentId", "StudentActivityIndex"),
    "module": ("moduleId", "ModuleId", "ModuleAnalyticsIndex"),
    "provider": ("provider", "Provider", "ProviderUsageIndex"),
}


class DynamoChatMessages:
    def __init__(self):
        from app.core.aws import dynamodb_resource

        self.table = dynamodb_resource().Table(settings.AWS_DYNAMODB_CHAT_TABLE)

    def put(self, item: Dict[str, Any]) -> None:
        item = dict(item)
        item.setdefault("ttl", int(datetime.now(timezone.utc).timestamp()) + RETENTION_DAYS * 86400)
        self.table.put_item(Item=item)

    def query(
        self,
        by: str,
        value: Any,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        newest_first: bool = False,
        limit: Optional[int] = None,
        keys_only: bool = False,
    ) -> List[Dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        attribute, _, index = _FILTERS[by]
        condition = Key(attribute).eq(value)
        if start_ts is not None and end_ts is not None:
            condition = condition & Key("timestamp").between(start_ts, end_ts)
        elif start_ts is not None:
            condition = condition & Key("timestamp").gte(start_ts)
        kwargs: Dict[str, Any] = {"KeyConditionExpression": condition, "ScanIndexForward": not newest_first}
        if index:
            kwargs["IndexName"] = index
        if keys_only:
            # 'timestamp' is a DynamoDB reserved word
            kwargs["ProjectionExpression"] = "conversationId, #ts"
            kwargs["ExpressionAttributeNames"] = {"#ts": "timestamp"}
        items: List[Dict[str, Any]] = []
        last_key = None
        while True:
            if limit is not None:
                kwargs["Limit"] = limit - len(items)
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            last_key = response.get("LastEvaluatedKey")
            if not last_key or (limit is not None and len(items) >= limit):
                return items

    def delete_student(self, student_id: int) -> int:
        items = self.query("student", student_id, keys_only=True)
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"conversationId": item["conversationId"], "timestamp": item["timestamp"]})
        return len(items)


class PostgresChatMessages:
    def __init__(self, engine=None):
        if engine is None:
            from app.core.database import engine as default_engine

            engine = default_engine
        self.engine = engine

    def put(self, item: Dict[str, Any]) -> None:
        row = {column: item.get(attribute) for attribute, column in _COLUMNS.items()}
        now = datetime.now(timezone.utc)
        created = row["CreatedAt"]
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        row["CreatedAt"] = created or now
        row["Ts"] = int(row["Ts"] or now.timestamp() * 1000)
        row["StudentId"] = int(row["StudentId"] or 0)
        row["ModuleId"] = int(row["ModuleId"] or 0)
        row["Question"] = row["Question"] or ""
        row["Response"] = row["Response"] or ""
        row["HasFile"] = bool(row["HasFile"])
        row["ExpiresAt"] = now + timedelta(days=RETENTION_DAYS)
        clip(row)
        columns = ", ".join(f'"{c}"' for c in row)
        values = ", ".join(f":{c}" for c in row)
        with self.engine.begin() as conn:
            conn.execute(text(f'INSERT INTO "ChatHistory" ({columns}) VALUES ({values})'), row)  # noqa: S608

    def query(
        self,
        by: str,
        value: Any,
        *,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        newest_first: bool = False,
        limit: Optional[int] = None,
        keys_only: bool = False,
    ) -> List[Dict[str, Any]]:
        _, column, _ = _FILTERS[by]
        where = [f'"{column}" = :value', '"ExpiresAt" > now()']
        params: Dict[str, Any] = {"value": value}
        if start_ts is not None:
            where.append('"Ts" >= :start_ts')
            params["start_ts"] = start_ts
        if end_ts is not None:
            where.append('"Ts" <= :end_ts')
            params["end_ts"] = end_ts
        order = "DESC" if newest_first else "ASC"
        sql = (
            f'SELECT {", ".join(chr(34) + c + chr(34) for c in _COLUMNS.values())} FROM "ChatHistory" '  # noqa: S608
            f'WHERE {" AND ".join(where)} ORDER BY "Ts" {order}, "Id" {order}'
        )
        if limit is not None:
            sql += " LIMIT :limit"
            params["limit"] = limit
        with self.engine.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [_item(row) for row in rows]

    def delete_student(self, student_id: int) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(text('DELETE FROM "ChatHistory" WHERE "StudentId" = :s'), {"s": student_id})
        return result.rowcount or 0


def _item(row) -> Dict[str, Any]:
    item: Dict[str, Any] = {}
    for attribute, column in _COLUMNS.items():
        value = row[column]
        if value is None:
            continue  # absent, as an optional DynamoDB attribute would be
        if attribute == "createdAt":
            value = value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()
        item[attribute] = value
    return item


@lru_cache
def chat_messages():
    """The store for this deployment (CHAT_STORE)."""
    if settings.CHAT_STORE == "postgres":
        return PostgresChatMessages()
    return DynamoChatMessages()
