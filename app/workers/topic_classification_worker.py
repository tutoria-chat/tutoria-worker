"""
Topic Classification Worker — Daily Background Job

Runs at 3:00 AM. Pulls yesterday's questions from DynamoDB per module,
sends to AI for topic classification, stores in TopicClassifications SQL.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, date, timezone

from app.core.database import SessionLocal
from app.workers.scheduling import seconds_until_next_run

logger = logging.getLogger(__name__)

CLASSIFICATION_HOUR = 3
CLASSIFICATION_MINUTE = 0
MIN_QUESTIONS_FOR_CLASSIFICATION = 5


async def classify_topics_for_date(target_date: date):
    """Classify yesterday's questions into academic topics using AI."""
    from app.models.module import Module
    from app.models.topic_classification import TopicClassification
    from app.services.dynamodb_service import query_module_messages
    from app.services.topic_classification_service import TopicClassificationService

    start_of_day = datetime.combine(target_date, datetime.min.time(), tzinfo=timezone.utc)
    end_of_day = datetime.combine(target_date, datetime.max.time(), tzinfo=timezone.utc)
    start_ts = int(start_of_day.timestamp() * 1000)
    end_ts = int(end_of_day.timestamp() * 1000)

    db = SessionLocal()
    try:
        modules = db.query(Module).filter(Module.is_active == True).all()
        if not modules:
            logger.info("No active modules found, skipping topic classification")
            return

        classified = 0
        for module in modules:
            try:
                messages = await asyncio.to_thread(query_module_messages, module.id, start_ts, end_ts)
                questions = [msg.get('question', '') for msg in messages if msg.get('question')]

                if len(questions) < MIN_QUESTIONS_FOR_CLASSIFICATION:
                    continue

                # Use AI to classify topics (3-tier provider chain)
                service = TopicClassificationService(db=db)
                topics = await service.classify(questions)

                if not topics:
                    continue

                # Delete existing classifications for this module+date
                db.query(TopicClassification).filter(
                    TopicClassification.module_id == module.id,
                    TopicClassification.date == target_date,
                ).delete()

                # Insert new classifications
                for topic in topics:
                    tc = TopicClassification(
                        module_id=module.id,
                        date=target_date,
                        topic_name=topic.get('topic', 'Unknown'),
                        question_count=topic.get('count', 0),
                        sample_questions=json.dumps(topic.get('samples', [])[:3]),
                    )
                    db.add(tc)

                classified += 1

            except Exception as e:
                logger.error(f"Error classifying topics for module {module.id}: {e}")
                continue

        db.commit()
        logger.info(f"✅ Topic classification complete: {classified} modules for {target_date}")

    except Exception as e:
        logger.error(f"Error in topic classification: {e}")
        db.rollback()
    finally:
        db.close()


async def run_topic_classification_worker():
    """Daily loop at 3:00 AM."""
    while True:
        try:
            wait = seconds_until_next_run(CLASSIFICATION_HOUR, CLASSIFICATION_MINUTE)
            next_run = datetime.now() + timedelta(seconds=wait)
            logger.info(f"🏷️ Next topic classification at {next_run.strftime('%Y-%m-%d %H:%M')} ({wait/3600:.1f}h)")
            await asyncio.sleep(wait)

            yesterday = date.today() - timedelta(days=1)
            logger.info(f"🏷️ Starting topic classification for {yesterday}...")
            await classify_topics_for_date(yesterday)

        except asyncio.CancelledError:
            logger.info("🛑 Topic classification worker cancelled")
            return
        except Exception as e:
            logger.error(f"Error in topic classification loop: {e}")
            await asyncio.sleep(300)
