# Demo experiment and review guide

## Behavior

The bot is experimental and unvalidated. The new default is observation only:
`MACS_EXECUTION_ENABLED=false`. Explicitly enabling it also requires
`MACS_MODE=PAPER`. The authenticated broker connection must use Deriv's demo
WebSocket endpoint and report USD cash. The existing demo account ID is preserved.

The integration follows Deriv's [OTP workflow](https://developers.deriv.com/docs/options/)
and [authenticated balance endpoint](https://developers.deriv.com/docs/account/balance/).

The proposed OTC_DJI rule permits BUY, requires at least 7 of 8 conditions,
requires at least 80% net payout, and requests a 15-minute contract. Eight-condition
signals also qualify; the small winning 87.5-score subgroup is not isolated as
though it proved an edge. Gold is observation-only. Additional symbols passed on
the command line cannot bypass the allowlist. Add explicit observation rules to
`MACS_MARKET_RULES` to collect other indices; no index is automatically promoted.

Each rule independently supports directions, threshold, minimum payout, duration
(15-minute multiples up to 120 minutes), optional completed-hour EMA confirmation,
and a same-direction candle cooldown. Hourly confirmation defaults off until it
is evaluated. The cooldown of one candle blocks a same-direction entry on the
next candle. Existing contract reconciliation and unique intent protection remain.

Volume was already excluded from the active eight-condition strategy. Its
constant placeholder remains for compatibility, never as evidence of demand.
The score is called indicator agreement, including in the dashboard and alerts.

## Stake and risk policy

- Stake: 0.25% of current available demo cash, rounded down to cents, capped at 25 USD.
- If the result is below the configured minimum stake, skip; never round up to trade.
- Maximum open contracts: one by default.
- Open stake plus the proposed stake must remain within 1% of available cash.
- Realized daily P&L minus all open stakes minus the proposed stake must remain
  above the stricter of 2% of current cash or a 500 USD loss ceiling.
- The percentage limit uses current cash conservatively, not claimed start-of-day
  equity. Deposits/resets are not inferred from P&L. Keep a demo experiment free
  of balance resets or start a new experiment record when a reset occurs.
- Closed trades are assigned to their recorded expiry, falling back to recorded
  close time then legacy entry time. Expiry is derived from contract duration;
  late reconciliation does not move a loss to the reconciliation day.
- Three consecutive losses trigger a four-hour cooldown from settlement.
- The stake does not depend on the agreement score or recent losses.
- Unknown/stale risk state, balance failure, unsupported currency, stale signals,
  low or invalid payout quotes, and unresolved intents prevent purchases.
- Risk is checked again after the quote, excluding only this order's own pending
  intent. The pipeline holds its existing single-worker trading lock throughout.

The 25 USD cap is a conservative demo default that can be lowered. A larger
account will not silently bring the 170-unit stake back. The broker may reject
its own minimum stake or product rules; rejection never triggers a stake increase.

## Audit trail

Signals retain their original indicator counts and include the complete non-secret
experiment configuration and its fingerprint in `indicators`. Trades include the
experiment ID and fingerprint in `reason`. Skipped signals have explicit decisions
such as OBSERVATION_ONLY, DIRECTION_BLOCKED, HOURLY_FILTER or POLICY_BLOCKED.
Order-level budget and payout blocks are also recorded in `risk_events`.
Observation collection continues during risk cooldowns when the database is usable.
No database migration is required; the existing JSON fields hold the metadata.

Legacy field names `confidence` and `confidence_score` remain for API compatibility.
New consumers should use `agreement_score`; it is not a probability estimate.

## Local verification

```bash
venv/bin/python -m pytest -q
venv/bin/python cli.py experiment-config
npm --prefix dashboard run build
```

Tests use a temporary SQLite database and block external HTTP/WebSocket requests
unless a test explicitly provides a fake transport. They do not trade, send
notifications, reset accounts, or write to Supabase.

Evaluate a full Beekeeper export without connecting to a database or broker:

```bash
venv/bin/python -m scripts.evaluate_experiment \
  --export /absolute/path/to/complete-export.json \
  --research-grid \
  --output /absolute/path/to/new-report.json
```

The output path must not already exist. The report contains a source digest,
effective configuration, actual settled performance, integrity counts and
unit-stake simulations. Keep raw exports and account information out of Git.

The chronological split preserves indicator warmup history, forbids outcomes
crossing the split, rejects conflicting duplicate candles, skips missing expiry
bars, and applies one-open-contract timing and cooldowns. The optional research
grid explores 6/7/8 conditions, 15/30/60/120 minutes and hourly confirmation only
on the earlier period. It never selects or promotes a winner automatically.

Simulation limitations: entry uses candle close rather than actual execution
ticks, payouts are assumptions, and account loss limits are not replayed. Results
are per-symbol diagnostics. Always-BUY and always-SELL comparisons use the same
one-open-contract timing. Probability intervals assume independent observations;
they are not proof of an edge when trades are correlated. Because this export
informed the candidate design, its later partition is not a genuinely untouched
holdout. New data is required to validate the candidate.

## Review before Railway activation

1. Review the local diff and test results; GitHub push requires the user's approval.
2. Review Railway's deployed commit and explicitly set the new non-secret variables
   from `.env.example`. Never copy the local database URL or secrets into a report.
3. Keep `MACS_EXECUTION_ENABLED=false` after deploying for the first observation cycle.
4. Verify experiment metadata, candle times, observations, database health and
   demo account/balance compatibility. An authenticated balance request still needs
   a broker integration check; automated tests use a simulated broker.
5. Only after the agreed review, enable the frozen demo experiment. Observe the
   first proposal and settlement, confirm payout/risk checks and audit records.
6. Keep a predefined evaluation window and drawdown limit. Do not retune on each
   loss. Disable new entries by setting `MACS_EXECUTION_ENABLED=false`; existing
   contracts still reconcile and observation collection continues.

This change does not push to GitHub, deploy to Railway, enable trading, or send
test purchases. Railway environment configuration remains a separate rollout step.

## Research still requiring evidence

Calibrated probabilities, confidence-based adaptive stakes, automatic promotion
of new indices and a redesigned indicator score are not enabled. The export has
58 settled trades and no active model, which is insufficient to justify those
decisions. Existing `ml/train.py` remains research tooling; adding a probability
to the trading path requires independent calibration and forward validation.
Reducing correlated trend votes would change the meaning of 7/8 and requires a
separate, versioned scoring experiment rather than silently modifying this one.
