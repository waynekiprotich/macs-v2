"""ML schema: trades, signals, market_snapshots, model_versions, model_predictions, risk_events

Revision ID: 0001
Revises:
Create Date: 2026-09-12

Reaches one schema from two starting points:
- Empty database (Supabase): every table is created.
- Pre-Alembic SQLite macs.db: paper_trades/system_logs are renamed to
  trades/signals and gain the new columns, so trade history — and with it
  RiskManager's daily-loss and circuit-breaker state — carries over.

Column meanings are documented on the models in models/database.py.
"""
import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSON = sa.JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")
TS = sa.DateTime(timezone=True)

TRADE_INDEXES = {
    "ix_trades_symbol": ["symbol"],
    "ix_trades_status": ["status"],
    "ix_trades_timestamp": ["timestamp"],
    "ix_trades_signal_id": ["signal_id"],
}
SIGNAL_INDEXES = {
    "ix_signals_symbol": ["symbol"],
    "ix_signals_timestamp": ["timestamp"],
    "ix_signals_symbol_candle_time": ["symbol", "candle_time"],
}
# Indexes the pre-Alembic app created. ix_*_id duplicated the primary key.
LEGACY_INDEXES = {
    "paper_trades": ["ix_paper_trades_id", "ix_paper_trades_symbol"],
    "system_logs": ["ix_system_logs_id", "ix_system_logs_symbol"],
}


def _base_trade_columns():
    """Columns paper_trades already had before this migration."""
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("side", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("order_id", sa.String()),
        sa.Column("reason", sa.String()),
        sa.Column("pnl", sa.Float()),
        sa.Column("proposal_id", sa.String()),
        sa.Column("contract_id", sa.String()),
        sa.Column("result", sa.String()),
        sa.Column("payout", sa.Float()),
        sa.Column("tech_score", sa.Float()),
        sa.Column("ai_score", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("regime", sa.String()),
        sa.Column("error_reason", sa.String()),
        sa.Column("closed_timestamp", TS),
        sa.Column("timestamp", TS),
    ]


def _new_trade_columns():
    return [
        sa.Column("signal_id", sa.Integer()),
        sa.Column("broker", sa.String()),
        sa.Column("mode", sa.String()),
        sa.Column("contract_type", sa.String()),
        sa.Column("duration", sa.Integer()),
        sa.Column("duration_unit", sa.String()),
        sa.Column("entry_time", TS),
        sa.Column("expiry_time", TS),
        sa.Column("entry_spot", sa.Float()),
        sa.Column("exit_spot", sa.Float()),
        sa.Column("quoted_payout", sa.Float()),
        sa.Column("sell_price", sa.Float()),
        sa.Column("spread", sa.Float()),
    ]


def _base_signal_columns():
    """Columns system_logs already had before this migration."""
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", TS),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("tech_score", sa.Float()),
        sa.Column("ai_score", sa.Float()),
        sa.Column("combined_confidence", sa.Float()),
        sa.Column("regime", sa.String()),
        sa.Column("is_volatile", sa.Boolean()),
        sa.Column("signal", sa.String(), nullable=False),
        sa.Column("error_warning", sa.String()),
    ]


def _new_signal_columns():
    return [
        sa.Column("candle_time", TS),
        sa.Column("granularity", sa.Integer()),
        sa.Column("close_price", sa.Float()),
        sa.Column("bull_conditions", sa.Integer()),
        sa.Column("bear_conditions", sa.Integer()),
        sa.Column("min_conditions", sa.Integer()),
        sa.Column("take_profit", sa.Float()),
        sa.Column("stop_loss", sa.Float()),
        sa.Column("reason", sa.String()),
        sa.Column("strategy", sa.String()),
        sa.Column("action_taken", sa.String()),
        sa.Column("indicators", JSON),
    ]


def _upgrade_legacy():
    # Legacy index names carry the old table names, so they go before the rename.
    for table, indexes in LEGACY_INDEXES.items():
        for name in indexes:
            op.drop_index(name, table_name=table)
    op.rename_table("system_logs", "signals")
    op.rename_table("paper_trades", "trades")

    with op.batch_alter_table("signals") as batch:
        for column in _new_signal_columns():
            batch.add_column(column)
    with op.batch_alter_table("trades") as batch:
        for column in _new_trade_columns():
            batch.add_column(column)
        batch.create_foreign_key(
            "fk_trades_signal_id_signals", "signals", ["signal_id"], ["id"], ondelete="SET NULL"
        )


def upgrade():
    existing = set() if context.is_offline_mode() else set(sa.inspect(op.get_bind()).get_table_names())

    if "paper_trades" in existing:
        _upgrade_legacy()
    else:
        op.create_table("signals", *_base_signal_columns(), *_new_signal_columns())
        op.create_table(
            "trades",
            *_base_trade_columns(),
            *_new_trade_columns(),
            sa.ForeignKeyConstraint(
                ["signal_id"], ["signals.id"], name="fk_trades_signal_id_signals", ondelete="SET NULL"
            ),
            sa.UniqueConstraint("order_id", name="uq_trades_order_id"),
        )

    for name, columns in SIGNAL_INDEXES.items():
        op.create_index(name, "signals", columns)
    for name, columns in TRADE_INDEXES.items():
        op.create_index(name, "trades", columns)

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("granularity", sa.Integer(), nullable=False),
        sa.Column("candle_time", TS, nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float()),
        sa.Column("spread", sa.Float()),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("indicators", JSON),
        sa.Column("created_at", TS),
        sa.UniqueConstraint(
            "symbol", "granularity", "candle_time",
            name="uq_market_snapshots_symbol_granularity_candle_time",
        ),
    )

    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("model_type", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("feature_names", JSON, nullable=False),
        sa.Column("hyperparams", JSON),
        sa.Column("metrics", JSON),
        sa.Column("decision_threshold", sa.Float()),
        sa.Column("train_start", TS),
        sa.Column("train_end", TS),
        sa.Column("test_start", TS),
        sa.Column("test_end", TS),
        sa.Column("n_train", sa.Integer()),
        sa.Column("n_test", sa.Integer()),
        sa.Column("artifact_uri", sa.String()),
        sa.Column("artifact_sha256", sa.String()),
        sa.Column("trained_at", TS, nullable=False),
        sa.Column("promoted_at", TS),
        sa.Column("retired_at", TS),
        sa.Column("notes", sa.String()),
        sa.UniqueConstraint("version", name="uq_model_versions_version"),
    )
    op.create_index(
        "ix_model_versions_one_active_per_target", "model_versions", ["target"], unique=True,
        postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "model_predictions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", TS),
        sa.Column("model_version_id", sa.Integer(), nullable=False),
        sa.Column("signal_id", sa.Integer()),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("candle_time", TS),
        sa.Column("side", sa.String()),
        sa.Column("predicted_proba", sa.Float(), nullable=False),
        sa.Column("rule_confidence", sa.Float()),
        sa.Column("blended_score", sa.Float()),
        sa.Column("decision", sa.String()),
        sa.Column("features", JSON),
        sa.Column("actual_won", sa.Boolean()),
        sa.Column("actual_pnl", sa.Float()),
        sa.Column("resolved_at", TS),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"],
            name="fk_model_predictions_model_version_id_model_versions",
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"], name="fk_model_predictions_signal_id_signals", ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "model_version_id", "signal_id", name="uq_model_predictions_model_version_id_signal_id"
        ),
    )
    op.create_index(
        "ix_model_predictions_model_version_id_timestamp", "model_predictions", ["model_version_id", "timestamp"]
    )

    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("timestamp", TS),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("symbol", sa.String()),
        sa.Column("trade_id", sa.Integer()),
        sa.Column("signal_id", sa.Integer()),
        sa.Column("model_version_id", sa.Integer()),
        sa.Column("daily_pnl", sa.Float()),
        sa.Column("consecutive_losses", sa.Integer()),
        sa.Column("cooldown_until", TS),
        sa.Column("threshold", sa.Float()),
        sa.Column("observed", sa.Float()),
        sa.Column("message", sa.String()),
        sa.Column("details", JSON),
        sa.ForeignKeyConstraint(
            ["trade_id"], ["trades.id"], name="fk_risk_events_trade_id_trades", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"], name="fk_risk_events_signal_id_signals", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"], ["model_versions.id"],
            name="fk_risk_events_model_version_id_model_versions", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_risk_events_timestamp", "risk_events", ["timestamp"])
    op.create_index("ix_risk_events_event_type", "risk_events", ["event_type"])


def downgrade():
    """Back to the pre-Alembic paper_trades/system_logs shape, legacy indexes
    included, so upgrade can run again. Keeps every row that existed before;
    drops only what this migration added."""
    op.drop_table("risk_events")
    op.drop_table("model_predictions")
    op.drop_table("model_versions")
    op.drop_table("market_snapshots")

    for name in TRADE_INDEXES:
        op.drop_index(name, table_name="trades")
    for name in SIGNAL_INDEXES:
        op.drop_index(name, table_name="signals")

    with op.batch_alter_table("trades") as batch:
        batch.drop_constraint("fk_trades_signal_id_signals", type_="foreignkey")
        for column in _new_trade_columns():
            batch.drop_column(column.name)
    with op.batch_alter_table("signals") as batch:
        for column in _new_signal_columns():
            batch.drop_column(column.name)

    op.rename_table("trades", "paper_trades")
    op.rename_table("signals", "system_logs")
    for table, indexes in LEGACY_INDEXES.items():
        for name in indexes:
            op.create_index(name, table, [name.rsplit("_", 1)[-1]])
