"""Security guards and protection mechanisms"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from app.core.config import settings
from app.models import User


def _get_protected_emails() -> set[str]:
    """Internal: Parse protected emails from environment variable"""
    if not settings.PROTECTED_ACCOUNTS:
        return set()
    return {email.strip().lower() for email in settings.PROTECTED_ACCOUNTS.split(",") if email.strip()}


def is_protected_account(email: str) -> bool:
    """Check if an email belongs to a protected account"""
    if not email:
        return False
    protected = _get_protected_emails()
    return email.lower() in protected


def check_protected_account(target_user: User, current_user: User):
    """
    Verify if a user account is protected from modification/deletion by current user.

    Protection rules:
    - Protected accounts can modify other protected accounts (and themselves)
    - Non-protected accounts CANNOT modify protected accounts

    Raises HTTPException with generic forbidden message if operation not allowed.

    Args:
        target_user: The user being modified/deleted
        current_user: The user performing the operation
    """
    target_is_protected = is_protected_account(target_user.email)
    current_is_protected = is_protected_account(current_user.email)

    # If target is protected and current user is NOT protected, block it
    if target_is_protected and not current_is_protected:
        # Generic error - don't reveal it's protected
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions to perform this operation"
        )
