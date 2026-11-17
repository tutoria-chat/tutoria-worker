"""
Chat history and quiz answers for the analytics workers.

The reads go through the CHAT_STORE stores (app/stores/): DynamoDB or
Postgres. get_chat_table / get_quiz_attempts_table / batch_add_ttl are the
DynamoDB-only leftovers (the one-time TTL backfill).
"""
import logging
import time
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)

_dynamodb = None
_chat_table = None
_quiz_attempts_table = None


def _get_dynamodb_resource():
    """Lazy initialize DynamoDB resource."""
    global _dynamodb
    if _dynamodb is not None:
        return _dynamodb

    try:
        from app.core.aws import dynamodb_resource
        _dynamodb = dynamodb_resource()
        logger.info("DynamoDB resource initialized")
        return _dynamodb
    except ImportError:
        logger.warning("boto3 not installed. DynamoDB features disabled.")
        return None
    except Exception as e:
        logger.error(f"Failed to initialize DynamoDB: {e}")
        return None


def get_chat_table():
    """Get the ChatMessages DynamoDB table."""
    global _chat_table
    if _chat_table is not None:
        return _chat_table

    dynamodb = _get_dynamodb_resource()
    if dynamodb is None:
        return None

    _chat_table = dynamodb.Table(settings.AWS_DYNAMODB_CHAT_TABLE)
    return _chat_table


def get_quiz_attempts_table():
    """Get the QuizAttempts DynamoDB table."""
    global _quiz_attempts_table
    if _quiz_attempts_table is not None:
        return _quiz_attempts_table

    dynamodb = _get_dynamodb_resource()
    if dynamodb is None:
        return None

    _quiz_attempts_table = dynamodb.Table(settings.AWS_DYNAMODB_QUIZ_ATTEMPTS_TABLE)
    return _quiz_attempts_table


def query_module_messages(module_id: int, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """
    All chat messages for a module in a time range (epoch ms), oldest first.
    CHAT_STORE: the DynamoDB ModuleAnalyticsIndex or Postgres "ChatHistory".
    """
    from app.stores.chat_messages import chat_messages

    try:
        store = chat_messages()
    except Exception as e:
        logger.warning(f"[Chat store unavailable] Cannot query module messages: {e}")
        return []
    return store.query("module", module_id, start_ts=start_ts, end_ts=end_ts)


def query_module_quiz_attempts(module_id: int, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """
    Quiz answers for a module in a time range (epoch ms), oldest first.
    CHAT_STORE: the DynamoDB QuizAttempts table or Postgres "QuizAttemptAnswers".
    """
    from app.stores.quiz_attempts import quiz_attempts

    try:
        store = quiz_attempts()
    except Exception as e:
        logger.warning(f"[Quiz store unavailable] Cannot query quiz attempts: {e}")
        return []
    return store.by_module_between(module_id, start_ts, end_ts)


def save_quiz_attempt(
    module_id: int,
    student_id: int,
    quiz_id: int,
    question_number: int,
    selected_answer: str,
    correct_answer: str,
    is_correct: bool,
    concepts_covered: str = "",
    difficulty: str = "medium",
) -> str:
    """Save a single quiz answer (90-day retention)."""
    from app.stores.quiz_attempts import quiz_attempts

    attempt_id = str(uuid.uuid4())
    quiz_attempts().put({
        'moduleId': module_id,
        'timestamp': int(time.time() * 1000),
        'attemptId': attempt_id,
        'studentId': student_id,
        'quizId': quiz_id,
        'questionNumber': question_number,
        'selectedAnswer': selected_answer,
        'correctAnswer': correct_answer,
        'isCorrect': is_correct,
        'conceptsCovered': concepts_covered,
        'difficulty': difficulty,
        'createdAt': datetime.utcnow().isoformat(),
    })
    return attempt_id


def batch_add_ttl(table_name: str = "chat", items_to_update: Optional[List[Dict]] = None):
    """
    Add TTL attribute to existing items that are missing it.
    Used for one-time backfill.

    Args:
        table_name: "chat" for ChatMessages, "quiz" for QuizAttempts
        items_to_update: If provided, update these specific items.
                        Otherwise scans for items missing 'ttl'.
    """
    if settings.CHAT_STORE != "dynamodb":
        logger.info("TTL backfill is DynamoDB only (CHAT_STORE=%s): nothing to do", settings.CHAT_STORE)
        return 0
    table = get_chat_table() if table_name == "chat" else get_quiz_attempts_table()
    if table is None:
        logger.warning("[DynamoDB DISABLED] Cannot backfill TTL")
        return 0

    updated_count = 0

    if items_to_update is None:
        # Scan for items missing ttl
        scan_kwargs = {
            'FilterExpression': 'attribute_not_exists(#ttl)',
            'ExpressionAttributeNames': {'#ttl': 'ttl'},
            'Limit': 100,
        }
        exclusive_start_key = None

        while True:
            if exclusive_start_key:
                scan_kwargs['ExclusiveStartKey'] = exclusive_start_key

            response = table.scan(**scan_kwargs)
            items = response.get('Items', [])

            for item in items:
                ts = item.get('timestamp', 0)
                ttl_value = int(ts / 1000) + (90 * 86400)

                # Determine key based on table
                if table_name == "chat":
                    key = {'conversationId': item['conversationId'], 'timestamp': item['timestamp']}
                else:
                    key = {'moduleId': item['moduleId'], 'timestamp': item['timestamp']}

                try:
                    table.update_item(
                        Key=key,
                        UpdateExpression='SET #ttl = :ttl_val',
                        ExpressionAttributeNames={'#ttl': 'ttl'},
                        ExpressionAttributeValues={':ttl_val': ttl_value},
                    )
                    updated_count += 1
                except Exception as e:
                    logger.error(f"Failed to update TTL for item: {e}")

                if updated_count % 1000 == 0 and updated_count > 0:
                    logger.info(f"TTL backfill progress: {updated_count} items updated")

            exclusive_start_key = response.get('LastEvaluatedKey')
            if not exclusive_start_key:
                break

    logger.info(f"TTL backfill complete: {updated_count} items updated in {table_name} table")
    return updated_count
