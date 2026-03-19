import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import modules
from app.core.config import settings
from app.core.swagger_config import custom_openapi
from app.workers.document_extraction_worker import run_extraction_worker
from app.workers.quiz_maintenance_worker import run_daily_maintenance
from app.workers.sqs_worker import run_sqs_workers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background workers on startup; they run until the process exits."""
    tasks = []

    logger.info("🚀 Starting tutoria-worker background jobs...")

    # SQS consumers — react to on-upload extraction and quiz-gen triggers from .NET API
    tasks.append(asyncio.create_task(run_sqs_workers(), name="sqs-workers"))
    logger.info("  ✅ SQS workers started (extraction + quiz-gen queues)")

    # Daily 2 AM sweep: extract text from any files that missed on-upload extraction
    tasks.append(asyncio.create_task(run_extraction_worker(), name="extraction-worker"))
    logger.info("  ✅ Document extraction worker scheduled (daily @ 2 AM)")

    # Daily 2 AM sweep: regenerate quizzes for modules whose files changed
    tasks.append(asyncio.create_task(run_daily_maintenance(), name="quiz-maintenance-worker"))
    logger.info("  ✅ Quiz maintenance worker scheduled (daily @ 2 AM)")

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
