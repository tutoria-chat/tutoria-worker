"""
Quiz Model - Pre-generated quiz questions for modules

Stores quiz questions generated once during module creation to eliminate
on-demand generation costs and improve performance.
"""
from sqlalchemy import Column, Integer, String, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.models.base import BaseModel
import enum


class QuizDifficulty(str, enum.Enum):
    """Quiz question difficulty levels."""
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class Quiz(BaseModel):
    """
    Pre-generated quiz questions for modules.

    Each module should have ~50 questions generated once when created,
    with detailed explanations for ALL answer options.
    """
    __tablename__ = "Quizzes"

    # Foreign key to module
    module_id = Column("ModuleId", Integer, ForeignKey("Modules.Id", ondelete="CASCADE"), nullable=False, index=True)

    # Question details
    question_number = Column("QuestionNumber", Integer, nullable=False)  # 1-50
    question_text = Column("QuestionText", Text, nullable=False)
    difficulty = Column("Difficulty", String(20), nullable=False, default="medium")

    # Answer options (4-5 options: A, B, C, D, E)
    option_a = Column("OptionA", Text, nullable=False)
    option_b = Column("OptionB", Text, nullable=False)
    option_c = Column("OptionC", Text, nullable=False)
    option_d = Column("OptionD", Text, nullable=False)
    option_e = Column("OptionE", Text, nullable=True)  # Optional 5th option

    # Correct answer (A, B, C, D, or E)
    correct_answer = Column("CorrectAnswer", String(1), nullable=False)

    # Detailed explanations for ALL options
    explanation_a = Column("ExplanationA", Text, nullable=False)
    explanation_b = Column("ExplanationB", Text, nullable=False)
    explanation_c = Column("ExplanationC", Text, nullable=False)
    explanation_d = Column("ExplanationD", Text, nullable=False)
    explanation_e = Column("ExplanationE", Text, nullable=True)  # Optional

    # Source tracking
    source = Column("Source", String(20), nullable=False, default="ai_generated")  # 'ai_generated', 'uploaded', 'manual'
    uploaded_file_id = Column("UploadedFileId", Integer, ForeignKey("Files.Id"), nullable=True)

    # Metadata
    concepts_covered = Column("ConceptsCovered", String(500), nullable=True)  # Comma-separated topics

    # Relationships
    module = relationship("Module", back_populates="quizzes")

    def __repr__(self):
        return f"<Quiz(id={self.id}, module_id={self.module_id}, question_number={self.question_number}, difficulty={self.difficulty})>"

    def to_dict(self):
        """Convert quiz to dictionary for API responses."""
        return {
            "id": self.id,
            "module_id": self.module_id,
            "question_number": self.question_number,
            "question_text": self.question_text,
            "difficulty": self.difficulty,
            "options": {
                "A": self.option_a,
                "B": self.option_b,
                "C": self.option_c,
                "D": self.option_d,
                "E": self.option_e
            },
            "correct_answer": self.correct_answer,
            "explanations": {
                "A": self.explanation_a,
                "B": self.explanation_b,
                "C": self.explanation_c,
                "D": self.explanation_d,
                "E": self.explanation_e
            },
            "concepts_covered": self.concepts_covered.split(",") if self.concepts_covered else [],
            "source": self.source,
            "uploaded_file_id": self.uploaded_file_id
        }
