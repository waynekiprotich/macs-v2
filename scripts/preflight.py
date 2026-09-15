"""Refuse to run a trading cycle that the database could not record.

Exits non-zero with one line per problem when:
- DERIV_API_TOKEN or DERIV_APP_ID is unset,
- DATABASE_URL is SQLite and MACS_ALLOW_SQLITE is not "1" (a container's
  SQLite file is lost on every redeploy, and with it the trade history the
  risk manager reads its loss limits from),
- DATABASE_URL uses Supabase's transaction pooler (port 6543), which can't
  hold the single-instance trading lock,
- the database cannot be reached,
- the schema is not at the Alembic head revision.

start_macs.sh runs this before every cycle and skips the cycle on failure.

    python -m scripts.preflight
"""
import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
REQUIRED_ENV = ("DERIV_API_TOKEN", "DERIV_APP_ID")


def _head_revisions() -> set:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def check(db_url: str, environ) -> list:
    problems = [f"{name} is not set" for name in REQUIRED_ENV if not environ.get(name)]

    if db_url.startswith("sqlite") and environ.get("MACS_ALLOW_SQLITE") != "1":
        problems.append("DATABASE_URL is SQLite; set a Postgres URL, or MACS_ALLOW_SQLITE=1 for local development")
        return problems

    if db_url.startswith("postgresql") and make_url(db_url).port == 6543:
        problems.append(
            "DATABASE_URL uses the transaction pooler (port 6543), which cannot hold the trading lock; "
            "use the session pooler on port 5432"
        )
        return problems

    connect_args = {"connect_timeout": 10} if db_url.startswith("postgresql") else {}
    engine = create_engine(db_url, poolclass=pool.NullPool, connect_args=connect_args)
    try:
        with engine.connect() as conn:
            current = set(MigrationContext.configure(conn).get_current_heads())
    except Exception as e:
        # The driver's own message names host and user, never the password.
        detail = str(getattr(e, "orig", e)).strip().splitlines()
        problems.append(f"cannot connect to the database: {detail[0] if detail else type(e).__name__}")
        return problems
    finally:
        engine.dispose()

    head = _head_revisions()
    if current != head:
        problems.append(
            f"database schema is at {sorted(current) or 'no revision'}, expected {sorted(head)}; run `alembic upgrade head`"
        )
    return problems


def main() -> int:
    # Imported here so settings loads .env before the environment is read.
    from models.database import db_url

    problems = check(db_url, os.environ)
    for problem in problems:
        print(f"PREFLIGHT FAILED: {problem}", flush=True)
    if not problems:
        print("Preflight OK: database reachable, schema at head, Deriv credentials set.", flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
