from sqlalchemy import Column, String, Text, Integer, BigInteger, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class Assignment(BaseModel):
    __tablename__ = "Assignments"

    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id"), nullable=False)
    title = Column("Title", String(255), nullable=False)
    description = Column("Description", Text)
    due_date = Column("DueDate", DateTime(timezone=True), nullable=False)
    is_published = Column("IsPublished", Boolean, default=False, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)

    # Comma-separated keywords the AI should focus on when giving feedback
    keywords = Column("Keywords", String(1000), nullable=True)

    # Professor-defined grading criteria / rubric description
    grading_criteria = Column("GradingCriteria", Text, nullable=True)

    # Assignment task file — NOT the File entity, avoids triggering RAG/quiz pipelines
    s3_key = Column("S3Key", String(500), nullable=False)
    original_file_name = Column("OriginalFileName", String(255), nullable=False)
    file_size_bytes = Column("FileSizeBytes", BigInteger, nullable=False)
    content_type = Column("ContentType", String(100), nullable=False)

    # Cached text extracted from the assignment document
    extracted_text = Column("ExtractedText", Text, nullable=True)

    # Optional rubric / evaluation criteria file
    rubric_s3_key = Column("RubricS3Key", String(500), nullable=True)
    rubric_original_file_name = Column("RubricOriginalFileName", String(255), nullable=True)
    rubric_file_size_bytes = Column("RubricFileSizeBytes", BigInteger, nullable=True)
    rubric_content_type = Column("RubricContentType", String(100), nullable=True)

    created_by_user_id = Column("CreatedByUserId", Integer, ForeignKey("Users.UserId"), nullable=False)

    submissions = relationship("AssignmentSubmission", back_populates="assignment")
