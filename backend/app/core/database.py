"""Async SQLAlchemy engine/session.

SQLite (default, zero-config, local-first) or PostgreSQL - including Supabase -
via `DATABASE_URL`.  Examples:

    sqlite+aiosqlite:///D:/Projects/Zeta/data/zeta.db          (default)
    postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
    postgresql+asyncpg://user:pass@localhost:5432/zeta

Plain `postgres://` / `postgresql://` URLs are rewritten to the asyncpg driver,
`sslmode=require` is translated to an asyncpg SSL context, and remote hosts
default to SSL (Supabase requires it).  Supabase's transaction pooler (port
6543, PgBouncer) does not support prepared statements, so the asyncpg statement
cache is disabled for pooler hosts.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def normalize_database_url(url: str) -> Tuple[str, Dict[str, Any]]:
    """Return (sqlalchemy_url, engine_kwargs) for a user-supplied DATABASE_URL."""
    url = (url or "").strip()
    kwargs: Dict[str, Any] = {}
    if url.startswith("sqlite"):
        if url.startswith("sqlite:///") and "+aiosqlite" not in url:
            url = "sqlite+aiosqlite:///" + url[len("sqlite:///"):]
        kwargs["connect_args"] = {"timeout": 30}
        return url, kwargs

    if url.startswith(("postgres://", "postgresql://", "postgresql+psycopg2://", "postgresql+psycopg://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if not url.startswith("postgresql+asyncpg://"):
        return url, kwargs

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", None)
    ssl_param = query.pop("ssl", None)
    host = (parts.hostname or "").lower()
    connect_args: Dict[str, Any] = {}
    want_ssl = (sslmode not in (None, "disable", "allow")) or (ssl_param not in (None, "false", "0", "disable")) or \
               (sslmode is None and ssl_param is None and host and host not in _LOCAL_HOSTS)
    if want_ssl:
        connect_args["ssl"] = "require"
    if "pooler.supabase.com" in host or "pgbouncer" in host or (parts.port == 6543):
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_cache_size"] = 0
    if connect_args:
        kwargs["connect_args"] = connect_args
    kwargs["pool_pre_ping"] = True
    kwargs["pool_size"] = 5
    kwargs["max_overflow"] = 5
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, kwargs


def describe_database_url(url: str) -> Dict[str, Any]:
    """Safe, secret-free description of the configured database (for the UI)."""
    try:
        norm, _ = normalize_database_url(url)
        parts = urlsplit(norm)
    except Exception:  # noqa: BLE001
        return {"backend": "unknown", "location": "", "supabase": False}
    if norm.startswith("sqlite"):
        return {"backend": "sqlite", "location": norm.split("///", 1)[-1], "supabase": False}
    host = parts.hostname or ""
    return {"backend": parts.scheme.split("+")[0], "location": f"{host}:{parts.port or 5432}{parts.path}",
            "supabase": "supabase" in host}


def init_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    global _engine, _session_factory
    url, kwargs = normalize_database_url(database_url)
    if url.startswith("postgresql+asyncpg"):
        try:
            import asyncpg  # noqa: F401
        except ImportError as e:
            from app.core.exceptions import ConfigurationError

            raise ConfigurationError("DATABASE_URL points at PostgreSQL but the driver is missing. Run: pip install asyncpg") from e
    _engine = create_async_engine(url, echo=echo, future=True, **kwargs)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised. Call init_engine() first.")
    return _engine


def is_initialised() -> bool:
    return _engine is not None


def backend_name() -> str:
    return _engine.url.get_backend_name() if _engine is not None else "none"


async def create_all() -> None:
    from app.models import db as _models  # noqa: F401  (ensure models are imported)

    engine = get_engine()
    async with engine.begin() as conn:
        if engine.url.get_backend_name() == "sqlite":
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)


async def ping() -> Dict[str, Any]:
    """Cheap connectivity check used by /api/system/status. Never raises."""
    if _engine is None:
        return {"ok": False, "detail": "not initialised"}
    try:
        async with _engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        return {"ok": True, "detail": _engine.url.get_backend_name()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{e.__class__.__name__}: {str(e)[:120]}"}


async def dispose() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Database not initialised")
    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
