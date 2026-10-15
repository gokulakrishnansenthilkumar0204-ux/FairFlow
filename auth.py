"""Application authentication: bcrypt password hashes + short-lived JWT sessions.

This is *application* login (who may use which API). Beneficiary identity
verification at the shop counter is separate - see biometric.py.
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import call

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY or len(SECRET_KEY) < 32:
    raise RuntimeError("JWT_SECRET_KEY must be set to a random value of at least 32 characters")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

_bearer = HTTPBearer(auto_error=False)
# Compared against when a username does not exist, so response time does not reveal it.
_DUMMY_HASH = bcrypt.hashpw(b"fairflow-dummy-password", bcrypt.gensalt()).decode()

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except ValueError:  # malformed stored hash
        return False


def authenticate(username: str, password: str) -> dict | None:
    user = call("ff_get_user_by_username", username, one=True)
    valid = verify_password(password, user["hashed_password"] if user else _DUMMY_HASH)
    return user if user and valid else None


def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Validate the bearer token and reload the user, so role/shop always come from the DB."""
    if credentials is None:
        raise _UNAUTHORIZED
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        raise _UNAUTHORIZED
    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise _UNAUTHORIZED

    user = call("ff_get_user_by_username", username, one=True)
    if user is None:
        raise _UNAUTHORIZED
    user.pop("hashed_password")
    return user
