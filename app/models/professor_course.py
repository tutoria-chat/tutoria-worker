from sqlalchemy import Column, Integer, ForeignKey, DateTime, func, Table
from sqlalchemy.orm import relationship
from .base import Base

# Association table for many-to-many relationship between professors and courses
# Note: ProfessorId references Users.UserId (not Professors.Id) since we use unified Users table
professor_courses = Table(
    "ProfessorCourses",
    Base.metadata,
    Column("ProfessorId", Integer, ForeignKey("Users.UserId"), primary_key=True),
    Column("CourseId", Integer, ForeignKey("Courses.Id"), primary_key=True),
    Column(
        "CreatedAt", DateTime(timezone=True), server_default=func.now(), nullable=False
    ),
)
