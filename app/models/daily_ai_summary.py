from sqlalchemy import Column, Integer, Date, String, Text, ForeignKey
from .base import BaseModel


class DailyAISummary(BaseModel):
    """AI-written daily briefing per university (shown on the analytics dashboard)."""
    __tablename__ = "DailyAISummaries"

    university_id = Column("UniversityId", Integer, ForeignKey("Universities.Id", ondelete="CASCADE"), nullable=False)
    date = Column("Date", Date, nullable=False)
    summary_text = Column("SummaryText", Text, nullable=False)
    highlights_json = Column("HighlightsJson", Text, nullable=True)  # JSON array of strings
    provider = Column("Provider", String(50), nullable=True)
