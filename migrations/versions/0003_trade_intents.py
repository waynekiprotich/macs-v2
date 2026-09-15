"""trade_intents: claim a BUY before it is sent to Deriv

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15

One row per symbol, closed candle and direction, committed before the buy.
The unique constraint stops a second buy on the same signal (a restarted or
duplicate worker), and a row left unresolved blocks trading until the
contract it may stand for is recorded or ruled out.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

JSON = sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "trade_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("candle_time", TS, nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("stake", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("contract_id", sa.String()),
        sa.Column("signal_id", sa.Integer()),
        sa.Column("trade_id", sa.Integer()),
        sa.Column("error", sa.String()),
        sa.Column("details", JSON),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"], name="fk_trade_intents_signal_id_signals", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name="fk_trade_intents_trade_id_trades", ondelete="SET NULL"
        ),
        sa.UniqueConstraint("symbol", "candle_time", "side", name="uq_trade_intents_symbol_candle_time_side"),
    )
    op.create_index("ix_trade_intents_status", "trade_intents", ["status"])
    # Same reason as 0002: keep the table out of Supabase's public Data API.
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE trade_intents ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.drop_index("ix_trade_intents_status", table_name="trade_intents")
    op.drop_table("trade_intents")
