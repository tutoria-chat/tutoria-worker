"""
CalendarImportJob Model — tracks "import calendar events from a PDF" jobs.

Schema is owned by EF Core migrations in TutoriaApi; this mirrors it so the
worker can read the job, run AI extraction, and write the results back.
"""
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, Text, CheckConstraint
from sqlalchemy.orm import relationship

from .base import BaseModel


class CalendarImportJob(BaseModel):
    __tablename__ = "CalendarImportJobs"

    course_id = Column("CourseId", Integer, ForeignKey("Courses.Id"), nullable=False)
    status = Column("Status", String(50), nullable=False, default="pending")
    extracted_count = Column("ExtractedCount", Integer, default=0, nullable=False)
    confirmed_count = Column("ConfirmedCount", Integer, default=0, nullable=False)
    error_message = Column("ErrorMessage", Text, nullable=True)
    processed_at = Column("ProcessedAt", DateTime(timezone=True), nullable=True)
    confirmed_at = Column("ConfirmedAt", DateTime(timezone=True), nullable=True)
    input_s3_key = Column("InputS3Key", String(500), nullable=True)
    original_filename = Column("OriginalFilename", String(255), nullable=True)
    extracted_events_json = Column("ExtractedEventsJson", Text, nullable=True)
    created_by_user_id = Column("CreatedByUserId", Integer, default=0, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "Status IN ('pending', 'processing', 'completed', 'failed', 'confirmed')",
            name="CK_CalendarImportJobs_Status",
        ),
    )

    course = relationship("Course")

    def __repr__(self):
        return (
            f"<CalendarImportJob(id={self.id}, course_id={self.course_id}, "
            f"status={self.status}, extracted_count={self.extracted_count})>"
        )
