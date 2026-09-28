from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class Signal(BaseModel):
    symbol: str
    action: str = Field(..., description="BUY, SELL, or HOLD")
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    timestamp: Optional[datetime] = None
    agreement_score: float = Field(default=0, ge=0, le=100)
    action_taken: Optional[str] = None
    price: Optional[float] = None

class Risk(BaseModel):
    symbol: str
    risk_level: str
    max_position_size: Optional[float] = None
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    warnings: List[str] = []

class Trade(BaseModel):
    symbol: str
    side: str
    quantity: float
    price: float
    status: str
    order_id: Optional[str] = None
    reason: Optional[str] = None
    timestamp: Optional[datetime] = None

class Position(BaseModel):
    symbol: str
    quantity: float
    avg_entry_price: float
    current_price: float
    unrealized_pl: float
    unrealized_pl_pc: float

class ExecuteRequest(BaseModel):
    symbol: str
    action: str
    quantity: float
    reason: Optional[str] = None

class ExecuteResponse(BaseModel):
    status: str
    message: str
    trade: Optional[Trade] = None
