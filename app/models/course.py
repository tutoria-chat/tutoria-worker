from sqlalchemy import Column, String, Text, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel
from .professor_course import professor_courses


class Course(BaseModel):
    __tablename__ = "Courses"

    name = Column("Name", String(255), nullable=False)
    code = Column("Code", String(50), nullable=False)
    description = Column("Description", Text)
    university_id = Column(
        "UniversityId", Integer, ForeignKey("Universities.Id"), nullable=False
    )

    university = relationship("University", back_populates="courses")
    modules = relationship(
        "Module", back_populates="course", cascade="all, delete-orphan"
    )
    students = relationship(
        "Student", back_populates="course", cascade="all, delete-orphan"
    )

    # Legacy professor relationship (DISABLED - incompatible with current FK setup)
    # ProfessorCourses.ProfessorId now references Users.UserId, not Professors.Id
    # Since there's no FK between Professors.Id and ProfessorCourses.ProfessorId,
    # this relationship cannot be configured. Use User.courses instead.
    # professors = relationship("Professor", secondary=professor_courses, viewonly=True)
