"""
GradingJob Model - Tracks AI batch grading of student answers.

Mirrors TutoriaApi GradingJob entity exactly.
"""
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, CheckConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class GradingJob(BaseModel):
    __tablename__ = "GradingJobs"

    course_id = Column("CourseId", Integer, ForeignKey("Courses.Id"), nullable=False)
    created_by_user_id = Column("CreatedByUserId", Integer, nullable=False)
    status = Column("Status", String(50), nullable=False, default="pending")
    input_s3_key = Column("InputS3Key", String(500), nullable=True)
    result_s3_key = Column("ResultS3Key", String(500), nullable=True)
    total_submissions = Column("TotalSubmissions", Integer, default=0, nullable=False)
    processed_submissions = Column("ProcessedSubmissions", Integer, default=0, nullable=False)
    error_message = Column("ErrorMessage", Text, nullable=True)
    processed_at = Column("ProcessedAt", DateTime(timezone=True), nullable=True)
    grading_criteria = Column("GradingCriteria", Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "\"Status\" IN ('pending', 'processing', 'completed', 'failed')",
            name="CK_GradingJobs_Status",
        ),
    )

    # Relationships
    course = relationship("Course")

    def __repr__(self):
        return (
            f"<GradingJob(id={self.id}, course_id={self.course_id}, "
            f"status={self.status}, processed={self.processed_submissions}/{self.total_submissions})>"
        )
