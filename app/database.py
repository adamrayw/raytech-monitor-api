import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


DATABASE_URL = os.environ["DATABASE_URL"]

engine_kwargs = {
    "pool_pre_ping": True,
}

# Only configure QueuePool parameters for client-server databases like PostgreSQL
if not DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update({
        "pool_size": int(os.environ.get("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", "10")),
        "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "1800")),
        "pool_timeout": int(os.environ.get("DB_POOL_TIMEOUT", "30")),
    })

engine = create_engine(
    DATABASE_URL,
    **engine_kwargs,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
