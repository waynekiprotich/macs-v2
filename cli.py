"""CLI commands for MACS."""

import click
import logging
from rich.console import Console
from rich.table import Table
from rich.logging import RichHandler

from logging.handlers import RotatingFileHandler

file_handler = RotatingFileHandler("macs.log", maxBytes=5*1024*1024, backupCount=5)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(level=logging.INFO, handlers=[RichHandler(), file_handler])
logger = logging.getLogger("macs")
console = Console()

SYMBOLS = ["OTC_DJI", "frxXAUUSD"]


@click.group()
def cli():
    """MACS — High Win Rate Trading System."""
    pass


@cli.command()
@click.option("--symbols", "-s", default=",".join(SYMBOLS), help="Comma-separated symbols")
@click.option("--no-ai", is_flag=True, help="Skip AI analysis")
@click.option("--dry-run", is_flag=True, help="Don't execute trades")
def analyze(symbols, no_ai, dry_run):
    """Run MACS pipeline once."""
    from core.pipeline import TradingPipeline

    symbol_list = [s.strip() for s in symbols.split(",")]
    pipeline = TradingPipeline(symbol_list)

    console.print(f"[bold cyan]MACS Analyze[/] — {symbol_list}")
    try:
        results = pipeline.run(execute=not dry_run)
        
        # Periodic Heartbeat Tracking (1 hour = 4 cycles of 15 min)
        import os, json
        state_file = ".heartbeat_state.json"
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            state = {"cycles": 0}
            
        state["cycles"] += 1
        
        if state["cycles"] >= 4:
            from core.notifications import send_heartbeat
            summary = ", ".join([f"{k}={v.get('action', v) if isinstance(v, dict) else v}" for k, v in results.items()])
            from datetime import datetime
            time_str = datetime.now().strftime("%H:%M")
            send_heartbeat(status=f"Last cycle at {time_str} — {summary}")
            state["cycles"] = 0
            
        with open(state_file, "w") as f:
            json.dump(state, f)
            
    except Exception as e:
        logger.error(f"Pipeline crashed: {e}")
        from core.notifications import send_heartbeat
        send_heartbeat(status=f"Crash: {e}")
        raise

    if results is None:
        results = {}

    table = Table(title="MACS Signals")
    table.add_column("Symbol", style="cyan")
    table.add_column("Action", style="bold")
    table.add_column("Confidence")
    table.add_column("TP / SL")
    table.add_column("Reason")

    for sym, res in results.items():
        if isinstance(res, str):
            # Legacy shape safety net — pipeline.run() now always returns dicts.
            action, conf, tp, sl, reason = res, 0, "-", "-", ""
        else:
            action = res.get("action", "error")
            conf = res.get("confidence", 0)
            tp = res.get("take_profit")
            sl = res.get("stop_loss")
            tp = f"{tp:.4f}" if isinstance(tp, (int, float)) else "-"
            sl = f"{sl:.4f}" if isinstance(sl, (int, float)) else "-"
            reason = res.get("reason", "")
        action_style = "green" if action == "BUY" else "red" if action == "SELL" else "yellow"
        table.add_row(sym, f"[{action_style}]{action.upper()}[/]", str(conf), f"{tp} / {sl}", reason[:60])

    console.print(table)


SYMBOL_PROXY = {
    "OTC_DJI": "^DJI",
    "frxXAUUSD": "GC=F",
}


def _run_backtest(symbol: str, interval: str, period: str, min_conditions: int):
    """Shared core: runs the SAME technical_strategy used live, bar by bar,
    simulating a fixed-payout Rise/Fall contract per signal (not a held equity
    position) so results are actually representative of what MACS trades."""
    import yfinance as yf
    from core.indicators import compute_indicators
    from core.regime import detect_regime
    from core import technical_strategy

    proxy = SYMBOL_PROXY.get(symbol, symbol)
    df = yf.Ticker(proxy).history(period=period, interval=interval)
    if df.empty:
        return None, f"No data returned for {proxy} ({period}/{interval})"

    df = compute_indicators(df)
    df = detect_regime(df)
    df = technical_strategy.prepare(df)
    if df.empty:
        return None, "No data left after indicator warm-up window"

    trades = []
    for i in range(210, len(df)):
        row = df.iloc[i]
        is_volatile = bool(row.get('Is_Volatile', False))
        sig = technical_strategy.generate_signal(row, min_conditions=min_conditions, is_volatile=is_volatile)
        if sig['signal'] not in ('BUY', 'SELL'):
            continue

        entry = row['Close']
        tp, sl = sig['take_profit'], sig['stop_loss']
        # Walk forward until TP/SL hit or a max holding window elapses
        outcome = None
        for j in range(i + 1, min(i + 40, len(df))):
            future = df.iloc[j]
            if sig['signal'] == 'BUY':
                if future['High'] >= tp:
                    outcome = ('TP', tp - entry)
                    break
                if future['Low'] <= sl:
                    outcome = ('SL', sl - entry)
                    break
            else:
                if future['Low'] <= tp:
                    outcome = ('TP', entry - tp)
                    break
                if future['High'] >= sl:
                    outcome = ('SL', entry - sl)
                    break
        if outcome is None:
            continue  # neither hit within window — excluded, not counted either way
        trades.append({
            "date": df.index[i], "signal": sig['signal'], "confidence": sig['confidence'],
            "conditions": sig['reason'], "result": outcome[0], "pnl": outcome[1],
        })

    return trades, None


@cli.command(name="equity-backtest")
@click.option("--symbol", default="OTC_DJI", help="MACS symbol (OTC_DJI, frxXAUUSD) or raw yfinance ticker")
@click.option("--days", default=59, help="Days of intraday history (yfinance caps 15m data at ~60d)")
@click.option("--interval", default="15m", help="Bar interval — match your live granularity")
@click.option("--min-conditions", default=None, type=int, help="Override MACS_MIN_CONDITIONS for this run")
@click.option("--sweep", is_flag=True, help="Test every threshold 4-8 to find the best min_conditions")
def backtest(symbol, days, interval, min_conditions, sweep):
    """Backtest the SAME technical_strategy that trades live (pure technical, no AI)."""
    from config.settings import settings

    console.print(f"[bold cyan]MACS Backtest[/] — {symbol} ({days}d @ {interval}) — proxy: {SYMBOL_PROXY.get(symbol, symbol)}")
    console.print("[dim]Simulates fixed-payout Rise/Fall style exits (first TP/SL hit within 40 bars), matching live TP=0.4xATR / SL=3xATR.[/dim]")

    thresholds = range(4, 9) if sweep else [min_conditions or settings.MACS_MIN_CONDITIONS]

    summary = Table(title=f"Backtest Results — {symbol}")
    summary.add_column("Min Conditions", style="cyan")
    summary.add_column("Trades", style="bold")
    summary.add_column("Wins")
    summary.add_column("Losses")
    summary.add_column("Win Rate")
    summary.add_column("Total P&L")

    for threshold in thresholds:
        trades, err = _run_backtest(symbol, interval, f"{days}d", threshold)
        if err:
            console.print(f"[red]{err}[/]")
            return

        wins = [t for t in trades if t['result'] == 'TP']
        losses = [t for t in trades if t['result'] == 'SL']
        total = len(wins) + len(losses)
        win_rate = (len(wins) / total * 100) if total else 0.0
        total_pnl = sum(t['pnl'] for t in trades)

        win_style = "green" if win_rate >= 55 else "red" if total else "yellow"
        pnl_style = "green" if total_pnl > 0 else "red"
        summary.add_row(
            str(threshold), str(total), str(len(wins)), str(len(losses)),
            f"[{win_style}]{win_rate:.1f}%[/]",
            f"[{pnl_style}]{total_pnl:.2f}[/]",
        )

    console.print(summary)
    console.print("[dim]Tune MACS_MIN_CONDITIONS in .env to the threshold with the best win-rate/trade-count tradeoff — higher = fewer, more selective trades.[/dim]")


@cli.command()
@click.option("--symbol", default=None, help="Filter to one symbol (default: all)")
def performance(symbol):
    """Show REAL win rate / profit factor / drawdown from closed trades in the DB."""
    from core.performance import print_report
    print_report(symbol)


@cli.command()
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8000)
def serve(host, port):
    """Start MACS API server + dashboard."""
    import uvicorn
    console.print(f"[bold cyan]MACS Server[/] — http://{host}:{port}")
    uvicorn.run("api.server:app", host=host, port=port, reload=False)


@cli.command()
@click.option("--symbols", "-s", default=",".join(SYMBOLS), help="Comma-separated symbols")
@click.option("--interval", default=15, help="Minutes between pipeline runs")
def run(symbols, interval):
    """Run MACS continuously (scheduler mode)."""
    from core.pipeline import TradingPipeline
    from core.scheduler import TradingScheduler

    symbol_list = [s.strip() for s in symbols.split(",")]
    pipeline = TradingPipeline(symbol_list)
    scheduler = TradingScheduler(pipeline, interval_minutes=interval)
    console.print(f"[bold cyan]MACS Scheduler[/] — {symbol_list} every {interval}m")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.stop()
        console.print("[yellow]MACS stopped by user[/]")


if __name__ == "__main__":
    cli()