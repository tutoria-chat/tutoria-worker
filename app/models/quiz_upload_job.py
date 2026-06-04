"""
QuizUploadJob Model - Tracking bulk quiz upload processing

Tracks the status of quiz import jobs from uploaded files (CSV, Excel),
including extraction progress, error tracking, and completion timestamps.
"""
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, CheckConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class QuizUploadJob(BaseModel):
    __tablename__ = "QuizUploadJobs"

    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id"), nullable=False)
    file_id = Column("FileId", Integer, ForeignKey("Files.Id"), nullable=True)
    status = Column("Status", String(50), nullable=False, default="pending")
    extracted_count = Column("ExtractedCount", Integer, default=0, nullable=False)
    error_message = Column("ErrorMessage", Text, nullable=True)
    processed_at = Column("ProcessedAt", DateTime(timezone=True), nullable=True)
    input_s3_key = Column("InputS3Key", String(500), nullable=True)
    original_filename = Column("OriginalFilename", String(255), nullable=True)
    extracted_questions_json = Column("ExtractedQuestionsJson", Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "Status IN ('pending', 'processing', 'completed', 'failed')",
            name="CK_QuizUploadJobs_Status",
        ),
    )

    # Relationships
    module = relationship("Module")

    def __repr__(self):
        return (
            f"<QuizUploadJob(id={self.id}, module_id={self.module_id}, "
            f"status={self.status}, extracted_count={self.extracted_count})>"
        )
