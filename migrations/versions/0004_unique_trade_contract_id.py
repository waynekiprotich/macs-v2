"""Unique Deriv contract IDs in trades

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15

One trades row per Deriv contract, enforced by the database. Rows without a
contract ID (paper and legacy trades) are unaffected: NULLs never collide.
No existing row is changed.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_trades_contract_id", "trades", ["contract_id"], unique=True)


def downgrade():
    op.drop_index("uq_trades_contract_id", table_name="trades")
