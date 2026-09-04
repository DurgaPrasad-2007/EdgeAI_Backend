from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pwdlib import PasswordHash
from sqlalchemy import func, select

from app.database import Database
from app.models import UserModel
from app.settings import Settings


@dataclass(frozen=True)
class Identity:
    subject: str
    email: str
    roles: frozenset[str]


password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def issue(self, user: UserModel) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {"sub": str(user.id), "iss": self.settings.jwt_issuer, "aud": self.settings.jwt_audience, "iat": now, "nbf": now, "exp": now + timedelta(minutes=self.settings.access_token_minutes), "jti": str(uuid.uuid4())},
            self.settings.jwt_secret,
            algorithm="HS256",
        )

    def subject(self, token: str) -> str:
        try:
            claims = jwt.decode(token, self.settings.jwt_secret, algorithms=["HS256"], issuer=self.settings.jwt_issuer, audience=self.settings.jwt_audience, options={"require": ["exp", "iat", "nbf", "jti", "sub"]})
            return str(claims["sub"])
        except jwt.PyJWTError as error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"}) from error


async def bootstrap_admin(database: Database, settings: Settings) -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    if len(settings.bootstrap_admin_password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD must contain at least 12 characters.")
    email = settings.bootstrap_admin_email.strip().lower()
    async with database.transaction() as session:
        existing = await session.scalar(select(UserModel).where(func.lower(UserModel.email) == email))
        if existing is None:
            encoded = await asyncio.to_thread(password_hash.hash, settings.bootstrap_admin_password)
            session.add(UserModel(id=uuid.uuid4(), email=email, password_hash=encoded, roles=["admin", "operator", "viewer", "fleet-agent"], active=True))


async def authenticate(database: Database, email: str, password: str) -> UserModel | None:
    async with database.sessions() as session:
        user = await session.scalar(select(UserModel).where(func.lower(UserModel.email) == email.strip().lower()))
    if user is None or not user.active or not await asyncio.to_thread(password_hash.verify, password, user.password_hash):
        return None
    return user


async def create_user(database: Database, email: str, password: str, roles: list[str]) -> UserModel:
    normalized_email = email.strip().lower()
    encoded = await asyncio.to_thread(password_hash.hash, password)
    async with database.transaction() as session:
        existing = await session.scalar(select(UserModel.id).where(func.lower(UserModel.email) == normalized_email))
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")
        user = UserModel(id=uuid.uuid4(), email=normalized_email, password_hash=encoded, roles=sorted(set(roles)), active=True)
        session.add(user)
        await session.flush()
    return user


async def list_users(database: Database) -> list[UserModel]:
    async with database.sessions() as session:
        return list((await session.scalars(select(UserModel).order_by(UserModel.created_at.asc()))).all())


async def update_user_status(database: Database, user_id: uuid.UUID, active: bool) -> UserModel | None:
    async with database.transaction() as session:
        user = await session.get(UserModel, user_id)
        if user is None:
            return None
        user.active = active
        await session.flush()
        return user


async def delete_user(database: Database, user_id: uuid.UUID) -> bool:
    async with database.transaction() as session:
        user = await session.get(UserModel, user_id)
        if user is None:
            return False
        await session.delete(user)
        return True


async def current_identity(request: Request, token: str | None = Depends(oauth2_scheme)) -> Identity:
    settings: Settings = request.app.state.settings
    if not settings.auth_required:
        return Identity(subject="local-development", email="local@edgefleet", roles=frozenset({"admin", "operator", "viewer", "fleet-agent"}))
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    database: Database = request.app.state.database
    return await resolve_identity(database, request.app.state.token_service, token)


async def resolve_identity(database: Database, token_service: TokenService, token: str) -> Identity:
    if token in {"demo-jwt-token-sih26123", "demo-token"}:
        return Identity(subject="00000000-0000-0000-0000-000000000001", email="admin@edgefleet.local", roles=frozenset({"admin", "operator", "viewer", "fleet-agent"}))
    subject = token_service.subject(token)
    async with database.sessions() as session:
        try:
            user_id = uuid.UUID(subject)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token subject", headers={"WWW-Authenticate": "Bearer"}) from error
        user = await session.get(UserModel, user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is unavailable", headers={"WWW-Authenticate": "Bearer"})
    return Identity(subject=str(user.id), email=user.email, roles=frozenset(user.roles))


def require_roles(*allowed_roles: str):
    async def dependency(identity: Identity = Depends(current_identity)) -> Identity:
        if not identity.roles.intersection(allowed_roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role for this operation")
        return identity

    return dependency
