"""
Quiz Maintenance Worker - Daily Background Job

Runs daily to ensure all modules have quiz questions generated.
Useful for:
- Modules created before quiz generation was implemented
- Modules where quiz generation failed
- Modules with files added after creation
"""
import logging
import asyncio
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Module, Quiz
from app.services.quiz_generator import QuizGeneratorService

logger = logging.getLogger(__name__)


async def check_and_regenerate_quizzes():
    """
    Check all modules and regenerate quizzes if needed.

    Regenerates quizzes if:
    1. Module has no quizzes but has files
    2. Module's files were updated after quizzes were created
    """
    logger.info("🔍 Starting daily quiz maintenance check...")

    db: Session = next(get_db())

    try:
        # Get all active modules
        modules = db.query(Module).filter(Module.is_active == True).all()

        regenerated_count = 0
        skipped_count = 0
        error_count = 0

        for module in modules:
            try:
                # Check if module has files
                if not module.files or len(module.files) == 0:
                    logger.debug(f"⏭️  Module {module.id} ({module.name}) has no files, skipping")
                    skipped_count += 1
                    continue

                # Check if module has AI-generated quizzes (question bank / uploaded quizzes are managed separately)
                quiz_count = db.query(Quiz).filter(
                    Quiz.module_id == module.id,
                    Quiz.source == "ai_generated"
                ).count()

                if quiz_count == 0:
                    # No quizzes - generate them
                    logger.info(f"📝 Module {module.id} ({module.name}) has {len(module.files)} files but no quizzes - generating...")
                    generator = QuizGeneratorService(module, db)
                    await generator.generate_quiz_bank(count=50)
                    regenerated_count += 1
                    continue

                # Check if files were updated after quizzes
                latest_file_update = max([f.updated_at for f in module.files if f.is_active])
                latest_quiz = db.query(Quiz).filter(
                    Quiz.module_id == module.id,
                    Quiz.source == "ai_generated"
                ).order_by(Quiz.created_at.desc()).first()

                if latest_quiz and latest_file_update > latest_quiz.created_at:
                    # Files updated after quizzes - regenerate AI quizzes only
                    logger.info(f"🔄 Module {module.id} ({module.name}) has files updated after quizzes - regenerating AI quizzes...")

                    # Delete only AI-generated quizzes, preserve question bank / uploaded quizzes
                    db.query(Quiz).filter(Quiz.module_id == module.id, Quiz.source == "ai_generated").delete()
                    db.commit()

                    # Generate new quizzes
                    generator = QuizGeneratorService(module, db)
                    await generator.generate_quiz_bank(count=50)
                    regenerated_count += 1
                else:
                    logger.debug(f"✅ Module {module.id} ({module.name}) quizzes are up to date")
                    skipped_count += 1

            except Exception as e:
                logger.error(f"❌ Error processing module {module.id}: {e}")
                error_count += 1
                continue

        logger.info(
            f"✅ Daily quiz maintenance complete: "
            f"{regenerated_count} regenerated, {skipped_count} skipped, {error_count} errors"
        )

    except Exception as e:
        logger.error(f"❌ Fatal error in quiz maintenance worker: {e}")
    finally:
        db.close()


async def run_daily_maintenance():
    """
    Run the maintenance task in a loop (every 24 hours).
    """
    while True:
        try:
            # Run at 2 AM (low traffic time)
            now = datetime.now()
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)

            # If it's already past 2 AM today, schedule for tomorrow
            if now.hour >= 2:
                next_run += timedelta(days=1)

            wait_seconds = (next_run - now).total_seconds()
            logger.info(f"⏰ Next quiz maintenance scheduled for {next_run} ({wait_seconds/3600:.1f} hours)")

            await asyncio.sleep(wait_seconds)
            await check_and_regenerate_quizzes()

        except Exception as e:
            logger.error(f"❌ Error in maintenance loop: {e}")
            # Wait 1 hour before retrying on error
            await asyncio.sleep(3600)


# For manual testing/triggering
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(check_and_regenerate_quizzes())
