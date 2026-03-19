from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class User(BaseModel):
    """Unified user model that consolidates Professors, SuperAdmins, and Students"""
    __tablename__ = "Users"

    # Override BaseModel's id with user_id (Users table uses UserId, not Id)
    id = None  # Exclude BaseModel's id column
    user_id = Column("UserId", Integer, primary_key=True, index=True, autoincrement=True)
    username = Column("Username", String(100), nullable=False, unique=True, index=True)
    email = Column("Email", String(255), nullable=False, unique=True, index=True)
    first_name = Column("FirstName", String(100), nullable=False)
    last_name = Column("LastName", String(100), nullable=False)
    hashed_password = Column("HashedPassword", String(255), nullable=False)
    user_type = Column("UserType", String(20), nullable=False, index=True)  # 'professor', 'super_admin', 'student'
    is_active = Column("IsActive", Boolean, default=True, nullable=False, index=True)

    # Professor-specific fields (nullable for non-professors)
    university_id = Column("UniversityId", Integer, ForeignKey("Universities.Id"), nullable=True)
    is_admin = Column("IsAdmin", Boolean, default=False, nullable=True)

    # Additional profile fields
    government_id = Column("GovernmentId", String(50), nullable=True)  # CPF (Brazil), SSN (US), etc.
    external_id = Column("ExternalId", String(100), nullable=True)  # Student registration ID, employee ID, etc.
    birthdate = Column("Birthdate", Date, nullable=True)

    # Common fields
    last_login_at = Column("LastLoginAt", DateTime(timezone=True), nullable=True)
    password_reset_token = Column("PasswordResetToken", String(255), nullable=True)
    password_reset_expires = Column("PasswordResetExpires", DateTime(timezone=True), nullable=True)
    theme_preference = Column("ThemePreference", String(20), default="system", nullable=True)
    language_preference = Column("LanguagePreference", String(10), default="pt-br", nullable=True)

    # Relationships
    university = relationship("University", foreign_keys=[university_id], backref="users")

    # Professor courses relationship (only relevant for user_type='professor')
    # Using explicit join conditions because ProfessorCourses.ProfessorId references Users.UserId
    # Note: We don't specify foreign_keys here to avoid circular import issues
    courses = relationship(
        "Course",
        secondary="ProfessorCourses",
        backref="assigned_professors"
    )
