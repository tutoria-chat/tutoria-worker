from sqlalchemy import Column, Integer, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .base import Base


class StudentCourse(Base):
    """Junction table for many-to-many relationship between Students and Courses"""
    __tablename__ = "StudentCourses"

    student_id = Column("StudentId", Integer, ForeignKey("Users.UserId"), primary_key=True)
    course_id = Column("CourseId", Integer, ForeignKey("Courses.Id"), primary_key=True)
    created_at = Column("CreatedAt", DateTime(timezone=True), server_default=func.getutcdate(), nullable=False)

    # Relationships
    student = relationship("User", foreign_keys=[student_id], backref="student_courses")
    course = relationship("Course", foreign_keys=[course_id], backref="student_enrollments")
