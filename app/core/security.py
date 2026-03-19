from datetime import datetime, timedelta, timezone
from typing import Union
import secrets
import re
import httpx
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Header
from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def validate_password_strength(password: str) -> tuple[bool, str]:
    """
    Validate password strength against security requirements.

    Requirements:
    - At least 8 characters
    - At least 1 uppercase letter
    - At least 1 lowercase letter
    - At least 1 number
    - Special characters are recommended but not required

    Returns:
        tuple: (is_valid, error_message or success_message)
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"

    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"

    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"

    if not re.search(r'\d', password):
        return False, "Password must contain at least one number"

    # Check for special characters (recommended but not required)
    has_special = bool(re.search(r'[!@#$%^&*(),.?":{}|<>]', password))

    if has_special:
        return True, "Password meets all security requirements"
    else:
        return True, "Password is valid. Consider adding special characters for extra security"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Union[timedelta, None] = None, university_id: Union[int, None] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )
    to_encode.update({"exp": expire})
    if university_id is not None:
        to_encode["university_id"] = university_id
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: Union[timedelta, None] = None, university_id: Union[int, None] = None):
    """Create a refresh token with longer expiration (7 days default)"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(days=7)
    # Store user_type separately and mark as refresh token
    user_type = to_encode.get("type")
    to_encode.update({"exp": expire, "token_type": "refresh", "user_type": user_type})
    if university_id is not None:
        to_encode["university_id"] = university_id
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_password_reset_token() -> str:
    """Generate a secure random token for password reset"""
    return secrets.token_urlsafe(32)


def verify_token(authorization: str = Header(None, alias="Authorization")):
    """
    Verify JWT token by calling .NET API's validation endpoint.
    This eliminates the need to maintain JWT secrets in multiple places.
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Call .NET API to validate token
    try:
        with httpx.Client() as client:
            response = client.get(
                f"{settings.DOTNET_API_URL}/api/auth/validate-token",
                headers={"Authorization": authorization},
                timeout=5.0  # 5 second timeout
            )

            if response.status_code == 401:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Could not validate credentials",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token validation failed",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Return the payload from .NET API
            return response.json()

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Authentication service error: {str(e)}",
        )
