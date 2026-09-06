"""
Real performance metrics from actual trades — not a backtest, not a guess.
This is the thing the old system never had: proof of your actual win rate.
"""
import logging
from typing import Dict, Any
from sqlalchemy import func

from models.database import SessionLocal, PaperTrade

logger = logging.getLogger(__name__)


def compute_metrics(symbol: str = None) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        query = db.query(PaperTrade).filter(PaperTrade.status == 'CLOSED')
        if symbol:
            query = query.filter(PaperTrade.symbol == symbol)
        trades = query.order_by(PaperTrade.timestamp.asc()).all()

        if not trades:
            return {"trade_count": 0, "message": "No closed trades yet — nothing to measure."}

        wins = [t for t in trades if (t.pnl or 0) > 0]
        losses = [t for t in trades if (t.pnl or 0) < 0]
        breakeven = [t for t in trades if (t.pnl or 0) == 0]

        total_trades = len(trades)
        win_rate = len(wins) / total_trades * 100 if total_trades else 0.0

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf') if gross_profit > 0 else 0.0

        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        reward_risk = (avg_win / avg_loss) if avg_loss > 0 else float('inf') if avg_win > 0 else 0.0

        net_pnl = sum(t.pnl or 0 for t in trades)

        # Max drawdown on cumulative equity curve
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            equity += (t.pnl or 0)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        # Consecutive loss streak (worst observed)
        worst_streak, streak = 0, 0
        for t in trades:
            if (t.pnl or 0) < 0:
                streak += 1
                worst_streak = max(worst_streak, streak)
            else:
                streak = 0

        # Breakdown by the confidence/condition-count the signal fired at,
        # so you can see whether higher confidence actually correlates with wins.
        by_confidence = {}
        for t in trades:
            bucket = "unknown"
            if t.confidence is not None:
                bucket = f"{int(t.confidence // 10) * 10}-{int(t.confidence // 10) * 10 + 9}%"
            by_confidence.setdefault(bucket, {"wins": 0, "losses": 0})
            if (t.pnl or 0) > 0:
                by_confidence[bucket]["wins"] += 1
            elif (t.pnl or 0) < 0:
                by_confidence[bucket]["losses"] += 1

        return {
            "trade_count": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": len(breakeven),
            "win_rate_pct": round(win_rate, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_pnl": round(net_pnl, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else "inf",
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "reward_risk_ratio": round(reward_risk, 2) if reward_risk != float('inf') else "inf",
            "max_drawdown": round(max_dd, 2),
            "worst_losing_streak": worst_streak,
            "by_confidence_bucket": by_confidence,
        }
    finally:
        db.close()


def print_report(symbol: str = None):
    from rich.console import Console
    from rich.table import Table

    console = Console()
    metrics = compute_metrics(symbol)

    if metrics["trade_count"] == 0:
        console.print(f"[yellow]{metrics['message']}[/]")
        return

    table = Table(title=f"MACS Live Performance{' — ' + symbol if symbol else ''}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold")

    wr_style = "green" if metrics["win_rate_pct"] >= 55 else "red"
    pnl_style = "green" if metrics["net_pnl"] > 0 else "red"

    table.add_row("Total Closed Trades", str(metrics["trade_count"]))
    table.add_row("Wins / Losses", f"{metrics['wins']} / {metrics['losses']}")
    table.add_row("Win Rate", f"[{wr_style}]{metrics['win_rate_pct']}%[/]")
    table.add_row("Net P&L", f"[{pnl_style}]{metrics['net_pnl']}[/]")
    table.add_row("Profit Factor", str(metrics["profit_factor"]))
    table.add_row("Avg Win / Avg Loss", f"{metrics['avg_win']} / {metrics['avg_loss']}")
    table.add_row("Reward:Risk Ratio", str(metrics["reward_risk_ratio"]))
    table.add_row("Max Drawdown", str(metrics["max_drawdown"]))
    table.add_row("Worst Losing Streak", str(metrics["worst_losing_streak"]))

    console.print(table)

    if metrics["by_confidence_bucket"]:
        conf_table = Table(title="Win Rate by Confidence Bucket")
        conf_table.add_column("Confidence")
        conf_table.add_column("Wins")
        conf_table.add_column("Losses")
        conf_table.add_column("Win Rate")
        for bucket, stats in sorted(metrics["by_confidence_bucket"].items()):
            total = stats["wins"] + stats["losses"]
            wr = (stats["wins"] / total * 100) if total else 0
            conf_table.add_row(bucket, str(stats["wins"]), str(stats["losses"]), f"{wr:.1f}%")
        console.print(conf_table)
        console.print("[dim]If higher confidence buckets don't show higher win rates, the condition-count threshold isn't actually predictive — that's a signal to rework it, not just retune the number.[/dim]")
