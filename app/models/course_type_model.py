"""
CourseTypeModel - Default AI model assignments by course type

Maps course types (e.g., 'programming', 'mathematics') to default AI models
with priority ordering. Used as the system-wide default when no university
override exists.
"""
from sqlalchemy import Column, String, Integer, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class CourseTypeModel(BaseModel):
    __tablename__ = "CourseTypeModels"

    course_type = Column("CourseType", String(100), nullable=False)
    ai_model_id = Column("AIModelId", Integer, ForeignKey("AIModels.Id"), nullable=False)
    priority = Column("Priority", Integer, default=0, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)

    # Relationships
    ai_model = relationship("AIModel")

    def __repr__(self):
        return f"<CourseTypeModel(id={self.id}, course_type={self.course_type}, ai_model_id={self.ai_model_id})>"
