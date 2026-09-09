import logging
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import List

from models.schemas import Signal, Risk, Position
from models.database import get_db, PaperTrade, SystemLog

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/signals", response_model=List[Signal])
def get_signals(limit: int = 20, db: Session = Depends(get_db)):
    """Recent signals from system_logs — every pipeline cycle writes one,
    whether or not it resulted in a trade."""
    try:
        logs = db.query(SystemLog).order_by(SystemLog.timestamp.desc()).limit(limit).all()
        return [
            Signal(
                symbol=log.symbol,
                action=log.signal,
                # SystemLog stores confidence 0-100; the schema wants 0-1.
                confidence_score=min(1.0, max(0.0, (log.combined_confidence or 0) / 100)),
                reasoning=log.error_warning or f"regime={log.regime}, volatile={bool(log.is_volatile)}",
                timestamp=log.timestamp,
            )
            for log in logs
        ]
    except Exception as e:
        logger.error(f"Error fetching signals: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/risk", response_model=Risk)
def get_risk(symbol: str = "ALL"):
    """Live risk state from RiskManager — previously returned hardcoded
    'NORMAL' constants that never reflected the actual circuit breaker."""
    try:
        from core.risk_management import RiskManager
        rm = RiskManager()
        status = rm.can_trade()
        warnings = [] if status['allowed'] else [status['reason']]
        if rm.consecutive_losses:
            warnings.append(f"{rm.consecutive_losses} consecutive loss(es)")
        return Risk(
            symbol=symbol,
            risk_level="BLOCKED" if not status['allowed'] else (
                "ELEVATED" if rm.consecutive_losses else "NORMAL"
            ),
            max_position_size=170.0,  # matches the hardcoded stake in pipeline.execute_trade
            warnings=warnings,
        )
    except Exception as e:
        logger.error(f"Error fetching risk: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/trades")
def get_trades(db: Session = Depends(get_db)):
    try:
        trades = db.query(PaperTrade).order_by(PaperTrade.timestamp.desc()).limit(100).all()
        return trades
    except Exception as e:
        logger.error(f"Error fetching trades: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/positions", response_model=List[Position])
def get_positions(db: Session = Depends(get_db)):
    """Open (unreconciled) contracts.

    Deriv CALL/PUT binaries have no running mark-to-market — they're worth
    either the payout or nothing, decided at expiry — so unrealized P&L is
    not meaningful here and is reported as 0 rather than invented.
    """
    try:
        open_trades = db.query(PaperTrade).filter(PaperTrade.status == "OPEN").all()
        return [
            Position(
                symbol=t.symbol,
                quantity=t.quantity,
                avg_entry_price=t.price,
                current_price=t.price,
                unrealized_pl=0.0,
                unrealized_pl_pc=0.0,
            )
            for t in open_trades
        ]
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/performance")
def get_performance(symbol: str = None):
    """Delegates to core.performance — the single source of truth.

    This endpoint previously reimplemented the math and got it wrong: it
    divided wins by ALL trades including still-open ones, understating the
    win rate, and it ignored the OPEN/CLOSED status entirely.
    """
    try:
        from core.performance import compute_metrics
        return compute_metrics(symbol)
    except Exception as e:
        logger.error(f"Error fetching performance: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
