import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import modules
from app.core.config import settings
from app.core.swagger_config import custom_openapi
from app.workers.document_extraction_worker import run_extraction_worker, run_failed_extraction_retry_worker
from app.workers.quiz_maintenance_worker import run_daily_maintenance
from app.workers.sqs_worker import run_sqs_workers
from app.workers.analytics_aggregation_worker import run_analytics_aggregation_worker
from app.workers.topic_classification_worker import run_topic_classification_worker
from app.workers.quiz_analytics_worker import run_quiz_analytics_worker
from app.workers.ai_summary_worker import run_ai_summary_worker
from app.workers.ttl_backfill_worker import run_ttl_backfill

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background workers on startup; they run until the process exits."""
    tasks = []

    logger.info("🚀 Starting tutoria-worker background jobs...")

    # SQS consumers — react to on-upload extraction and quiz-gen triggers from .NET API
    tasks.append(asyncio.create_task(run_sqs_workers(), name="sqs-workers"))
    logger.info("  ✅ SQS workers started (extraction + quiz-gen queues)")

    # Every 4h: retry files with processing_status='failed'
    tasks.append(asyncio.create_task(run_failed_extraction_retry_worker(), name="failed-extraction-retry"))
    logger.info("  ✅ Failed-extraction retry worker started (every 4h)")

    # Daily 2 AM sweep: extract text from any files that were never processed
    tasks.append(asyncio.create_task(run_extraction_worker(), name="extraction-worker"))
    logger.info("  ✅ Document extraction worker scheduled (daily @ 2 AM)")

    # Daily 2 AM sweep: regenerate quizzes for modules whose files changed
    tasks.append(asyncio.create_task(run_daily_maintenance(), name="quiz-maintenance-worker"))
    logger.info("  ✅ Quiz maintenance worker scheduled (daily @ 2 AM)")

    # Analytics workers (pre-compute dashboard data nightly)
    tasks.append(asyncio.create_task(run_analytics_aggregation_worker(), name="analytics-aggregation"))
    logger.info("  ✅ Analytics aggregation worker scheduled (daily @ 2:30 AM)")

    tasks.append(asyncio.create_task(run_topic_classification_worker(), name="topic-classification"))
    logger.info("  ✅ Topic classification worker scheduled (daily @ 3:00 AM)")

    tasks.append(asyncio.create_task(run_quiz_analytics_worker(), name="quiz-analytics"))
    logger.info("  ✅ Quiz analytics worker scheduled (daily @ 3:30 AM)")

    tasks.append(asyncio.create_task(run_ai_summary_worker(), name="daily-ai-summary"))
    logger.info("  ✅ Daily AI summary worker scheduled (daily @ 4:00 AM)")

    # One-time TTL backfill (enable once, run, disable)
    if settings.TTL_BACKFILL_ENABLED:
        tasks.append(asyncio.create_task(run_ttl_backfill(), name="ttl-backfill"))
        logger.info("  ✅ TTL backfill job started (one-time)")

    yield  # App runs here

    # Graceful shutdown
    logger.info("🛑 Shutting down background workers...")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("  ✅ Workers stopped")


app = FastAPI(
    lifespan=lifespan,
    title="Tutoria Worker — Document & Quiz Service",
    description="""
## ⚙️ Tutoria Worker Service

Handles **offline heavy processing** so the main chat API stays lean and always-on:

- **Document extraction** — extracts text from PDFs, DOCX, XLSX, images for AI context
- **Quiz generation** — auto-generates quiz questions from course materials using AI
- **Quiz management** — upload, validate, and manage question banks
- **Prompt improvement** — AI-assisted system prompt enhancement for professors

### 🔐 Authentication

All endpoints require either:
- `X-Internal-Api-Key` header — for service-to-service calls from the .NET API
- `Authorization: Bearer <JWT>` header — for professor / super-admin dashboard calls

### 📅 Background Jobs

Two daily workers run automatically at **2 AM** (server time):
1. **Document Extraction Worker** — processes any files without extracted text
2. **Quiz Maintenance Worker** — regenerates AI quizzes for updated modules
""",
    version="1.0.0",
    openapi_tags=[
        {
            "name": "📖 Modules & Quizzes",
            "description": "Quiz generation, document extraction, and prompt management endpoints.",
        },
    ],
)

app.openapi = lambda: custom_openapi(app)  # type: ignore

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(
    modules.router,
    tags=["📖 Modules & Quizzes"],
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Access denied"},
        429: {"description": "Rate limit exceeded"},
    },
)


@app.get("/")
async def root():
    return {"message": "Tutoria Worker Service — Document & Quiz Processing"}


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "tutoria-worker"}
