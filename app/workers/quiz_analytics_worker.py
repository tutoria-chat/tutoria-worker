"""
Quiz Analytics Worker — Daily Background Job

Runs at 3:30 AM. Queries yesterday's QuizAttempts from DynamoDB per module,
aggregates pass/fail per concept, stores in QuizAnalytics SQL.
"""
import asyncio
import logging
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict
from decimal import Decimal

from app.core.database import SessionLocal
from app.workers.scheduling import seconds_until_next_run

logger = logging.getLogger(__name__)

QUIZ_ANALYTICS_HOUR = 3
QUIZ_ANALYTICS_MINUTE = 30


async def aggregate_quiz_analytics():
    """Aggregate yesterday's quiz attempts into QuizAnalytics."""
    from app.models.module import Module
    from app.models.quiz_analytics import QuizAnalytic
    from app.services.dynamodb_service import query_module_quiz_attempts

    yesterday = date.today() - timedelta(days=1)
    start_of_day = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
    end_of_day = datetime.combine(yesterday, datetime.max.time(), tzinfo=timezone.utc)
    start_ts = int(start_of_day.timestamp() * 1000)
    end_ts = int(end_of_day.timestamp() * 1000)

    db = SessionLocal()
    try:
        modules = db.query(Module).filter(Module.is_active == True).all()
        if not modules:
            logger.info("No active modules found, skipping quiz analytics")
            return

        aggregated = 0
        for module in modules:
            try:
                attempts = await asyncio.to_thread(query_module_quiz_attempts, module.id, start_ts, end_ts)
                if not attempts:
                    continue

                # Group by concept
                concepts = defaultdict(lambda: {"total": 0, "correct": 0, "incorrect": 0, "difficulty": None})

                for attempt in attempts:
                    concept_str = attempt.get('conceptsCovered', '')
                    is_correct = attempt.get('isCorrect', False)
                    difficulty = attempt.get('difficulty', 'medium')

                    # A question can cover multiple concepts (comma-separated)
                    concept_names = [c.strip() for c in concept_str.split(",") if c.strip()] if concept_str else ["General"]

                    for concept in concept_names:
                        concepts[concept]["total"] += 1
                        if is_correct:
                            concepts[concept]["correct"] += 1
                        else:
                            concepts[concept]["incorrect"] += 1
                        concepts[concept]["difficulty"] = difficulty

                # Delete existing QuizAnalytics for this module
                db.query(QuizAnalytic).filter(QuizAnalytic.module_id == module.id).delete()

                # Insert fresh aggregated rows
                for concept_name, stats in concepts.items():
                    total = stats["total"]
                    correct = stats["correct"]
                    success_rate = Decimal(str(round((correct / total) * 100, 2))) if total > 0 else Decimal('0')

                    qa = QuizAnalytic(
                        module_id=module.id,
                        concept_name=concept_name,
                        total_attempts=total,
                        correct_count=correct,
                        incorrect_count=stats["incorrect"],
                        success_rate=success_rate,
                        difficulty=stats["difficulty"],
                        last_updated_at=datetime.utcnow(),
                    )
                    db.add(qa)

                aggregated += 1

            except Exception as e:
                logger.error(f"Error aggregating quiz analytics for module {module.id}: {e}")
                continue

        db.commit()
        logger.info(f"✅ Quiz analytics aggregation complete: {aggregated} modules")

    except Exception as e:
        logger.error(f"Error in quiz analytics aggregation: {e}")
        db.rollback()
    finally:
        db.close()


async def run_quiz_analytics_worker():
    """Daily loop at 3:30 AM."""
    while True:
        try:
            wait = seconds_until_next_run(QUIZ_ANALYTICS_HOUR, QUIZ_ANALYTICS_MINUTE)
            next_run = datetime.now() + timedelta(seconds=wait)
            logger.info(f"📝 Next quiz analytics aggregation at {next_run.strftime('%Y-%m-%d %H:%M')} ({wait/3600:.1f}h)")
            await asyncio.sleep(wait)

            logger.info("📝 Starting quiz analytics aggregation...")
            await aggregate_quiz_analytics()

        except asyncio.CancelledError:
            logger.info("🛑 Quiz analytics worker cancelled")
            return
        except Exception as e:
            logger.error(f"Error in quiz analytics loop: {e}")
            await asyncio.sleep(300)
