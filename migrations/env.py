"""Alembic environment.

The database URL comes from DATABASE_URL (through models.database, so a
relative SQLite path resolves exactly as the app resolves it) — never from
alembic.ini, so credentials stay in the gitignored .env.

Target another database without editing .env:
    alembic -x db_url=postgresql://... upgrade head
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from config.settings import normalize_database_url
from models.database import Base, db_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # attributes: set programmatically (tests). -x: set on the command line.
    override = config.attributes.get("db_url") or context.get_x_argument(as_dictionary=True).get("db_url")
    return normalize_database_url(override) if override else db_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
