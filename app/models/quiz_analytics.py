from sqlalchemy import Column, Integer, String, DECIMAL, DateTime, ForeignKey
from .base import BaseModel


class QuizAnalytic(BaseModel):
    """Pre-computed quiz performance stats per concept per module."""
    __tablename__ = "QuizAnalytics"

    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id", ondelete="CASCADE"), nullable=False)
    quiz_id = Column("QuizId", Integer, nullable=True)
    concept_name = Column("ConceptName", String(255), nullable=False)
    total_attempts = Column("TotalAttempts", Integer, default=0, nullable=False)
    correct_count = Column("CorrectCount", Integer, default=0, nullable=False)
    incorrect_count = Column("IncorrectCount", Integer, default=0, nullable=False)
    success_rate = Column("SuccessRate", DECIMAL(5, 2), default=0, nullable=False)
    difficulty = Column("Difficulty", String(20), nullable=True)
    last_updated_at = Column("LastUpdatedAt", DateTime(timezone=True), nullable=True)
