from sqlalchemy import Column, String, Text, Integer, Boolean, ForeignKey, CheckConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel


class University(BaseModel):
    __tablename__ = "Universities"

    name = Column("Name", String(255), nullable=False, unique=True)
    code = Column("Code", String(50), nullable=False, unique=True)  # Fantasy Name (Nome Fantasia) - e.g., USP, BYU
    description = Column("Description", Text)
    address = Column("Address", String(500))
    tax_id = Column("TaxId", String(20))  # CNPJ in Brazil, Tax ID in other countries
    contact_email = Column("ContactEmail", String(255))
    contact_phone = Column("ContactPhone", String(50))
    contact_person = Column("ContactPerson", String(200))
    website = Column("Website", String(255))
    subscription_tier = Column("SubscriptionTier", Integer, default=3, nullable=False)
    stripe_customer_id = Column("StripeCustomerId", String(255), nullable=True)
    is_enterprise = Column("IsEnterprise", Boolean, default=False)
    created_by_user_id = Column("CreatedByUserId", Integer, ForeignKey("Users.UserId"), nullable=True)
    max_courses = Column("MaxCourses", Integer, nullable=True)
    max_modules = Column("MaxModules", Integer, nullable=True)
    max_students = Column("MaxStudents", Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("SubscriptionTier BETWEEN 1 AND 3", name="CK_Universities_SubscriptionTier"),
    )

    courses = relationship(
        "Course", back_populates="university", cascade="all, delete-orphan"
    )
    professors = relationship(
        "Professor", back_populates="university", cascade="all, delete-orphan"
    )
