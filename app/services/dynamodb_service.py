"""
DynamoDB Service for tutoria-worker.

Provides access to ChatMessages and QuizAttempts tables for analytics workers.
Reuses the same lazy-init pattern as tutoria-api/dynamodb_chat_service.py.
"""
import logging
import time
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

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
        import boto3
        _dynamodb = boto3.resource(
            'dynamodb',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION
        )
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
    Query all chat messages for a module in a time range using ModuleAnalyticsIndex.
    Handles pagination automatically.

    Args:
        module_id: Module ID to query
        start_ts: Start timestamp in epoch milliseconds
        end_ts: End timestamp in epoch milliseconds

    Returns:
        List of message items
    """
    table = get_chat_table()
    if table is None:
        logger.warning("[DynamoDB DISABLED] Cannot query module messages")
        return []

    from boto3.dynamodb.conditions import Key

    all_items = []
    exclusive_start_key = None

    while True:
        query_kwargs = {
            'IndexName': 'ModuleAnalyticsIndex',
            'KeyConditionExpression': Key('moduleId').eq(module_id) & Key('timestamp').between(start_ts, end_ts),
        }
        if exclusive_start_key:
            query_kwargs['ExclusiveStartKey'] = exclusive_start_key

        response = table.query(**query_kwargs)
        all_items.extend(response.get('Items', []))

        exclusive_start_key = response.get('LastEvaluatedKey')
        if not exclusive_start_key:
            break

    return all_items


def query_module_quiz_attempts(module_id: int, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
    """
    Query quiz attempts for a module in a time range.
    QuizAttempts table PK: moduleId + timestamp.

    Args:
        module_id: Module ID to query
        start_ts: Start timestamp in epoch milliseconds
        end_ts: End timestamp in epoch milliseconds

    Returns:
        List of quiz attempt items
    """
    table = get_quiz_attempts_table()
    if table is None:
        logger.warning("[DynamoDB DISABLED] Cannot query quiz attempts")
        return []

    from boto3.dynamodb.conditions import Key

    all_items = []
    exclusive_start_key = None

    while True:
        query_kwargs = {
            'KeyConditionExpression': Key('moduleId').eq(module_id) & Key('timestamp').between(start_ts, end_ts),
        }
        if exclusive_start_key:
            query_kwargs['ExclusiveStartKey'] = exclusive_start_key

        response = table.query(**query_kwargs)
        all_items.extend(response.get('Items', []))

        exclusive_start_key = response.get('LastEvaluatedKey')
        if not exclusive_start_key:
            break

    return all_items


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
    """Save a single quiz attempt to DynamoDB with 90-day TTL."""
    table = get_quiz_attempts_table()
    attempt_id = str(uuid.uuid4())
    timestamp = int(time.time() * 1000)
    ttl_seconds = int(time.time()) + (90 * 86400)

    if table is None:
        logger.info(f"[DynamoDB DISABLED] Would save quiz attempt: module={module_id}, student={student_id}")
        return attempt_id

    item = {
        'moduleId': module_id,
        'timestamp': timestamp,
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
        'ttl': ttl_seconds,
    }

    try:
        table.put_item(Item=item)
        logger.debug(f"Saved quiz attempt {attempt_id} to DynamoDB")
        return attempt_id
    except Exception as e:
        logger.error(f"Failed to save quiz attempt to DynamoDB: {e}")
        raise


def batch_add_ttl(table_name: str = "chat", items_to_update: Optional[List[Dict]] = None):
    """
    Add TTL attribute to existing items that are missing it.
    Used for one-time backfill.

    Args:
        table_name: "chat" for ChatMessages, "quiz" for QuizAttempts
        items_to_update: If provided, update these specific items.
                        Otherwise scans for items missing 'ttl'.
    """
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
