"""Application authentication: bcrypt password hashes + short-lived JWT sessions.

Sessions are JWTs issued after fingerprint (passkey) sign-in - see passkeys.py.
Password helpers remain for the optional ALLOW_PASSWORD_LOGIN mode and tests.
Beneficiary identity verification at the shop counter is separate - see biometric.py.
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





def authenticate(username: str, password: str) -> dict | None:
    user = call("ff_get_user_by_username", username, one=True)
    stored = user["hashed_password"] if user and user["hashed_password"] else _DUMMY_HASH
    valid = verify_password(password, stored)
    return user if user and user["hashed_password"] and user["is_active"] and valid else None


def create_access_token(user: dict) -> str:
    """Session token. `tv` (token_version) lets the server end all of a user's sessions
    at once - deactivation, role change or revoked devices bump it in the database."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    claims = {"sub": user["username"], "tv": int(user.get("token_version") or 0), "exp": expire}
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


def public_user(user: dict) -> dict:
    """The user fields clients may see (no hash, no token version)."""
    return {k: user[k] for k in ("id", "username", "role", "shop_id", "is_active", "approved") if k in user}


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
    if user is None or not user["is_active"]:
        raise _UNAUTHORIZED
    # Sessions issued before a deactivation / role change / device revocation are void.
    if payload.get("tv", 0) != user["token_version"]:
        raise _UNAUTHORIZED
    return public_user(user)
