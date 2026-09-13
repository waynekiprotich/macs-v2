"""Enable row level security on every MACS table (Postgres only)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13

Supabase publishes every table in the public schema through its Data API.
Without RLS, anyone holding the project's anon key could read and write
trades. With RLS on and no policies, the API sees no rows, while the app,
which connects as the table owner, is unaffected: owners bypass RLS unless
it is forced. SQLite has no RLS, so this is a no-op there.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (
    "alembic_version", "signals", "trades", "market_snapshots",
    "model_versions", "model_predictions", "risk_events",
)


def upgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade():
    if op.get_context().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
