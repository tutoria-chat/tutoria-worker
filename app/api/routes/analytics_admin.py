"""
On-demand analytics recompute endpoints (service-to-service / staff).

The nightly jobs rebuild SQL aggregates from DynamoDB, but staff sometimes need
fresh numbers immediately (e.g. right after students take quizzes). These let the
dashboard trigger a rebuild without waiting for the scheduled run.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.routes.modules import _require_professor_or_internal_key
from app.workers.quiz_analytics_worker import (
    QUIZ_ANALYTICS_WINDOW_DAYS,
    aggregate_quiz_analytics,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/analytics", tags=["analytics"])


@router.post("/recompute-quiz")
async def recompute_quiz_analytics(
    window_days: int = Query(QUIZ_ANALYTICS_WINDOW_DAYS, ge=1, le=365),
    current_user=Depends(_require_professor_or_internal_key),
):
    """Rebuild QuizAnalytics now from the last `window_days` of attempts."""
    try:
        modules = await aggregate_quiz_analytics(window_days=window_days)
        return {"status": "ok", "modules_aggregated": modules, "window_days": window_days}
    except Exception as e:
        logger.error(f"Manual quiz analytics recompute failed: {e}")
        raise HTTPException(status_code=500, detail="Quiz analytics recompute failed")
