"""
Plan Model - Subscription plan definitions

Defines the available subscription tiers for universities, including
feature flags, resource limits, and Stripe pricing integration.
"""
from sqlalchemy import Column, String, Integer, Boolean, Text, DECIMAL
from .base import BaseModel


class Plan(BaseModel):
    __tablename__ = "Plans"

    name = Column("Name", String(100), nullable=False, unique=True)
    slug = Column("Slug", String(50), nullable=False, unique=True)
    description = Column("Description", Text, nullable=True)
    monthly_price_brl = Column("MonthlyPriceBRL", DECIMAL(10, 2), nullable=False)
    stripe_price_id = Column("StripePriceId", String(255), nullable=True)

    # Resource limits
    max_courses = Column("MaxCourses", Integer, nullable=True)  # NULL = unlimited
    max_modules = Column("MaxModules", Integer, nullable=True)  # NULL = unlimited
    max_students = Column("MaxStudents", Integer, nullable=True)  # NULL = unlimited

    # Feature flags
    has_ai_quizzes = Column("HasAIQuizzes", Boolean, default=False, nullable=False)
    has_whatsapp = Column("HasWhatsApp", Boolean, default=False, nullable=False)
    has_priority_support = Column("HasPrioritySupport", Boolean, default=False, nullable=False)
    has_custom_model_config = Column("HasCustomModelConfig", Boolean, default=False, nullable=False)

    # Trial and display
    trial_days = Column("TrialDays", Integer, default=0, nullable=False)
    display_order = Column("DisplayOrder", Integer, default=0, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False)
    is_custom = Column("IsCustom", Boolean, default=False, nullable=False)

    def __repr__(self):
        return f"<Plan(id={self.id}, name={self.name}, slug={self.slug})>"
