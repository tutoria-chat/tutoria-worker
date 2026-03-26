from sqlalchemy import Column, Integer, Date, String, Text, ForeignKey
from .base import BaseModel


class TopicClassification(BaseModel):
    """AI-classified question topics per module per day."""
    __tablename__ = "TopicClassifications"

    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id", ondelete="CASCADE"), nullable=False)
    date = Column("Date", Date, nullable=False)
    topic_name = Column("TopicName", String(255), nullable=False)
    question_count = Column("QuestionCount", Integer, default=0, nullable=False)
    sample_questions = Column("SampleQuestions", Text, nullable=True)  # JSON array
