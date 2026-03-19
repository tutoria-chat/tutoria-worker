from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel
from .professor_course import professor_courses


class Professor(BaseModel):
    __tablename__ = "Professors"

    username = Column("Username", String(100), nullable=False, unique=True, index=True)
    email = Column("Email", String(255), nullable=False, unique=True, index=True)
    first_name = Column("FirstName", String(100), nullable=False)
    last_name = Column("LastName", String(100), nullable=False)
    hashed_password = Column("HashedPassword", String(255), nullable=False)
    is_admin = Column("IsAdmin", Boolean, default=False, nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False, index=True)
    university_id = Column(
        "UniversityId", Integer, ForeignKey("Universities.Id"), nullable=False
    )
    last_login_at = Column("LastLoginAt", DateTime(timezone=True), nullable=True)
    password_reset_token = Column("PasswordResetToken", String(255), nullable=True)
    password_reset_expires = Column(
        "PasswordResetExpires", DateTime(timezone=True), nullable=True
    )
    theme_preference = Column("ThemePreference", String(20), default="system", nullable=True)
    language_preference = Column("LanguagePreference", String(10), default="pt-br", nullable=True)

    university = relationship("University", back_populates="professors")

    # Legacy courses relationship (DISABLED - incompatible with current FK setup)
    # ProfessorCourses.ProfessorId now references Users.UserId, not Professors.Id
    # Since there's no FK between Professors.Id and ProfessorCourses.ProfessorId,
    # this relationship cannot be configured. Use User.courses instead.
    # courses = relationship("Course", secondary=professor_courses, viewonly=True)
