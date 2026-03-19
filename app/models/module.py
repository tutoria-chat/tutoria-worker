from sqlalchemy import Column, String, Text, Integer, ForeignKey, CheckConstraint, DateTime, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class Module(BaseModel):
    __tablename__ = "Modules"

    name = Column("Name", String(255), nullable=False)
    code = Column("Code", String(50), nullable=False)
    description = Column("Description", Text)
    system_prompt = Column("SystemPrompt", Text, nullable=False)
    semester = Column("Semester", Integer, nullable=False)
    year = Column("Year", Integer, nullable=False)
    course_id = Column("CourseId", Integer, ForeignKey("Courses.Id"), nullable=False)
    openai_assistant_id = Column("OpenAIAssistantId", String(255), nullable=True)  # Cached Assistant ID
    openai_vector_store_id = Column("OpenAIVectorStoreId", String(255), nullable=True)  # Cached Vector Store ID
    last_prompt_improved_at = Column("LastPromptImprovedAt", DateTime(timezone=True), nullable=True)
    prompt_improvement_count = Column("PromptImprovementCount", Integer, default=0, nullable=False)
    tutor_language = Column("TutorLanguage", String(10), default="pt-br", nullable=False)  # Language for AI tutor responses
    ai_model_id = Column("AIModelId", Integer, ForeignKey("AIModels.Id"), nullable=True)
    course_type = Column("CourseType", String(50), nullable=True)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)

    __table_args__ = (
        CheckConstraint("Semester BETWEEN 1 AND 8", name="CK_Modules_Semester"),
        CheckConstraint("Year BETWEEN 2020 AND 2050", name="CK_Modules_Year"),
    )

    course = relationship("Course", back_populates="modules")
    files = relationship("File", back_populates="module", cascade="all, delete-orphan")
    quizzes = relationship("Quiz", back_populates="module", cascade="all, delete-orphan")
    ai_model = relationship("AIModel", back_populates="modules")
