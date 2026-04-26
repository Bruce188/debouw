"""
Async SQLAlchemy engine + sessionmaker factories.

No module-level engine — both are factory functions called by the CLI / pipeline.
"""

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from debouw.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    """Build the async SQLAlchemy engine pointed at SQLite WAL mode."""
    # Ensure the data directory exists before sqlite creates the file
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{settings.db_path}",
        echo=False,
        future=True,
        connect_args={"timeout": 30},
    )

    # Enable WAL mode on every new connection for concurrent read/write
    @event.listens_for(engine.sync_engine, "connect")
    def _set_wal_mode(dbapi_conn, connection_record):  # noqa: ARG001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return an async sessionmaker bound to the given engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
