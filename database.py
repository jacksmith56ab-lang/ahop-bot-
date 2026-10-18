from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from models import Base


def make_engine(database_path: Path | str) -> AsyncEngine:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)


def make_session_factory(engine: AsyncEngine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_database(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
        # Add configurable task texts to databases created by older versions.
        columns = await conn.exec_driver_sql("PRAGMA table_info(tasks)")
        existing = {row[1] for row in columns.fetchall()}
        if "description" not in existing:
            await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        if "button_text" not in existing:
            await conn.exec_driver_sql("ALTER TABLE tasks ADD COLUMN button_text VARCHAR(64) NOT NULL DEFAULT ''")
