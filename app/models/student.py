from sqlalchemy import Column, String, Integer, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class Student(BaseModel):
    __tablename__ = "Students"

    username = Column("Username", String(100), nullable=False, unique=True, index=True)
    email = Column("Email", String(255), nullable=False, unique=True, index=True)
    first_name = Column("FirstName", String(100), nullable=False)
    last_name = Column("LastName", String(100), nullable=False)
    hashed_password = Column("HashedPassword", String(255), nullable=False)
    is_active = Column("IsActive", Boolean, default=True, nullable=False, index=True)
    course_id = Column("CourseId", Integer, ForeignKey("Courses.Id"), nullable=False)
    last_login_at = Column("LastLoginAt", DateTime(timezone=True), nullable=True)
    password_reset_token = Column("PasswordResetToken", String(255), nullable=True)
    password_reset_expires = Column(
        "PasswordResetExpires", DateTime(timezone=True), nullable=True
    )

    course = relationship("Course", back_populates="students")
