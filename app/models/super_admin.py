from sqlalchemy import Column, String, Integer, Boolean, DateTime, func
from .base import Base


class SuperAdmin(Base):
    __tablename__ = "SuperAdmins"

    super_admin_id = Column(
        "SuperAdminId", Integer, primary_key=True, index=True, autoincrement=True
    )
    username = Column("Username", String(100), nullable=False, unique=True, index=True)
    email = Column("Email", String(255), nullable=False, unique=True, index=True)
    first_name = Column("FirstName", String(100), nullable=False)
    last_name = Column("LastName", String(100), nullable=False)
    hashed_password = Column("HashedPassword", String(255), nullable=False)
    created_at = Column(
        "CreatedAt", DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        "UpdatedAt",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    is_active = Column("IsActive", Boolean, default=True, nullable=False, index=True)
    last_login_at = Column("LastLoginAt", DateTime(timezone=True), nullable=True)
    password_reset_token = Column("PasswordResetToken", String(255), nullable=True)
    password_reset_expires = Column(
        "PasswordResetExpires", DateTime(timezone=True), nullable=True
    )
    theme_preference = Column("ThemePreference", String(20), default="system", nullable=True)
    language_preference = Column("LanguagePreference", String(10), default="pt-br", nullable=True)
