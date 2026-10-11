"""Alembic environment.

D19: Phase 0 only initializes this config; it does NOT generate revisions.
DDL (including triggers / EXCLUDE / permission revocation) lives in
``migrations/001_init.sql`` and is executed directly with ``psql -f``.
Task 7 fills in ``target_metadata`` so that alembic autogenerate can later
track incremental DDL for Phase A/B.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# this is the Alembic Config object, which provides access to
# the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Set target_metadata so autogenerate can diff against our models.
# Filled in by Task 7 (`app/infra/models.py`).
from app.infra.models import Base  # type: ignore  # noqa: E402

target_metadata = Base.metadata


# Connection URL source: alembic.ini no longer hardcodes the URL (avoids
# committing plaintext credentials + drift between .env / compose).
# Prefer ZHL_DATABASE_URL from the environment (.env, see .env.example),
# fall back to the (now empty) sqlalchemy.url in ini. Keeps alembic in
# sync with the app / langgraph-checkpoint-postgres connection.


def _resolve_database_url() -> str:
    url = os.environ.get("ZHL_DATABASE_URL")
    if not url:
        # Load from project-root .env without a third-party dotenv dep.
        env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.isfile(env_file):
            with open(env_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    if key.strip() == "ZHL_DATABASE_URL" and val.strip():
                        url = val.strip().strip('"').strip("'")
                        break
    if not url:
        url = config.get_main_option("sqlalchemy.url") or ""
    url = url.strip()
    if not url:
        raise RuntimeError(
            "No database URL for alembic. Set ZHL_DATABASE_URL in .env "
            "(see .env.example) before running alembic."
        )
    return url


def _apply_database_url() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_database_url())


_apply_database_url()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
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
    """Run migrations in 'online' mode."""
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
