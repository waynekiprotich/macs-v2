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


def _fetch_backtest_data(symbol: str, interval: str, period: str):
    """Fetch + prepare indicators ONCE. Reused across every threshold in a
    --sweep so we don't hammer yfinance with N identical requests (that's
    the fastest way to get rate-limited) and each threshold is compared
    against the exact same bars, not a fresh fetch that could differ."""
    import time
    import yfinance as yf
    from yfinance.exceptions import YFRateLimitError
    from core.indicators import compute_indicators
    from core.regime import detect_regime
    from core import technical_strategy

    proxy = SYMBOL_PROXY.get(symbol, symbol)

    df = None
    last_err = None
    for attempt in range(3):
        try:
            df = yf.Ticker(proxy).history(period=period, interval=interval)
            break
        except YFRateLimitError as e:
            last_err = e
            if attempt < 2:
                wait = 15 * (attempt + 1)
                console.print(f"[yellow]Rate limited by Yahoo Finance, waiting {wait}s and retrying ({attempt + 1}/3)...[/]")
                time.sleep(wait)
        except Exception as e:
            return None, f"yfinance fetch failed: {e}"

    if df is None:
        return None, (
            f"Still rate-limited by Yahoo Finance after 3 attempts for {proxy}. "
            "This is Yahoo throttling your IP, not a code bug — wait a few minutes "
            "and try again, ideally with a single --min-conditions run instead of --sweep."
        )
    if df.empty:
        return None, f"No data returned for {proxy} ({period}/{interval})"

    df = compute_indicators(df)
    df = detect_regime(df)
    df = technical_strategy.prepare(df)
    if df.empty:
        return None, "No data left after indicator warm-up window"

    return df, None


def _simulate(df, min_conditions: int, tp_multiplier: float = 0.4, sl_multiplier: float = 3.0):
    """Pure computation over already-fetched data — runs the SAME
    technical_strategy used live, bar by bar, simulating a fixed-payout
    Rise/Fall contract per signal (not a held equity position) so results
    are actually representative of what MACS trades."""
    from core import technical_strategy

    trades = []
    for i in range(210, len(df)):
        row = df.iloc[i]
        is_volatile = bool(row.get('Is_Volatile', False))
        sig = technical_strategy.generate_signal(
            row, min_conditions=min_conditions, is_volatile=is_volatile,
            tp_multiplier=tp_multiplier, sl_multiplier=sl_multiplier,
        )
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

    return trades


TP_GRID = [0.4, 0.8, 1.2, 1.6, 2.0]
SL_GRID = [0.4, 0.8, 1.2, 1.6, 2.0, 3.0]
BASELINE_TP, BASELINE_SL = 0.4, 3.0


def _trade_stats(trades):
    wins = [t for t in trades if t['result'] == 'TP']
    losses = [t for t in trades if t['result'] == 'SL']
    total = len(wins) + len(losses)
    win_rate = (len(wins) / total * 100) if total else 0.0
    total_pnl = sum(t['pnl'] for t in trades)
    return wins, losses, total, win_rate, total_pnl


@cli.command(name="equity-backtest")
@click.option("--symbol", default="OTC_DJI", help="MACS symbol (OTC_DJI, frxXAUUSD) or raw yfinance ticker")
@click.option("--days", default=59, help="Days of intraday history (yfinance caps 15m data at ~60d)")
@click.option("--interval", default="15m", help="Bar interval — match your live granularity")
@click.option("--min-conditions", default=None, type=int, help="Override MACS_MIN_CONDITIONS for this run")
@click.option("--sweep", is_flag=True, help="Test every condition threshold 4-8 to find the best min_conditions")
@click.option("--tpsl-sweep", is_flag=True, help="Test a grid of TP/SL multipliers (at the chosen min-conditions) to find the best payout ratio")
def backtest(symbol, days, interval, min_conditions, sweep, tpsl_sweep):
    """Backtest the SAME technical_strategy that trades live (pure technical, no AI)."""
    from config.settings import settings

    fixed_threshold = min_conditions or settings.MACS_MIN_CONDITIONS
    console.print(f"[bold cyan]MACS Backtest[/] — {symbol} ({days}d @ {interval}) — proxy: {SYMBOL_PROXY.get(symbol, symbol)}")
    console.print("[dim]Simulates fixed-payout Rise/Fall style exits (first TP/SL hit within 40 bars).[/dim]")

    df, err = _fetch_backtest_data(symbol, interval, f"{days}d")
    if err:
        console.print(f"[red]{err}[/]")
        return

    if sweep:
        summary = Table(title=f"Condition Threshold Sweep — {symbol} (TP={BASELINE_TP}x/SL={BASELINE_SL}x ATR)")
        summary.add_column("Min Conditions", style="cyan")
        summary.add_column("Trades", style="bold")
        summary.add_column("Wins")
        summary.add_column("Losses")
        summary.add_column("Win Rate")
        summary.add_column("Total P&L")

        for threshold in range(4, 9):
            trades = _simulate(df, threshold, BASELINE_TP, BASELINE_SL)
            wins, losses, total, win_rate, total_pnl = _trade_stats(trades)
            win_style = "green" if win_rate >= 55 else "red" if total else "yellow"
            pnl_style = "green" if total_pnl > 0 else "red"
            summary.add_row(
                str(threshold), str(total), str(len(wins)), str(len(losses)),
                f"[{win_style}]{win_rate:.1f}%[/]",
                f"[{pnl_style}]{total_pnl:.2f}[/]",
            )
        console.print(summary)
        console.print("[dim]Tune MACS_MIN_CONDITIONS in .env to the threshold with the best win-rate/trade-count tradeoff.[/dim]")

    if tpsl_sweep:
        results = []
        for tp in TP_GRID:
            for sl in SL_GRID:
                trades = _simulate(df, fixed_threshold, tp, sl)
                wins, losses, total, win_rate, total_pnl = _trade_stats(trades)
                expectancy = (total_pnl / total) if total else 0.0
                results.append((tp, sl, total, len(wins), len(losses), win_rate, total_pnl, expectancy))

        # Always show the current live baseline, then the best-by-total-P&L combos.
        baseline = next((r for r in results if r[0] == BASELINE_TP and r[1] == BASELINE_SL), None)
        ranked = sorted(results, key=lambda r: r[6], reverse=True)

        grid = Table(title=f"TP/SL Multiplier Sweep — {symbol} (fixed at {fixed_threshold}/8 conditions)")
        grid.add_column("TP (xATR)", style="cyan")
        grid.add_column("SL (xATR)", style="cyan")
        grid.add_column("Ratio TP:SL")
        grid.add_column("Trades")
        grid.add_column("Win Rate")
        grid.add_column("Total P&L")
        grid.add_column("Exp/Trade")

        def _add_row(r, tag=""):
            tp, sl, total, w, l, win_rate, total_pnl, expectancy = r
            pnl_style = "green" if total_pnl > 0 else "red" if total else "yellow"
            grid.add_row(
                f"{tp:.1f}{tag}", f"{sl:.1f}", f"1:{sl/tp:.1f}", str(total),
                f"{win_rate:.1f}%", f"[{pnl_style}]{total_pnl:.2f}[/]", f"{expectancy:.4f}",
            )

        if baseline:
            _add_row(baseline, tag=" (current live)")
        for r in ranked[:10]:
            if baseline and r[0] == baseline[0] and r[1] == baseline[1]:
                continue
            _add_row(r)

        console.print(grid)
        console.print(f"[dim]Fixed at {fixed_threshold}/8 conditions (pass --min-conditions to test a different threshold). "
                       f"Ranked by total P&L, current live baseline (0.4/3.0) always shown first for comparison.[/dim]")


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