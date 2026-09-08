"""Copy Zeta's database from one backend to another (e.g. local SQLite -> Supabase).

    python scripts/migrate_db.py --to "postgresql://postgres.<ref>:<pw>@aws-0-<region>.pooler.supabase.com:6543/postgres"
    python scripts/migrate_db.py --from "postgresql://..." --to "sqlite:///D:/Projects/Zeta/data/zeta.db"

Defaults: --from is the current DATABASE_URL (or the local SQLite file), tables are
created on the target if missing, and rows that already exist there (same primary
key) are skipped, so the script is safe to re-run.  Set DATABASE_URL in .env to
the target afterwards and restart Zeta.

The file search index (data/file_index.db) is not migrated: it mirrors this
computer's disk and is rebuilt automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.database import Base, normalize_database_url  # noqa: E402
from app.models import db as models  # noqa: E402

TABLES = [models.Conversation, models.Message, models.TaskRecord, models.MemoryItem, models.ScheduledJob, models.AuditLog]


def _engine(url: str):
    norm, kwargs = normalize_database_url(url)
    return create_async_engine(norm, **kwargs)


async def migrate(src_url: str, dst_url: str, batch: int = 500) -> None:
    src, dst = _engine(src_url), _engine(dst_url)
    async with dst.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    S, D = async_sessionmaker(src, expire_on_commit=False), async_sessionmaker(dst, expire_on_commit=False)
    for model in TABLES:
        cols = [c.name for c in model.__table__.columns]
        pk = [c.name for c in model.__table__.primary_key.columns][0]
        async with S() as s:
            rows = (await s.execute(select(model))).scalars().all()
        async with D() as d:
            existing = set((await d.execute(select(getattr(model, pk)))).scalars().all())
        copied = 0
        for i in range(0, len(rows), batch):
            chunk = [r for r in rows[i:i + batch] if getattr(r, pk) not in existing]
            if not chunk:
                continue
            async with D() as d:
                for r in chunk:
                    data = {c: getattr(r, c) for c in cols}
                    if model is models.AuditLog:
                        data.pop("id", None)  # autoincrement on the target
                    d.add(model(**data))
                await d.commit()
            copied += len(chunk)
        print(f"{model.__tablename__:16s} source={len(rows):6d}  copied={copied:6d}  skipped(existing)={len(rows) - copied}")
    await src.dispose()
    await dst.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src", default="", help="source DATABASE_URL (default: current setting / local SQLite)")
    ap.add_argument("--to", dest="dst", required=True, help="target DATABASE_URL")
    args = ap.parse_args()
    settings = get_settings()
    src = args.src or settings.database_url
    if normalize_database_url(src)[0] == normalize_database_url(args.dst)[0]:
        sys.exit("source and target are the same database")
    print(f"from: {normalize_database_url(src)[0].split('@')[-1]}")
    print(f"to:   {normalize_database_url(args.dst)[0].split('@')[-1]}")
    asyncio.run(migrate(src, args.dst))
    print("done. Set DATABASE_URL in .env to the target and restart Zeta.")


if __name__ == "__main__":
    main()
