"""
Analytics Aggregation Worker — Daily Background Job

Runs at 2:30 AM. Queries yesterday's DynamoDB ChatMessages per module,
aggregates into AnalyticsDailySummary PostgreSQL rows.
"""
import asyncio
import logging
from datetime import datetime, timedelta, date, timezone
from collections import Counter
from decimal import Decimal

from app.core.database import SessionLocal
from app.workers.scheduling import seconds_until_next_run

logger = logging.getLogger(__name__)

AGGREGATION_HOUR = 2
AGGREGATION_MINUTE = 30

# Cost calculation ratios (same as .NET AnalyticsService)
INPUT_TOKEN_RATIO = 0.25
OUTPUT_TOKEN_RATIO = 0.75


async def aggregate_daily_analytics():
    """Aggregate yesterday's chat messages into AnalyticsDailySummary."""
    from app.models.module import Module
    from app.models.ai_model import AIModel
    from app.models.analytics_daily_summary import AnalyticsDailySummary
    from app.services.dynamodb_service import query_module_messages

    yesterday = date.today() - timedelta(days=1)
    start_of_day = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
    end_of_day = datetime.combine(yesterday, datetime.max.time(), tzinfo=timezone.utc)
    start_ts = int(start_of_day.timestamp() * 1000)
    end_ts = int(end_of_day.timestamp() * 1000)

    db = SessionLocal()
    try:
        # Get all active modules
        modules = db.query(Module).filter(Module.is_active == True).all()
        if not modules:
            logger.info("No active modules found, skipping aggregation")
            return

        # Load AI model costs for cost calculation
        ai_models = {m.model_name: m for m in db.query(AIModel).filter(AIModel.is_active == True).all()}

        aggregated = 0
        for module in modules:
            try:
                messages = await asyncio.to_thread(query_module_messages, module.id, start_ts, end_ts)
                if not messages:
                    continue

                # Aggregate metrics
                student_ids = set()
                conversation_ids = set()
                total_tokens = 0
                response_times = []
                provider_counts = Counter()
                model_counts = Counter()
                total_cost = Decimal('0')

                for msg in messages:
                    student_ids.add(msg.get('studentId', 0))
                    conversation_ids.add(msg.get('conversationId', ''))
                    tokens = msg.get('tokenCount', 0) or 0
                    total_tokens += tokens

                    rt = msg.get('responseTime')
                    if rt:
                        response_times.append(rt)

                    provider = msg.get('provider', 'unknown')
                    model = msg.get('modelUsed', 'unknown')
                    provider_counts[provider] += 1
                    model_counts[model] += 1

                    # Calculate cost from AI model pricing
                    if tokens > 0 and model in ai_models:
                        ai_model = ai_models[model]
                        input_cost = ai_model.input_cost_per_1m or Decimal('0')
                        output_cost = ai_model.output_cost_per_1m or Decimal('0')
                        input_tokens = int(tokens * INPUT_TOKEN_RATIO)
                        output_tokens = int(tokens * OUTPUT_TOKEN_RATIO)
                        total_cost += (Decimal(input_tokens) * input_cost / Decimal('1000000'))
                        total_cost += (Decimal(output_tokens) * output_cost / Decimal('1000000'))

                top_provider = provider_counts.most_common(1)[0][0] if provider_counts else None
                top_model = model_counts.most_common(1)[0][0] if model_counts else None
                avg_rt = int(sum(response_times) / len(response_times)) if response_times else None

                # UPSERT into AnalyticsDailySummary
                summary = AnalyticsDailySummary(
                    module_id=module.id,
                    date=yesterday,
                    question_count=len(messages),
                    unique_students=len(student_ids),
                    unique_conversations=len(conversation_ids),
                    total_tokens=total_tokens,
                    estimated_cost_usd=total_cost,
                    avg_response_time_ms=avg_rt,
                    top_provider=top_provider,
                    top_model=top_model,
                )
                db.merge(summary)
                aggregated += 1

            except Exception as e:
                logger.error(f"Error aggregating module {module.id}: {e}")
                continue

        db.commit()
        logger.info(f"✅ Analytics aggregation complete: {aggregated} modules for {yesterday}")

    except Exception as e:
        logger.error(f"Error in daily analytics aggregation: {e}")
        db.rollback()
    finally:
        db.close()


async def run_analytics_aggregation_worker():
    """Daily loop at 2:30 AM."""
    while True:
        try:
            wait = seconds_until_next_run(AGGREGATION_HOUR, AGGREGATION_MINUTE)
            next_run = datetime.now() + timedelta(seconds=wait)
            logger.info(f"📊 Next analytics aggregation at {next_run.strftime('%Y-%m-%d %H:%M')} ({wait/3600:.1f}h)")
            await asyncio.sleep(wait)

            logger.info("📊 Starting daily analytics aggregation...")
            await aggregate_daily_analytics()

        except asyncio.CancelledError:
            logger.info("🛑 Analytics aggregation worker cancelled")
            return
        except Exception as e:
            logger.error(f"Error in analytics aggregation loop: {e}")
            await asyncio.sleep(300)
