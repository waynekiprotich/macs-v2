"""
Copy every row from one MACS database into another with the same schema.
Used once to move the live SQLite macs.db onto Supabase.

Both databases must be at the same Alembic revision. Refuses to write into
a target that already has rows, so it can never double-copy. Row ids are
kept, so trades.signal_id and friends still point at the right rows, and
Postgres id sequences are advanced past the copied ids.

    python -m scripts.copy_database --source sqlite:///macs.db --target "$TARGET_DATABASE_URL"
"""
import argparse
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select, text

from config.settings import normalize_database_url
from models.database import Base


def _revision(conn) -> str:
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _as_utc(value):
    # SQLite returns naive datetimes that hold UTC; say so explicitly rather
    # than let Postgres read them in its session time zone.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def copy_database(source_url: str, target_url: str) -> dict:
    source = create_engine(normalize_database_url(source_url))
    target = create_engine(normalize_database_url(target_url))
    counts = {}
    with source.connect() as src, target.begin() as dst:
        if _revision(src) != _revision(dst):
            raise SystemExit(f"Revision mismatch: source {_revision(src)}, target {_revision(dst)}. Run alembic upgrade first.")
        for table in Base.metadata.sorted_tables:
            if dst.execute(select(func.count()).select_from(table)).scalar():
                raise SystemExit(f"Target table {table.name} already has rows; refusing to copy over them.")

        # sorted_tables is foreign-key order: signals before trades, and so on.
        for table in Base.metadata.sorted_tables:
            rows = [{k: _as_utc(v) for k, v in row._mapping.items()} for row in src.execute(select(table))]
            if rows:
                dst.execute(table.insert(), rows)
                if dst.dialect.name == "postgresql":
                    dst.execute(text(
                        f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), (SELECT MAX(id) FROM {table.name}))"
                    ))
            counts[table.name] = len(rows)
    source.dispose()
    target.dispose()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    for table, n in copy_database(args.source, args.target).items():
        print(f"{table}: {n} rows")


if __name__ == "__main__":
    main()
