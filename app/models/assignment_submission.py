from sqlalchemy import Column, String, Text, Integer, BigInteger, DateTime, Numeric, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class AssignmentSubmission(BaseModel):
    __tablename__ = "AssignmentSubmissions"

    assignment_id = Column("AssignmentId", Integer, ForeignKey("Assignments.Id"), nullable=False)

    # User.UserId of the verified student (nullable only for legacy pre-verification rows)
    student_id = Column("StudentId", Integer, nullable=True)

    s3_key = Column("S3Key", String(500), nullable=False)
    original_file_name = Column("OriginalFileName", String(255), nullable=False)
    file_size_bytes = Column("FileSizeBytes", BigInteger, nullable=False)
    content_type = Column("ContentType", String(100), nullable=False)

    submitted_at = Column("SubmittedAt", DateTime(timezone=True), nullable=False)
    status = Column("Status", String(50), default="submitted", nullable=False)

    # AI feedback generated asynchronously ("processing" -> "completed"/"failed")
    feedback_text = Column("FeedbackText", Text, nullable=True)
    feedback_generated_at = Column("FeedbackGeneratedAt", DateTime(timezone=True), nullable=True)

    # Nullable until grading is built
    grade = Column("Grade", Numeric(5, 2), nullable=True)
    grading_notes = Column("GradingNotes", Text, nullable=True)
    graded_at = Column("GradedAt", DateTime(timezone=True), nullable=True)
    graded_by_user_id = Column("GradedByUserId", Integer, ForeignKey("Users.UserId"), nullable=True)

    assignment = relationship("Assignment", back_populates="submissions")
