import logging
import os
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String,
    UniqueConstraint, create_engine, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker

from config.settings import settings

logger = logging.getLogger(__name__)

db_url = settings.DATABASE_URL
if db_url.startswith("sqlite:///"):
    db_path = db_url.replace("sqlite:///", "")
    if not os.path.isabs(db_path) and db_path != ":memory:":
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_path = os.path.join(base_dir, db_path)
        db_url = f"sqlite:///{db_path}"

if db_url.startswith("sqlite"):
    engine_options = {"connect_args": {"check_same_thread": False}}
else:
    # Supabase drops idle connections; pre-ping replaces a dead one instead of
    # failing the first query of a pipeline cycle.
    engine_options = {
        "pool_pre_ping": True,
        # Without a timeout an unreachable pooler blocks a cycle indefinitely.
        "connect_args": {"connect_timeout": 10},
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_recycle": 1800,
    }

# Deterministic constraint and index names, so these models and
# migrations/versions agree on what every constraint is called.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

try:
    engine = create_engine(db_url, **engine_options)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base = declarative_base(metadata=MetaData(naming_convention=NAMING_CONVENTION))
except Exception as e:
    logger.error(f"Failed to initialize database engine: {e}")
    raise

# JSONB on Postgres, plain JSON on SQLite. none_as_null stores Python None as
# SQL NULL instead of the JSON literal null, so IS NULL filters mean what they say.
JSONType = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
UTCDateTime = DateTime(timezone=True)


def _utcnow():
    return datetime.now(timezone.utc)


class PaperTrade(Base):
    """One contract bought (Deriv) or simulated (paper). The class name
    predates the paper_trades -> trades rename; kept so imports keep working."""
    __tablename__ = "trades"
    # One row per Deriv contract (migration 0004).
    __table_args__ = (Index("uq_trades_contract_id", "contract_id", unique=True),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String, index=True, nullable=False)
    side = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)  # stake requested
    price = Column(Float, nullable=False)  # stake actually charged (Deriv buy_price)
    status = Column(String, index=True, nullable=False, default="OPEN")
    order_id = Column(String, nullable=True, unique=True)
    reason = Column(String, nullable=True)
    pnl = Column(Float, nullable=True)
    proposal_id = Column(String, nullable=True)
    contract_id = Column(String, nullable=True)
    result = Column(String, nullable=True)
    # What the contract pays on a win: Deriv's quote at buy, confirmed by
    # reconcile. The same whether the contract won or lost; what it settled for
    # is sell_price, and the result is pnl. Rows reconciled before this change
    # can hold 0 for a loss; quoted_payout keeps the untouched quote.
    payout = Column(Float, nullable=True)
    tech_score = Column(Float, nullable=True)
    ai_score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    regime = Column(String, nullable=True)
    error_reason = Column(String, nullable=True)
    closed_timestamp = Column(UTCDateTime, nullable=True)
    timestamp = Column(UTCDateTime, index=True, default=_utcnow)

    # The signal this trade was placed on: joins an outcome to the exact
    # features that produced it.
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="SET NULL"), index=True, nullable=True)
    broker = Column(String, nullable=True)  # "deriv", "paper"
    mode = Column(String, nullable=True)  # settings.MACS_MODE when the trade was placed
    contract_type = Column(String, nullable=True)  # CALL / PUT
    duration = Column(Integer, nullable=True)
    duration_unit = Column(String, nullable=True)
    entry_time = Column(UTCDateTime, nullable=True)
    expiry_time = Column(UTCDateTime, nullable=True)
    entry_spot = Column(Float, nullable=True)
    exit_spot = Column(Float, nullable=True)
    # Deriv's payout quote at buy. For a CALL/PUT this is the trading cost:
    # breakeven win rate = price / quoted_payout (170 / 314.5 = 54.1%).
    quoted_payout = Column(Float, nullable=True)
    sell_price = Column(Float, nullable=True)
    # Bid/ask spread at entry, in price units, for instruments quoted with one.
    # Null for Deriv CALL/PUT, whose cost is in quoted_payout instead.
    spread = Column(Float, nullable=True)


class SystemLog(Base):
    """One pipeline evaluation of one symbol: a BUY/SELL/HOLD decision,
    traded or not. The class name predates the system_logs -> signals
    rename; kept so imports keep working."""
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_symbol_candle_time", "symbol", "candle_time"),)

    id = Column(Integer, primary_key=True)
    timestamp = Column(UTCDateTime, index=True, default=_utcnow)
    symbol = Column(String, index=True, nullable=False)
    tech_score = Column(Float, nullable=True)
    ai_score = Column(Float, nullable=True)
    combined_confidence = Column(Float, nullable=True)
    regime = Column(String, nullable=True)
    is_volatile = Column(Boolean, nullable=True)
    signal = Column(String, nullable=False)
    error_warning = Column(String, nullable=True)

    candle_time = Column(UTCDateTime, nullable=True)  # open time of the bar evaluated
    granularity = Column(Integer, nullable=True)  # bar length in seconds (900 = 15m)
    close_price = Column(Float, nullable=True)
    bull_conditions = Column(Integer, nullable=True)
    bear_conditions = Column(Integer, nullable=True)
    min_conditions = Column(Integer, nullable=True)  # threshold in force
    take_profit = Column(Float, nullable=True)  # diagnostic only, see technical_strategy
    stop_loss = Column(Float, nullable=True)
    reason = Column(String, nullable=True)
    strategy = Column(String, nullable=True)
    # EXECUTED, DRY_RUN, RISK_BLOCKED, VOLATILITY_SKIP, HOLD, ERROR
    action_taken = Column(String, nullable=True)
    # The prepared indicator row the decision was made on; the input to
    # ml.features.compute_features. Write NaN as None: JSONB rejects NaN.
    indicators = Column(JSONType, nullable=True)


class MarketSnapshot(Base):
    """One OHLC bar per symbol and granularity. Labels signals that weren't
    traded, and is the price history models train on."""
    __tablename__ = "market_snapshots"
    __table_args__ = (UniqueConstraint("symbol", "granularity", "candle_time"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    granularity = Column(Integer, nullable=False)
    candle_time = Column(UTCDateTime, nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    # Null for Deriv: data_deriv.py fills Volume with a constant 1000.0 placeholder.
    volume = Column(Float, nullable=True)
    spread = Column(Float, nullable=True)
    source = Column(String, nullable=False, default="deriv")
    indicators = Column(JSONType, nullable=True)
    created_at = Column(UTCDateTime, default=_utcnow)


class ModelVersion(Base):
    """A trained model: what it learned from, how it scored out of sample,
    and where its artifact is. At most one 'active' model per target."""
    __tablename__ = "model_versions"
    __table_args__ = (
        Index(
            "ix_model_versions_one_active_per_target", "target", unique=True,
            postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    version = Column(String, nullable=False, unique=True)
    model_type = Column(String, nullable=False)  # "xgboost"
    target = Column(String, nullable=False)  # what is predicted, e.g. "won_15m_callput"
    status = Column(String, nullable=False, default="candidate")  # candidate / active / retired
    feature_names = Column(JSONType, nullable=False)  # ordered, as ml.features.FEATURE_COLUMNS was at train time
    hyperparams = Column(JSONType, nullable=True)
    metrics = Column(JSONType, nullable=True)  # out-of-sample only
    decision_threshold = Column(Float, nullable=True)
    train_start = Column(UTCDateTime, nullable=True)
    train_end = Column(UTCDateTime, nullable=True)
    test_start = Column(UTCDateTime, nullable=True)
    test_end = Column(UTCDateTime, nullable=True)
    n_train = Column(Integer, nullable=True)
    n_test = Column(Integer, nullable=True)
    artifact_uri = Column(String, nullable=True)
    artifact_sha256 = Column(String, nullable=True)
    trained_at = Column(UTCDateTime, nullable=False, default=_utcnow)
    promoted_at = Column(UTCDateTime, nullable=True)
    retired_at = Column(UTCDateTime, nullable=True)
    notes = Column(String, nullable=True)


class ModelPrediction(Base):
    """What a model said about a signal and, once the contract settles, what
    actually happened. Live accuracy and drift monitoring read this."""
    __tablename__ = "model_predictions"
    __table_args__ = (
        UniqueConstraint("model_version_id", "signal_id"),
        Index("ix_model_predictions_model_version_id_timestamp", "model_version_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True)
    timestamp = Column(UTCDateTime, default=_utcnow)
    model_version_id = Column(Integer, ForeignKey("model_versions.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True)
    symbol = Column(String, nullable=False)
    candle_time = Column(UTCDateTime, nullable=True)
    side = Column(String, nullable=True)
    predicted_proba = Column(Float, nullable=False)  # P(win) for side
    rule_confidence = Column(Float, nullable=True)  # technical_strategy confidence, 0-100
    blended_score = Column(Float, nullable=True)
    decision = Column(String, nullable=True)  # TAKE / SKIP
    features = Column(JSONType, nullable=True)  # exact inputs, keyed by feature name
    actual_won = Column(Boolean, nullable=True)
    actual_pnl = Column(Float, nullable=True)
    resolved_at = Column(UTCDateTime, nullable=True)


class RiskEvent(Base):
    """A risk decision or alarm (circuit breaker, daily loss limit, blocked
    trade, model drift), written when state changes rather than every cycle."""
    __tablename__ = "risk_events"

    id = Column(Integer, primary_key=True)
    timestamp = Column(UTCDateTime, index=True, default=_utcnow)
    event_type = Column(String, index=True, nullable=False)  # CIRCUIT_BREAKER, DAILY_LOSS_LIMIT, TRADE_BLOCKED, DRIFT_ALERT
    severity = Column(String, nullable=False, default="WARN")  # INFO / WARN / CRITICAL
    symbol = Column(String, nullable=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="SET NULL"), nullable=True)
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True)
    model_version_id = Column(Integer, ForeignKey("model_versions.id", ondelete="SET NULL"), nullable=True)
    daily_pnl = Column(Float, nullable=True)
    consecutive_losses = Column(Integer, nullable=True)
    cooldown_until = Column(UTCDateTime, nullable=True)
    threshold = Column(Float, nullable=True)  # the limit that was crossed
    observed = Column(Float, nullable=True)  # the value that crossed it
    message = Column(String, nullable=True)
    details = Column(JSONType, nullable=True)


# A trade intent in any of these states may stand for a contract Deriv sold
# that trades doesn't have; RiskManager blocks trading while one exists.
UNRESOLVED_INTENT_STATUSES = ("PENDING", "AMBIGUOUS", "UNRECORDED")


class TradeIntent(Base):
    """A BUY claimed before it is sent to Deriv, one per symbol, closed candle
    and direction, so one signal can never buy twice. It is committed before
    the buy, so a contract that was bought but never recorded still leaves a
    row behind.

    PENDING: claimed; the buy is about to be sent, or the process died.
    FAILED: Deriv refused or the buy was never sent; nothing was bought.
    EXECUTED: bought and recorded in trades (trade_id).
    AMBIGUOUS: the buy was sent but no usable response came back.
    UNRECORDED: bought (contract_id known) but the trades row wasn't written;
        reconciliation writes it from details.
    RESOLVED: an operator settled an unresolved row by hand.
    """
    __tablename__ = "trade_intents"
    __table_args__ = (UniqueConstraint("symbol", "candle_time", "side"),)

    id = Column(Integer, primary_key=True)
    created_at = Column(UTCDateTime, nullable=False, default=_utcnow)
    updated_at = Column(UTCDateTime, nullable=True)
    symbol = Column(String, nullable=False)
    candle_time = Column(UTCDateTime, nullable=False)  # open time of the closed bar the signal was on
    side = Column(String, nullable=False)  # BUY / SELL
    stake = Column(Float, nullable=False)
    status = Column(String, index=True, nullable=False, default="PENDING")
    contract_id = Column(String, nullable=True)
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="SET NULL"), nullable=True)
    error = Column(String, nullable=True)
    # The trades row as it would have been written, so an UNRECORDED contract
    # can be recorded later without asking Deriv to buy anything.
    details = Column(JSONType, nullable=True)


def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise

def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        logger.error(f"Database session error: {e}")
        raise
    finally:
        db.close()
