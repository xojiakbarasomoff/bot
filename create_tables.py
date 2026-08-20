"""Temporary bootstrap script: create all database tables on deploy.

This exists because the app currently has Alembic wired up (see
migrations/) but relies on tables actually being created against the
target database before requests start hitting endpoints like the webhook.
Running Base.metadata.create_all() here is idempotent — it only creates
tables that don't already exist — so it's safe to run on every deploy.

This is a stop-gap. Once the Alembic migration workflow is fully adopted
for schema changes, this script should be replaced by `alembic upgrade
head` as the pre-deploy step.
"""

import asyncio

from sqlalchemy.ext.asyncio import create_async_engine

# Import the models package so every model class registers itself on
# Base.metadata before create_all() runs. Base itself is also re-exported
# from app.models, so importing app.models is enough.
from app.core.config import get_settings
from app.models import Base


async def create_tables() -> None:
    database_url = get_settings().database_url
    engine = create_async_engine(database_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await engine.dispose()

    print("Database tables created (or already existed).")


if __name__ == "__main__":
    asyncio.run(create_tables())
