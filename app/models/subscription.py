"""
Subscription Model - University plan subscriptions

Tracks active subscriptions linking universities to plans, with Stripe
integration for billing and lifecycle management (trials, cancellations).
"""
from sqlalchemy import Column, String, Integer, ForeignKey, DateTime, CheckConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class Subscription(BaseModel):
    __tablename__ = "Subscriptions"

    university_id = Column("UniversityId", Integer, ForeignKey("Universities.Id"), nullable=False)
    plan_id = Column("PlanId", Integer, ForeignKey("Plans.Id"), nullable=False)
    status = Column("Status", String(50), nullable=False, default="active")
    stripe_subscription_id = Column("StripeSubscriptionId", String(255), nullable=True)
    stripe_customer_id = Column("StripeCustomerId", String(255), nullable=True)
    current_period_start = Column("CurrentPeriodStart", DateTime(timezone=True), nullable=True)
    current_period_end = Column("CurrentPeriodEnd", DateTime(timezone=True), nullable=True)
    trial_ends_at = Column("TrialEndsAt", DateTime(timezone=True), nullable=True)
    canceled_at = Column("CanceledAt", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "Status IN ('active', 'trialing', 'past_due', 'canceled', 'incomplete', 'expired')",
            name="CK_Subscriptions_Status",
        ),
    )

    # Relationships
    university = relationship("University")
    plan = relationship("Plan")

    def __repr__(self):
        return (
            f"<Subscription(id={self.id}, university_id={self.university_id}, "
            f"plan_id={self.plan_id}, status={self.status})>"
        )
