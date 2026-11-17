from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Optional

from app.core.config_source import load_config_source

# AWS SSM (AWS_SSM_PREFIX, e.g. /tutoria/dev/worker/) or Azure Key Vault
# (AZURE_KEYVAULT_URL), else the plain environment (Railway, local).
load_config_source()


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "sqlite:///./test.db"

    # Security
    SECRET_KEY: str = "your-super-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    INTERNAL_API_KEY: str = "change-me-in-production"
    PROTECTED_ACCOUNTS: str = ""
    DOTNET_API_URL: str = "http://localhost:6969"

    # AWS
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION: str = "us-east-2"

    # S3 Storage (files — PDFs, documents). S3_ENDPOINT_URL (+ S3_ACCESS_KEY_ID /
    # S3_SECRET_ACCESS_KEY) points at an S3-compatible store (Cloudflare R2 on
    # Azure); unset = AWS S3 with the AWS credentials, as before.
    S3_BUCKET_NAME: str = "tutoria-files-dev"
    S3_REGION: str = "us-east-2"
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY_ID: Optional[str] = None
    S3_SECRET_ACCESS_KEY: Optional[str] = None

    # Chat history + quiz answers: "dynamodb" (as before) or "postgres"
    # ("ChatHistory" / "QuizAttemptAnswers"; tutoria-api docs/MULTICLOUD.md).
    CHAT_STORE: str = "dynamodb"
    # Where jobs come from: "sqs" (the queues below) or "postgres" ("WorkerJobs",
    # claimed with SKIP LOCKED by app/workers/job_runner.py).
    JOB_QUEUE: str = "sqs"
    # Postgres runner: jobs at once, delivery attempts before "failed" (the DLQ's
    # maxReceiveCount), and how long a claim holds before another runner may take it
    # (renewed every minute while the job runs).
    JOB_CONCURRENCY: int = 2
    JOB_MAX_ATTEMPTS: int = 3
    JOB_LEASE_SECONDS: int = 300
    JOB_POLL_SECONDS: float = 5.0

    # AI Provider API Keys
    OPENAI_API_KEY: str = "sk-proj-example"
    GEMINI_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None      # fallback for quiz generation
    ASSEMBLYAI_API_KEY: Optional[str] = None

    # AI Provider Base URLs
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    GEMINI_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # AI Provider Defaults (used by document extraction + quiz generation)
    DEFAULT_AI_PROVIDER: str = "openai"
    DEFAULT_AI_MODEL: str = "gpt-4.1-nano"
    FILE_PROCESSING_PROVIDER: str = "gemini"
    FILE_PROCESSING_FALLBACK_CHAIN: str = "openai"

    # DB-managed provider keys are always used when a DB session is available
    # (env-var keys remain the fallback). See gemini/deepseek/xai services.
    ENCRYPTION_KEY: Optional[str] = None

    # Retry & Reliability
    ENABLE_RETRY_LOGIC: bool = True
    MAX_RETRIES: int = 3
    RETRY_BASE_DELAY: float = 1.0
    RETRY_MAX_DELAY: float = 10.0
    API_TIMEOUT_SECONDS: int = 60      # longer for extraction jobs
    ENABLE_PROVIDER_FALLBACK: bool = True
    ENABLE_CIRCUIT_BREAKER: bool = True

    # Token & Cost
    TOKEN_COUNTING_ENABLED: bool = True
    CHEAP_MODEL_THRESHOLD: int = 50
    PREGENERATED_QUIZZES_ENABLED: bool = True

    # PDF / Document Processing
    USE_LOCAL_PDF_EXTRACTION: bool = True
    PDF_CHUNK_SIZE: int = 4000
    PDF_CHUNK_OVERLAP: int = 200
    PDF_MAX_CHUNKS: int = 20
    PDF_MAX_CONTEXT_CHARS: int = 50000
    DOCUMENT_SUMMARY_THRESHOLD: int = 25000

    # SQS Queues (set to empty string to disable; worker skips unconfigured queues)
    SQS_EXTRACTION_QUEUE_URL: str = ""       # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-extraction-dev
    SQS_QUIZ_GEN_QUEUE_URL: str = ""         # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-quiz-gen-dev
    SQS_TRANSCRIPTION_QUEUE_URL: str = ""    # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-transcription-dev
    SQS_GRADING_QUEUE_URL: str = ""          # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-grading-dev
    SQS_QUIZ_UPLOAD_QUEUE_URL: str = ""      # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-quiz-upload-dev
    SQS_FEEDBACK_QUEUE_URL: str = ""         # e.g. https://sqs.us-east-2.amazonaws.com/123/tutoria-feedback-dev

    # DynamoDB (analytics)
    AWS_DYNAMODB_CHAT_TABLE: str = "ChatMessages"
    AWS_DYNAMODB_QUIZ_ATTEMPTS_TABLE: str = "QuizAttempts"

    # Analytics Workers
    TOPIC_CLASSIFICATION_PROVIDER: str = "openai"
    TOPIC_CLASSIFICATION_FALLBACK_CHAIN: str = "gemini,deepseek"
    TTL_BACKFILL_ENABLED: bool = False

    # TutorIA Estudantes (B2C app): Expo push when a student's upload is processed
    STUDENT_APP_PUSH_ENABLED: bool = True
    STUDENT_APP_EXPO_ACCESS_TOKEN: Optional[str] = None

    # Application
    debug: bool = False
    ENVIRONMENT: str = "local"

    @field_validator("CHAT_STORE", "JOB_QUEUE")
    @classmethod
    def _store_choice(cls, value: str, info):
        allowed = {"CHAT_STORE": {"dynamodb", "postgres"}, "JOB_QUEUE": {"sqs", "postgres"}}[info.field_name]
        value = (value or "").strip().lower()
        if value not in allowed:
            raise ValueError(f"{info.field_name} must be one of {sorted(allowed)}, is {value!r}")
        return value

    class Config:
        env_file = ".env"


settings = Settings()
