import datetime
import logging
from typing import Dict, Any
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import SessionLocal, PaperTrade, RiskEvent, TradeIntent, UNRESOLVED_INTENT_STATUSES

logger = logging.getLogger(__name__)


def _as_utc(value):
    # SQLite hands back naive datetimes that hold UTC.
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _is_due(trade, now: datetime.datetime, margin: datetime.timedelta) -> bool:
    """Whether an OPEN contract is at or past expiry, within margin. A row
    without a recorded expiry (legacy, or recorded by hand) counts as due: its
    outcome can't be ruled out."""
    if trade.expiry_time is None:
        return True
    return _as_utc(trade.expiry_time) - margin <= now


class RiskManager:
    # can_trade() refuses state older than this, so a decision always sees
    # contracts that settled since the state was read.
    MAX_STATE_AGE = datetime.timedelta(minutes=5)
    # An OPEN contract at or past expiry has an outcome the loss limits can't
    # see yet, so it blocks until reconciliation settles it. The margin covers
    # clock skew with Deriv and a contract expiring during the cycle.
    EXPIRY_MARGIN = datetime.timedelta(seconds=30)

    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path # Kept for signature compatibility if needed, but not used.
        self.max_daily_loss = -500.0
        self.cooldown_hours = 4
        self.consecutive_loss_limit = 3

        # None until read from the database: unknown is not the same as zero.
        self.daily_pnl = None
        self.consecutive_losses = None
        self.cooldown_until = None
        self.unresolved_intents = []
        self.overdue_contracts = []
        self.state_loaded_at = None
        self.load_error = None

        self.reload()

    def reload(self) -> bool:
        """Re-read risk state. On any failure the state is unavailable and
        can_trade() blocks until a later reload succeeds."""
        self.state_loaded_at = None
        try:
            self._load_state()
        except Exception as e:
            self.load_error = str(e)
            self.daily_pnl = None
            self.consecutive_losses = None
            logger.error(f"Error loading risk state from DB; trading is blocked: {e}")
            return False
        self.load_error = None
        self.state_loaded_at = datetime.datetime.now(datetime.timezone.utc)
        return True

    def _load_state(self):
        """Read state from the database. Raises on any error, and assigns
        nothing until every query has succeeded."""
        db: Session = SessionLocal()
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            today_start = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.timezone.utc)

            # Daily PNL
            daily_pnl = db.query(func.sum(PaperTrade.pnl)).filter(
                PaperTrade.timestamp >= today_start,
                PaperTrade.status == 'CLOSED'
            ).scalar() or 0.0

            # Consecutive losses
            recent_trades = db.query(PaperTrade).filter(
                PaperTrade.status == 'CLOSED'
            ).order_by(PaperTrade.timestamp.desc()).limit(self.consecutive_loss_limit).all()

            losses = 0
            latest_loss_time = None
            for trade in recent_trades:
                if trade.pnl is not None and trade.pnl < 0:
                    losses += 1
                    if latest_loss_time is None:
                        latest_loss_time = trade.timestamp
                else:
                    break

            cooldown_until = None
            if losses >= self.consecutive_loss_limit and latest_loss_time:
                cooldown_until = _as_utc(latest_loss_time) + datetime.timedelta(hours=self.cooldown_hours)

            unresolved = [row.id for row in db.query(TradeIntent.id).filter(
                TradeIntent.status.in_(UNRESOLVED_INTENT_STATUSES)
            ).all()]

            overdue = [
                trade.contract_id or f"trade {trade.id}"
                for trade in db.query(PaperTrade).filter(PaperTrade.status == 'OPEN').all()
                if _is_due(trade, now, self.EXPIRY_MARGIN)
            ]
        finally:
            db.close()

        self.daily_pnl = float(daily_pnl)
        self.consecutive_losses = losses
        self.cooldown_until = cooldown_until
        self.unresolved_intents = unresolved
        self.overdue_contracts = overdue
        logger.info(
            f"Risk state loaded via SQLAlchemy. Daily PnL: {self.daily_pnl}, Consecutive Losses: {self.consecutive_losses}, "
            f"Unresolved trade intents: {len(unresolved)}, Overdue open contracts: {len(overdue)}"
        )

    def can_trade(self) -> Dict[str, Any]:
        """Check if trading is allowed. Fails closed: unknown or stale state blocks."""
        now = datetime.datetime.now(datetime.timezone.utc)

        if self.state_loaded_at is None:
            return {
                "allowed": False,
                "reason": f"Risk state unavailable: {self.load_error or 'not loaded'}"
            }

        if now - self.state_loaded_at > self.MAX_STATE_AGE:
            return {
                "allowed": False,
                "reason": f"Risk state is stale (loaded {self.state_loaded_at.isoformat()})"
            }

        if self.unresolved_intents:
            return {
                "allowed": False,
                "reason": f"Reconciliation required: unresolved trade intents {self.unresolved_intents}"
            }

        if self.overdue_contracts:
            return {
                "allowed": False,
                "reason": f"Reconciliation required: contracts at or past expiry still OPEN {self.overdue_contracts}"
            }

        if self.cooldown_until:
            if now < self.cooldown_until:
                return {
                    "allowed": False,
                    "reason": f"Circuit breaker active until {self.cooldown_until.isoformat()}"
                }

        if self.daily_pnl <= self.max_daily_loss:
            return {
                "allowed": False,
                "reason": f"Max daily loss exceeded: {self.daily_pnl}"
            }

        return {
            "allowed": True,
            "reason": "Risk checks passed"
        }

    def record_block(self) -> None:
        """Write a risk_events row when a block starts, not on every cycle it
        stays in force: once per cooldown for the circuit breaker, once per
        UTC day for the other blocks."""
        now = datetime.datetime.now(datetime.timezone.utc)
        today_start = datetime.datetime.combine(now.date(), datetime.time.min, tzinfo=datetime.timezone.utc)
        if self.state_loaded_at is None:
            event_type, since = "RISK_STATE_UNAVAILABLE", today_start
            threshold = observed = None
        elif self.unresolved_intents or self.overdue_contracts:
            event_type, since = "RECONCILIATION_REQUIRED", today_start
            threshold, observed = 0, len(self.unresolved_intents) + len(self.overdue_contracts)
        elif self.cooldown_until and now < self.cooldown_until:
            event_type = "CIRCUIT_BREAKER"
            since = self.cooldown_until - datetime.timedelta(hours=self.cooldown_hours)
            threshold, observed = self.consecutive_loss_limit, self.consecutive_losses
        elif self.daily_pnl is not None and self.daily_pnl <= self.max_daily_loss:
            event_type = "DAILY_LOSS_LIMIT"
            since = today_start
            threshold, observed = self.max_daily_loss, self.daily_pnl
        else:
            return

        db: Session = SessionLocal()
        try:
            already_recorded = db.query(RiskEvent.id).filter(
                RiskEvent.event_type == event_type, RiskEvent.timestamp >= since
            ).first()
            if already_recorded:
                return
            db.add(RiskEvent(
                event_type=event_type,
                severity="CRITICAL",
                daily_pnl=self.daily_pnl,
                consecutive_losses=self.consecutive_losses,
                cooldown_until=self.cooldown_until,
                threshold=None if threshold is None else float(threshold),
                observed=None if observed is None else float(observed),
                message=self.can_trade()["reason"],
            ))
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record risk event: {e}")
            db.rollback()
        finally:
            db.close()

    def update_trade_result(self, pnl: float):
        """Update risk state after a trade closes."""
        self.daily_pnl = (self.daily_pnl or 0.0) + pnl
        if pnl < 0:
            self.consecutive_losses = (self.consecutive_losses or 0) + 1
            if self.consecutive_losses >= self.consecutive_loss_limit:
                self.cooldown_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=self.cooldown_hours)
                logger.warning(f"Circuit breaker triggered! Cooldown until {self.cooldown_until}")
        else:
            self.consecutive_losses = 0
