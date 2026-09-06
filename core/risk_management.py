import datetime
import logging
from typing import Dict, Any
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import SessionLocal, PaperTrade

logger = logging.getLogger(__name__)

class RiskManager:
    def __init__(self, db_path: str = "trading.db"):
        self.db_path = db_path # Kept for signature compatibility if needed, but not used.
        self.max_daily_loss = -500.0
        self.cooldown_hours = 4
        self.consecutive_loss_limit = 3
        
        self.daily_pnl = 0.0
        self.consecutive_losses = 0
        self.cooldown_until = None
        
        self._load_state()

    def _load_state(self):
        """Read state from SQLite DB PaperTrade table on init to survive restarts."""
        db: Session = SessionLocal()
        try:
            today = datetime.datetime.now(datetime.timezone.utc).date()
            today_start = datetime.datetime.combine(today, datetime.time.min, tzinfo=datetime.timezone.utc)
            
            # Daily PNL
            result = db.query(func.sum(PaperTrade.pnl)).filter(
                PaperTrade.timestamp >= today_start,
                PaperTrade.status == 'CLOSED'
            ).scalar()
            self.daily_pnl = result if result else 0.0
            
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
                    
            self.consecutive_losses = losses
            
            if self.consecutive_losses >= self.consecutive_loss_limit and latest_loss_time:
                # Ensure latest_loss_time is timezone-aware
                if latest_loss_time.tzinfo is None:
                    latest_loss_time = latest_loss_time.replace(tzinfo=datetime.timezone.utc)
                self.cooldown_until = latest_loss_time + datetime.timedelta(hours=self.cooldown_hours)
                
            logger.info(f"Risk state loaded via SQLAlchemy. Daily PnL: {self.daily_pnl}, Consecutive Losses: {self.consecutive_losses}")
        except Exception as e:
            logger.error(f"Error loading state from DB: {e}")
        finally:
            db.close()

    def can_trade(self) -> Dict[str, Any]:
        """Check if trading is allowed based on risk parameters."""
        now = datetime.datetime.now(datetime.timezone.utc)
        
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
        
    def update_trade_result(self, pnl: float):
        """Update risk state after a trade closes."""
        self.daily_pnl += pnl
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.consecutive_loss_limit:
                self.cooldown_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=self.cooldown_hours)
                logger.warning(f"Circuit breaker triggered! Cooldown until {self.cooldown_until}")
        else:
            self.consecutive_losses = 0
