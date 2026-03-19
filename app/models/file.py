from sqlalchemy import Column, String, Text, Integer, ForeignKey, BigInteger, Boolean, DateTime, Numeric
from sqlalchemy.orm import relationship
from .base import BaseModel


class File(BaseModel):
    __tablename__ = "Files"

    name = Column("Name", String(255), nullable=False)
    file_type = Column(
        "FileType", String(50), nullable=False
    )  # 'pdf', 'txt', 'docx', 'youtube_transcript', etc.
    file_name = Column("FileName", String(255), nullable=True)  # Original filename
    blob_url = Column("BlobUrl", String(1000), nullable=True)  # Azure Blob Storage URL
    blob_container = Column(
        "BlobContainer", String(100), nullable=True
    )  # Container name
    blob_path = Column("BlobPath", String(500), nullable=True)  # Path within container
    file_size = Column("FileSize", BigInteger, nullable=True)  # File size in bytes
    content_type = Column("ContentType", String(100), nullable=True)  # MIME type
    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id"), nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)
    openai_file_id = Column("OpenAIFileId", String(255), nullable=True)  # Cached OpenAI file_id
    anthropic_file_id = Column("AnthropicFileId", String(255), nullable=True)  # Cached Anthropic file_id

    # Video/Transcription Support
    source_type = Column("SourceType", String(50), nullable=True)  # 'upload', 'youtube', 'url'
    source_url = Column("SourceUrl", String(1000), nullable=True)  # YouTube URL or external link
    transcription_status = Column("TranscriptionStatus", String(50), nullable=True)  # 'pending', 'processing', 'completed', 'failed'
    transcript_text = Column("TranscriptText", Text, nullable=True)  # Full transcript text
    transcript_language = Column("TranscriptLanguage", String(10), nullable=True)  # 'pt-br', 'en', 'es'
    transcript_job_id = Column("TranscriptJobId", String(255), nullable=True)  # Background job ID for tracking
    video_duration_seconds = Column("VideoDurationSeconds", Integer, nullable=True)  # Video/audio duration
    transcripted_at = Column("TranscriptedAt", DateTime, nullable=True)  # Transcription completion timestamp
    transcript_word_count = Column("TranscriptWordCount", Integer, nullable=True)  # Word count for analytics
    transcription_cost_usd = Column("TranscriptionCostUSD", Numeric(10, 4), nullable=True)  # Cost in USD (e.g., AssemblyAI $0.25/hour)

    # File processing status (shown to user as "Preparing" / "Ready")
    processing_status = Column("ProcessingStatus", String(50), nullable=True, default="pending")

    # Document Text Extraction (internal - for RAG performance optimization)
    extracted_text = Column("ExtractedText", Text, nullable=True)
    extracted_at = Column("ExtractedAt", DateTime, nullable=True)
    extracted_word_count = Column("ExtractedWordCount", Integer, nullable=True)
    summarized_text = Column("SummarizedText", Text, nullable=True)  # AI-generated summary for large files

    module = relationship("Module", back_populates="files")
