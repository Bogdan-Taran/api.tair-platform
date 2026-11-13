import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select

from app.config import settings
from app.db import db_session
from app.models import RefreshToken, User

hasher = PasswordHash.recommended()
bearer = HTTPBearer()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def current_user(
    auth: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    db: Annotated[object, Depends(db_session, scope="function")],
):
    try:
        claims = jwt.decode(
            auth.credentials,
            settings().jwt_secret,
            algorithms=["HS256"],
            audience="tair-api",
            issuer="tair-api",
            options={"require": ["exp", "sub", "iat", "type"]},
        )
        if claims["type"] != "access":
            raise ValueError()
        user = db.get(User, claims["sub"])
        if not user or not user.active:
            raise ValueError()
        return user
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})


def issue_tokens(db, user):
    cfg = settings()
    now = datetime.now(UTC)
    access = jwt.encode(
        {
            "sub": user.id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=cfg.access_minutes),
            "aud": "tair-api",
            "iss": "tair-api",
        },
        cfg.jwt_secret,
        algorithm="HS256",
    )
    refresh = secrets.token_urlsafe(48)
    db.add(
        RefreshToken(
            user_id=user.id, digest=digest(refresh), expires_at=now + timedelta(days=cfg.refresh_days)
        )
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": cfg.access_minutes * 60,
    }


def refresh_record(db, token):
    record = db.scalar(select(RefreshToken).where(RefreshToken.digest == digest(token)).with_for_update())
    if not record or record.revoked or record.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
        raise HTTPException(401, "Invalid or expired refresh token")
    return record
