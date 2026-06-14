"""Alembic environment. Reads DATABASE_URL_SYNC from .env if present, falling back to alembic.ini."""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Load .env from the repo root (one level above backend/) so DATABASE_URL_SYNC is honoured.
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

config = context.config

if env_url := os.getenv("DATABASE_URL_SYNC"):
    config.set_main_option("sqlalchemy.url", env_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# Import models so 'autogenerate' can detect them.
from day_trade.db.base import Base  # noqa: E402
from day_trade.db import models  # noqa: E402, F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
