from pydantic_settings import BaseSettings
from typing import Optional


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

    # S3 Storage (files — PDFs, documents)
    S3_BUCKET_NAME: str = "tutoria-files-dev"
    S3_REGION: str = "us-east-2"

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

    # Encryption key for DB-managed provider keys (must match tutoria-api)
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

    # Application
    debug: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
