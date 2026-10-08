from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .database import connection


password_hasher = PasswordHasher()
bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return password_hasher.verify(stored_hash, password)
    except VerifyMismatchError:
        return False


def create_access_token(user_id: str, role: str = "user") -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.token_expire_minutes)
    return jwt.encode(
        {"sub": user_id, "role": role, "exp": expires, "jti": str(uuid4())},
        settings.secret_key,
        algorithm="HS256",
    )


def _token_payload(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Sign in to continue."})
    try:
        return jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail={"code": "TOKEN_EXPIRED", "message": "Your session expired. Sign in again."}) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "The session token is invalid."}) from exc


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    payload = _token_payload(credentials)
    if payload.get("role", "user") != "user":
        raise HTTPException(status_code=403, detail={"code": "ADMIN_ONLY", "message": "This account cannot access user resources."})
    user_id = payload.get("sub")
    with connection() as db:
        row = db.execute(
            "SELECT id, email, full_name, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "USER_NOT_FOUND", "message": "This account is no longer available."})
    return dict(row)


def current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    payload = _token_payload(credentials)
    if (
        payload.get("role") != "admin"
        or not settings.admin_username
        or not settings.admin_password
        or payload.get("sub") != settings.admin_username
    ):
        raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED", "message": "Administrator access is required."})
    return {"username": settings.admin_username, "role": "admin"}
