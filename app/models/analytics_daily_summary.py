from sqlalchemy import Column, Integer, BigInteger, Date, String, DECIMAL, DateTime, ForeignKey, func
from .base import Base


class AnalyticsDailySummary(Base):
    """Pre-computed daily analytics per module. Composite PK (ModuleId, Date)."""
    __tablename__ = "AnalyticsDailySummaries"

    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id", ondelete="CASCADE"), primary_key=True)
    date = Column("Date", Date, primary_key=True)
    question_count = Column("QuestionCount", Integer, default=0, nullable=False)
    unique_students = Column("UniqueStudents", Integer, default=0, nullable=False)
    unique_conversations = Column("UniqueConversations", Integer, default=0, nullable=False)
    total_tokens = Column("TotalTokens", BigInteger, default=0, nullable=False)
    estimated_cost_usd = Column("EstimatedCostUsd", DECIMAL(10, 4), default=0, nullable=False)
    avg_response_time_ms = Column("AvgResponseTimeMs", Integer, nullable=True)
    top_provider = Column("TopProvider", String(50), nullable=True)
    top_model = Column("TopModel", String(100), nullable=True)
    created_at = Column("CreatedAt", DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column("UpdatedAt", DateTime(timezone=True), onupdate=func.now())
