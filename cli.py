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


CACHE_DIR = ".backtest_cache"
CACHE_MAX_AGE_HOURS = 12


def _fetch_backtest_data(symbol: str, interval: str, period: str, refresh: bool = False):
    """Fetch + prepare indicators ONCE, with an on-disk cache.

    Yahoo rate-limits aggressively, and a --sweep or --walk-forward re-reads
    the same bars many times. Caching to disk means one fetch serves every
    subsequent analysis of the same symbol/interval/period, which is both
    faster and the difference between being able to iterate at all and
    spending the session waiting out throttles. Pass --refresh to force a
    re-fetch; cache expires after CACHE_MAX_AGE_HOURS anyway.
    """
    import os
    import time
    import pandas as pd
    from core.indicators import compute_indicators
    from core.regime import detect_regime
    from core import technical_strategy

    proxy = SYMBOL_PROXY.get(symbol, symbol)
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{proxy.replace('^','').replace('=','_')}_{interval}_{period}.pkl")

    df = None
    if not refresh and os.path.exists(cache_path):
        age_hours = (time.time() - os.path.getmtime(cache_path)) / 3600
        if age_hours < CACHE_MAX_AGE_HOURS:
            try:
                df = pd.read_pickle(cache_path)
                console.print(f"[dim]Using cached data ({age_hours:.1f}h old, {len(df)} raw bars). --refresh to re-fetch.[/dim]")
            except Exception:
                df = None

    if df is None:
        import yfinance as yf
        from yfinance.exceptions import YFRateLimitError
        for attempt in range(3):
            try:
                df = yf.Ticker(proxy).history(period=period, interval=interval)
                break
            except YFRateLimitError:
                if attempt < 2:
                    wait = 15 * (attempt + 1)
                    console.print(f"[yellow]Rate limited by Yahoo Finance, waiting {wait}s and retrying ({attempt + 1}/3)...[/]")
                    time.sleep(wait)
                else:
                    df = None
            except Exception as e:
                return None, f"yfinance fetch failed: {e}"

        if df is None:
            return None, (
                f"Still rate-limited by Yahoo Finance after 3 attempts for {proxy}, and no usable cache. "
                "Wait a few minutes and retry — once one fetch succeeds it's cached for "
                f"{CACHE_MAX_AGE_HOURS}h and every later sweep reuses it."
            )
        if not df.empty:
            try:
                df.to_pickle(cache_path)
            except Exception as e:
                logger.warning(f"Could not write backtest cache: {e}")

    if df.empty:
        return None, f"No data returned for {proxy} ({period}/{interval})"

    df = compute_indicators(df)
    df = detect_regime(df)
    df = technical_strategy.prepare(df)
    if df.empty:
        return None, "No data left after indicator warm-up window"

    return df, None


def _interval_to_minutes(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 1, "h": 60, "d": 1440}.get(unit, 1)


def _simulate(df, min_conditions: int, payout_pct: float = 0.85, expiry_bars: int = 1):
    """
    Models the REAL contract MACS buys, confirmed against execution/deriv_engine.py:
    a fixed-duration CALL (BUY) / PUT (SELL) binary option — duration=15,
    duration_unit='m', no barrier of any kind. It settles exactly once, at
    expiry, purely on direction:
      - correct direction -> +payout_pct * stake
      - wrong direction   -> -1.0 * stake (entire stake lost)

    take_profit/stop_loss from technical_strategy.generate_signal() are NOT
    used here and are not sent to Deriv by execute_signal() either — they're
    diagnostic-only. An earlier version of this backtest simulated a TP/SL
    barrier walk that doesn't correspond to any contract this system actually
    buys; that model is gone. `expiry_bars` must equal how many bars of the
    fetched interval make up the real 15-minute contract duration (1 bar at
    interval=15m, which is why that's the required default).
    """
    from core import technical_strategy

    trades = []
    for i in range(210, len(df) - expiry_bars):
        row = df.iloc[i]
        is_volatile = bool(row.get('Is_Volatile', False))
        sig = technical_strategy.generate_signal(row, min_conditions=min_conditions, is_volatile=is_volatile)
        if sig['signal'] not in ('BUY', 'SELL'):
            continue

        entry = row['Close']
        exit_price = df.iloc[i + expiry_bars]['Close']
        won = (exit_price > entry) if sig['signal'] == 'BUY' else (exit_price < entry)
        pnl = payout_pct if won else -1.0
        trades.append({
            "date": df.index[i], "signal": sig['signal'], "confidence": sig['confidence'],
            "conditions": sig['reason'], "result": "WIN" if won else "LOSS", "pnl": pnl,
        })

    return trades


def _trade_stats(trades):
    wins = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total = len(wins) + len(losses)
    win_rate = (len(wins) / total * 100) if total else 0.0
    total_pnl = sum(t['pnl'] for t in trades)
    return wins, losses, total, win_rate, total_pnl


def _baseline_win_rate(df, expiry_bars: int, direction: str = 'BUY') -> float:
    """
    Control group: bet the SAME direction on every single bar, no strategy at all.

    This is the bar that actually matters. If the underlying drifts upward over
    the sample, "always CALL" wins >50% with precisely zero skill — so a
    strategy scoring 52% isn't finding signal, it's just riding drift with extra
    steps. Any honest claim of edge has to beat this number, not 50%.
    """
    wins = 0
    total = 0
    for i in range(210, len(df) - expiry_bars):
        entry = df.iloc[i]['Close']
        exit_price = df.iloc[i + expiry_bars]['Close']
        won = (exit_price > entry) if direction == 'BUY' else (exit_price < entry)
        wins += 1 if won else 0
        total += 1
    return (wins / total * 100) if total else 0.0


# (bars at 15m interval, human label)
DURATION_GRID = [(1, "15m"), (2, "30m"), (4, "1h"), (8, "2h"), (16, "4h"), (32, "8h"), (96, "24h")]


def _walk_forward(df, payout_pct: float, expiry_bars: int, train_frac: float = 0.7):
    """
    The honest test: pick the best threshold on the FIRST chunk of history,
    then measure that same threshold on data it has never seen.

    Every other number this tool reports is in-sample — the threshold was
    chosen by looking at the same bars it's scored on, which guarantees the
    winner looks good whether or not it has any predictive power. Picking on
    train and scoring on test is what separates a real effect from having
    selected the luckiest noise out of five candidates.

    Returns (train_rows, test_rows, chosen_threshold, split_index).
    """
    split = int(len(df) * train_frac)
    train_df, test_df = df.iloc[:split], df.iloc[split:]

    train_rows, test_rows = [], []
    best_threshold, best_expectancy = None, None

    for threshold in range(4, 9):
        trades = _simulate(train_df, threshold, payout_pct, expiry_bars)
        _, _, total, win_rate, total_pnl = _trade_stats(trades)
        expectancy = (total_pnl / total) if total else 0.0
        train_rows.append((threshold, total, win_rate, total_pnl, expectancy))
        # Require a minimum sample so a 2-trade fluke can't win the selection.
        if total >= 30 and (best_expectancy is None or expectancy > best_expectancy):
            best_threshold, best_expectancy = threshold, expectancy

    for threshold in range(4, 9):
        trades = _simulate(test_df, threshold, payout_pct, expiry_bars)
        _, _, total, win_rate, total_pnl = _trade_stats(trades)
        expectancy = (total_pnl / total) if total else 0.0
        test_rows.append((threshold, total, win_rate, total_pnl, expectancy))

    return train_rows, test_rows, best_threshold, split


@cli.command(name="equity-backtest")
@click.option("--symbol", default="OTC_DJI", help="MACS symbol (OTC_DJI, frxXAUUSD) or raw yfinance ticker")
@click.option("--days", default=59, help="Days of intraday history (yfinance caps 15m data at ~60d)")
@click.option("--interval", default="15m", help="Bar interval — MUST be 15m unless you also adjust the live contract duration; this is what the fixed-duration payout math is built around")
@click.option("--min-conditions", default=None, type=int, help="Override MACS_MIN_CONDITIONS for this run")
@click.option("--payout", default=0.85, type=float, help="Assumed Deriv payout ratio for a correct call (e.g. 0.85 = 85%% return on win). Check the real proposal payout in your Deriv app/logs — it varies by symbol and market conditions and isn't something this strategy controls.")
@click.option("--sweep", is_flag=True, help="Test every condition threshold 4-8 to find the best min_conditions")
@click.option("--duration-sweep", is_flag=True, help="Test contract durations 15m-24h at the chosen threshold — does the strategy have ANY edge at a longer horizon?")
@click.option("--walk-forward", is_flag=True, help="Pick the best threshold on the first 70%% of history, then score it on the unseen last 30%% — the only result here that isn't in-sample")
@click.option("--refresh", is_flag=True, help="Force a re-fetch instead of using cached data")
def backtest(symbol, days, interval, min_conditions, payout, sweep, duration_sweep, walk_forward, refresh):
    """Backtest the fixed-duration CALL/PUT binary contract MACS actually buys (pure technical, no AI, no TP/SL — there is none in this product)."""
    from config.settings import settings

    fixed_threshold = min_conditions or settings.MACS_MIN_CONDITIONS
    expiry_bars = max(1, round(15 / _interval_to_minutes(interval)))
    breakeven = 1 / (1 + payout) * 100

    console.print(f"[bold cyan]MACS Backtest[/] — {symbol} ({days}d @ {interval}) — proxy: {SYMBOL_PROXY.get(symbol, symbol)}")
    console.print(f"[dim]Fixed 15-min CALL/PUT binary, settled purely on direction at expiry. Assumed payout: {payout*100:.0f}% "
                  f"(breakeven win rate: {breakeven:.1f}%). Real payout varies — check your Deriv proposal logs.[/dim]")
    if interval != "15m":
        console.print(f"[yellow]Warning: --interval {interval} means {expiry_bars} bar(s) approximate a 15-min expiry — for an exact match use --interval 15m.[/]")

    df, err = _fetch_backtest_data(symbol, interval, f"{days}d", refresh=refresh)
    if err:
        console.print(f"[red]{err}[/]")
        return

    if walk_forward:
        train_rows, test_rows, chosen, split = _walk_forward(df, payout, expiry_bars)

        wf = Table(title=f"Walk-Forward — {symbol} (train: first {split} bars, test: last {len(df)-split} unseen bars)")
        wf.add_column("Min Conditions", style="cyan")
        wf.add_column("Train Trades")
        wf.add_column("Train Win%")
        wf.add_column("Train Exp/Trade")
        wf.add_column("Test Trades")
        wf.add_column("Test Win%")
        wf.add_column("Test Exp/Trade")

        test_by_threshold = {r[0]: r for r in test_rows}
        for threshold, tot, wr, pnl, exp in train_rows:
            t_thr, t_tot, t_wr, t_pnl, t_exp = test_by_threshold[threshold]
            marker = " ← picked" if threshold == chosen else ""
            tr_style = "green" if exp > 0 else "red"
            te_style = "green" if t_exp > 0 else "red"
            wf.add_row(
                f"{threshold}{marker}", str(tot), f"{wr:.1f}%",
                f"[{tr_style}]{exp:+.4f}[/]",
                str(t_tot), f"{t_wr:.1f}%",
                f"[{te_style}]{t_exp:+.4f}[/]",
            )
        console.print(wf)

        if chosen is None:
            console.print("[yellow]No threshold produced enough training trades (min 30) to select from.[/]")
        else:
            picked_test = test_by_threshold[chosen]
            verdict_style = "green" if picked_test[4] > 0 else "red"
            console.print(
                f"[bold]Out-of-sample result:[/] threshold {chosen}/8 was best on training data, and on unseen data "
                f"it scored [{verdict_style}]{picked_test[2]:.1f}% win rate, {picked_test[4]:+.4f} expectancy/trade[/] "
                f"over {picked_test[1]} trades (breakeven needs {breakeven:.1f}%)."
            )
            console.print("[dim]If the picked threshold's test expectancy is negative while its train expectancy was "
                          "positive, the training result was selection noise, not signal. That's the failure mode this "
                          "whole split exists to catch.[/dim]")
        return

    if duration_sweep:
        grid = Table(title=f"Contract Duration Sweep — {symbol} (at {fixed_threshold}/8 conditions, {payout*100:.0f}% payout)")
        grid.add_column("Duration", style="cyan")
        grid.add_column("Trades", style="bold")
        grid.add_column("Win Rate")
        grid.add_column("Always-CALL baseline")
        grid.add_column("Edge vs baseline")
        grid.add_column("Expectancy/Trade")

        for bars, label in DURATION_GRID:
            if len(df) - bars <= 210:
                continue
            trades = _simulate(df, fixed_threshold, payout, bars)
            _, _, total, win_rate, total_pnl = _trade_stats(trades)
            expectancy = (total_pnl / total) if total else 0.0
            baseline = _baseline_win_rate(df, bars, 'BUY')
            edge = win_rate - baseline
            win_style = "green" if win_rate >= breakeven else "red" if total else "yellow"
            edge_style = "green" if edge > 0 else "red"
            grid.add_row(
                label, str(total),
                f"[{win_style}]{win_rate:.1f}%[/]",
                f"{baseline:.1f}%",
                f"[{edge_style}]{edge:+.1f} pts[/]",
                f"{expectancy:.4f}",
            )
        console.print(grid)
        console.print(f"[dim]'Always-CALL baseline' = betting CALL on every single bar with no strategy at all. If the "
                      f"strategy's win rate doesn't clearly beat that column, it has no signal — it's riding drift. "
                      f"And it still needs to clear {breakeven:.1f}% in absolute terms to make money at this payout.[/dim]")
        return

    thresholds = range(4, 9) if sweep else [fixed_threshold]

    summary = Table(title=f"Backtest Results — {symbol} (fixed 15-min CALL/PUT, {payout*100:.0f}% payout)")
    summary.add_column("Min Conditions", style="cyan")
    summary.add_column("Trades", style="bold")
    summary.add_column("Wins")
    summary.add_column("Losses")
    summary.add_column("Win Rate")
    summary.add_column("Baseline")
    summary.add_column("Total P&L (stakes)")
    summary.add_column("Expectancy/Trade")

    baseline = _baseline_win_rate(df, expiry_bars, 'BUY')

    for threshold in thresholds:
        trades = _simulate(df, threshold, payout, expiry_bars)
        wins, losses, total, win_rate, total_pnl = _trade_stats(trades)
        expectancy = (total_pnl / total) if total else 0.0
        win_style = "green" if win_rate >= breakeven else "red" if total else "yellow"
        pnl_style = "green" if total_pnl > 0 else "red"
        summary.add_row(
            str(threshold), str(total), str(len(wins)), str(len(losses)),
            f"[{win_style}]{win_rate:.1f}%[/]",
            f"{baseline:.1f}%",
            f"[{pnl_style}]{total_pnl:.2f}[/]",
            f"{expectancy:.4f}",
        )
    console.print(summary)
    console.print(f"[dim]P&L is in units of stake (1.0 = one full stake). Win rate must clear {breakeven:.1f}% to be "
                  f"profitable at this payout — that's the real bar, not 50%. 'Baseline' is what betting CALL on every "
                  f"bar scores with no strategy at all; beating {breakeven:.1f}% while barely matching the baseline "
                  f"means you're riding drift, not predicting.[/dim]")


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