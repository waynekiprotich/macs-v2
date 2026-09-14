from core.performance import compute_metrics
from models.database import PaperTrade, SessionLocal, init_db

SYMBOL = "TEST_PERF"


def test_payout_ratio_ignores_loss_payouts_zeroed_by_reconcile():
    init_db()
    db = SessionLocal()
    try:
        db.add_all([
            # Legacy win: payout holds the quote.
            PaperTrade(symbol=SYMBOL, side="BUY", quantity=170.0, price=170.0, status="CLOSED",
                       result="WON", pnl=136.0, payout=306.0),
            # Legacy loss: reconcile overwrote payout with 0, so there is no quote.
            PaperTrade(symbol=SYMBOL, side="BUY", quantity=170.0, price=170.0, status="CLOSED",
                       result="LOST", pnl=-170.0, payout=0.0),
            # New loss: quoted_payout survives reconcile.
            PaperTrade(symbol=SYMBOL, side="SELL", quantity=170.0, price=170.0, status="CLOSED",
                       result="LOST", pnl=-170.0, payout=0.0, quoted_payout=306.0),
        ])
        db.commit()

        assert compute_metrics(SYMBOL)["avg_payout_ratio"] == 0.8
    finally:
        # Other tests share this database and RiskManager sums today's losses.
        db.query(PaperTrade).filter_by(symbol=SYMBOL).delete()
        db.commit()
        db.close()
