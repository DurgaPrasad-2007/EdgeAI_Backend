from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

_env_file = Path(__file__).resolve().parents[1] / ".env"
if _env_file.exists():
    load_dotenv(_env_file)


def _enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    allowed_origins: list[str]
    auth_required: bool
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    access_token_minutes: int
    bootstrap_admin_email: str | None
    bootstrap_admin_password: str | None
    rate_limit_per_minute: int

    @classmethod
    def from_environment(cls) -> "Settings":
        environment = os.getenv("EDGEFLEET_ENV", "development").strip().lower()
        data_path = Path(os.getenv("EDGEFLEET_DATA_PATH", str(Path(__file__).resolve().parents[1] / "data" / "edgefleet.db"))).resolve()
        database_url = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{data_path.as_posix()}")
        auth_required = _enabled(os.getenv("AUTH_REQUIRED"), environment == "production")
        jwt_secret = os.getenv("JWT_SECRET", "")
        if environment == "production":
            if not database_url.startswith("postgresql+"):
                raise RuntimeError("Production requires DATABASE_URL using an async PostgreSQL driver.")
            if not auth_required or len(jwt_secret.encode()) < 32:
                raise RuntimeError("Production requires AUTH_REQUIRED=true and JWT_SECRET of at least 256 bits.")
        return cls(
            environment=environment,
            database_url=database_url,
            allowed_origins=[value.strip() for value in os.getenv("EDGEFLEET_ALLOWED_ORIGINS", "http://localhost:3000").split(",") if value.strip()],
            auth_required=auth_required,
            jwt_secret=jwt_secret or "development-only-secret-change-before-deploying",
            jwt_issuer=os.getenv("JWT_ISSUER", "edgefleet-api"),
            jwt_audience=os.getenv("JWT_AUDIENCE", "edgefleet-console"),
            access_token_minutes=max(5, min(60, int(os.getenv("ACCESS_TOKEN_MINUTES", "15")))),
            bootstrap_admin_email=os.getenv("BOOTSTRAP_ADMIN_EMAIL") or None,
            bootstrap_admin_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or None,
            rate_limit_per_minute=max(10, int(os.getenv("RATE_LIMIT_PER_MINUTE", "240"))),
        )
