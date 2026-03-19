"""
UniversityCourseTypeModel - University-specific AI model overrides by course type

Allows universities to override the system-wide default AI model for specific
course types. Falls back to CourseTypeModel defaults when no override exists.
"""
from sqlalchemy import Column, String, Integer, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class UniversityCourseTypeModel(BaseModel):
    __tablename__ = "UniversityCourseTypeModels"

    university_id = Column("UniversityId", Integer, ForeignKey("Universities.Id"), nullable=False)
    course_type = Column("CourseType", String(100), nullable=False)
    ai_model_id = Column("AIModelId", Integer, ForeignKey("AIModels.Id"), nullable=False)
    priority = Column("Priority", Integer, default=0, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)

    # Relationships
    university = relationship("University")
    ai_model = relationship("AIModel")

    def __repr__(self):
        return (
            f"<UniversityCourseTypeModel(id={self.id}, university_id={self.university_id}, "
            f"course_type={self.course_type}, ai_model_id={self.ai_model_id})>"
        )
