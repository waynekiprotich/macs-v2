# MACS profitability research plan

Prepared September 22, 2026. Goal: improve demo-account profitability through measured experiments. Near-100% winning trades is not a defensible target or promise.

## Verified starting point

Read-only analysis of local macs.db: 80 CLOSED trades dated September 7–11, 2026; 47 wins, 33 losses; net P&L -445.57 account units. An additional 11 lowercase closed rows have no P&L and are excluded, not treated as breakeven.

| Recorded stake | Trades | Wins | Net P&L |
| --- | ---: | ---: | ---: |
| 10 | 68 | 42 | +75.28 |
| 170 | 12 | 5 | -520.85 |

The larger-stake group dominates the local loss. This is descriptive, not proof that lower stakes create an edge: the groups may differ in dates, assets, and strategy version. Average winning stake was 27.02; average losing stake was 43.94.

Local limitations: no quoted_payout or signal_id values on these 80 trades; no market_snapshots; account mode and broker missing. Later logs reference Supabase through September 19, so this snapshot cannot establish current performance. README reports earlier negative-expectancy backtests; those were not rerun for this review.

Current source: core/pipeline.py requests a fixed 170-unit stake; core/risk_management.py uses a fixed 500-unit daily loss threshold. Strategy confidence is indicator agreement, not calibrated win probability.

## Ordered restructuring

1. Establish the authoritative dataset. Obtain current Supabase exports and Deriv demo statement; reconcile every contract, payout, stake, settlement, duplicate, missing outcome, and deposit/reset. Separate versions, account modes, and currencies. Preserve originals.
2. Separate sizing from prediction. Compare actual P&L with constant-stake returns, then break results down by date, symbol, direction, duration, payout and strategy version. Treat discovered subgroups as hypotheses only.
3. Centralize risk controls. Replace hardcoded stakes and limits with explicit demo experiment settings, equity-aware sizing, aggregate open-exposure limits and a projected-loss check before each order. Count losses by settlement time. Keep reconciliation and stale-state blocking. Do not increase stakes to recover losses.
4. Make experiments reproducible. Persist closed candles, decision-time features, rejected signals, proposal quotes, actual entry/expiry, settlement, strategy/config version, account mode and execution errors. Relabel indicator confidence as agreement score unless independently calibrated.
5. Evaluate a small preregistered set of hypotheses. Use chronological training/validation and a final untouched holdout; prevent overlapping trade outcomes from leaking across split boundaries. Model actual contract timing and payout costs. Compare against simple direction baselines. Record every attempted variant, including failures.
6. Forward-test frozen candidates in demo. Judge net expectancy, profit factor, drawdown, exposure and uncertainty, not win rate alone. Define sample size and risk limits before starting; avoid stopping as soon as a result looks good. Account for correlated trades in uncertainty estimates.

## Gates

- Data gate: all evaluation trades reconciled with the broker; missing rows and legacy records explicitly classified.
- Engineering gate: no duplicate purchases; unknown risk/settlement state blocks execution; risk checks include the proposed order and existing exposure.
- Research gate: positive out-of-sample expectancy with an uncertainty interval supporting an edge after costs; results not explained by a single asset, period or lucky parameter search.
- Demo gate: independently collected forward results support the research result within predefined drawdown limits. If they fail, retain research mode or retire the candidate.
- Real-money use is outside this plan and requires a separate decision; demo success does not guarantee it.

## Data requested

- Deriv demo transaction/contract export covering the full run: contract ID, asset, direction, timestamps, stake, quoted payout, settlement and profit/loss; account currency and resets/deposits.
- Current Supabase exports: trades, signals, market_snapshots, risk_events, trade_intents and any model versions/predictions. Existing read-only access can substitute for exports. Do not share passwords or API tokens in chat.
- Starting/current demo balance and maximum acceptable experimental drawdown.
- Dates when stake, strategy, thresholds or contract durations changed, where not already recorded.

## Progress

- Complete: preliminary local-data and source review; planning document.
- Pending: current authoritative data, broker reconciliation and full loss attribution.
- Pending: implementation, backtests and prospective demo validation. No trading settings were changed during planning.
