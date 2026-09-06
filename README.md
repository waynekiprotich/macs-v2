# MACS-V2

A trading bot for Deriv synthetic/forex Rise-Fall options. Pulls candles, scores them against 8 technical conditions, and fires a CALL or PUT if enough conditions agree.

**Status: no demonstrated edge.** The engineering works end-to-end. The strategy itself does not beat the house edge. Read "What actually needs to change" before running this with anything you care about.

## How it works

```
Deriv candles → indicators → regime → technical_strategy (8 conditions) → risk check → execute CALL/PUT
```

- **`core/data_deriv.py`** — pulls 15-min candles from Deriv's API for the demo account. No real volume on this feed (mocked to a constant).
- **`core/indicators.py`** — SMA/EMA/MACD/RSI/Stochastic/Bollinger/ATR via the `ta` library.
- **`core/regime.py`** — labels each bar bullish/bearish/neutral off SMA50/SMA200, flags high-volatility bars.
- **`core/technical_strategy.py`** — the actual decision logic. Checks 8 symmetric conditions (regime, EMA trend, RSI zone, stochastic, MACD, support/resistance touch-count, Bollinger position, candle strength). If `min_conditions` (default 6/8) agree in one direction, it signals BUY or SELL. **No AI is involved** — an earlier version blended in a Gemini API call; it failed on most cycles and was removed.
- **`core/pipeline.py`** — orchestrates the above, logs every cycle to `system_logs`, and calls execution on a BUY/SELL.
- **`execution/deriv_engine.py`** — buys a Deriv contract: `CALL` (BUY) or `PUT` (SELL), **fixed 15-minute duration, no barrier**. It settles once, at expiry, purely on direction — correct pays a fixed percentage, wrong loses the full stake. **`take_profit`/`stop_loss` computed by the strategy are logged for diagnostics only — they are never sent to Deriv and don't affect the contract.**
- **`core/risk_management.py`** — daily loss limit + consecutive-loss cooldown, checked before every trade.
- **`core/performance.py`** — real win rate / profit factor / drawdown from actually-closed trades in the DB.

## How to run it

```bash
source venv/bin/activate
pip install -r requirements.txt        # pinned versions matter — see comments in the file
```

**One-off signal check (no trading):**
```bash
python cli.py analyze --dry-run
```

**Continuous unattended trading** (runs as a macOS LaunchAgent, survives sleep/reboot):
```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.macsv2.daemon.plist
launchctl list | grep macsv2                    # confirm it's running
tail -f macs_daemon.log                         # watch it live
launchctl bootout gui/$(id -u)/com.macsv2.daemon   # stop it
```

**Check real performance** (once trades have closed):
```bash
python cli.py performance
```

**Backtest** (models the real fixed-duration binary contract, not a made-up TP/SL barrier):
```bash
python cli.py equity-backtest --symbol OTC_DJI --sweep            # every condition threshold
python cli.py equity-backtest --symbol OTC_DJI --duration-sweep   # 15m-24h contract durations
python cli.py equity-backtest --symbol OTC_DJI --walk-forward     # out-of-sample: the honest one
python cli.py equity-backtest --symbol OTC_DJI --slices           # by hour / volatility / regime
python cli.py equity-backtest --symbol OTC_DJI --refresh          # bust the 12h data cache
```

Reading these honestly matters more than running them:

- **`--walk-forward` is the only non-in-sample result here.** It picks the best threshold on the first 70% of history and scores it on the last 30%. Everything else chooses a threshold by looking at the same bars it then grades, which flatters whichever candidate got luckiest.
- **The "always-CALL baseline" column** is betting one direction on every bar with no strategy at all. If the underlying drifted up, that scores >50% with zero skill. Beat *that*, not 50%.
- **`--slices` generates hypotheses, not results.** Run on synthetic random data with no edge by construction, it still surfaces hours at ~62% win rate. Cutting data 20 ways guarantees a winner by luck. Anything it finds must survive `--walk-forward` on data it wasn't discovered in.
- **Breakeven is `1/(1+payout)`**, ~54.1% at an 85% payout — not 50%. `python cli.py performance` now reports your *real* payout ratio from Deriv's own quotes and whether live results clear it.

`.env` needs `DERIV_API_TOKEN`, `DERIV_APP_ID` (demo account only — hardcoded in `data_deriv.py`), and `DATABASE_URL=sqlite:///macs.db`.

**Never hardcode credentials in scripts.** A live API token was once committed here in two scratch files and pushed to GitHub, bypassing the gitignored `.env` entirely. There's a guard against a repeat:
```bash
ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
```

## What actually needs to change

**The core problem isn't a bug — it's the strategy's premise.** Backtested against the real contract (fixed 15-min direction bet, ~85% payout, so you need >54.1% accuracy just to break even):

- Win rate across every condition threshold (4-8) and every contract duration tested (15m-24h): **43-53%**, never clearing breakeven
- Edge over the always-CALL baseline: flips sign between adjacent durations that should behave similarly — the signature of noise, not a real effect
- Expectancy is negative everywhere it was measured

Lagging trend indicators (SMA/EMA/MACD/RSI/Stochastic) describe where price *has been*. At a 15-minute horizon they don't carry enough information to beat a ~5.4% house edge — and this feed doesn't even have real volume to add.

**Options, in order of honesty:**

1. **Stop trading it as-is.** No amount of threshold or duration tuning fixes negative expectancy — tuning on the same data just finds the luckiest noise.
2. **Change the inputs, not the parameters.** A real edge at this horizon would need something the current features don't have — order flow, volatility clustering, time-of-day effects. This is a research project with a real chance of finding nothing, not a weekend fix.
3. **Change the horizon.** Trend indicators have more documented value at longer horizons (hours/days) than 15 minutes. Already tested up to 24h with no consistent edge, but a different asset class or a swing-trade structure (not a fixed-expiry binary) is a different, untested question.
4. **Treat it as a learning project**, not an income source. The framework built here (`equity-backtest`, `performance`, the daemon) will honestly tell you the truth about any future idea in minutes — that's the reusable part.

See the full backtest output and reasoning in the conversation history — this file is the short version.
