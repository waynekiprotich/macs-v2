# Deploying MACS-V2

MACS-V2 is a background worker with no HTTP port. It buys 15-minute CALL/PUT contracts on Deriv
for OTC_DJI and frxXAUUSD, with a $170 stake, when 6 of 8 technical conditions agree.

**The strategy is unproven.** On Deriv's own 15m candles it measured about 47.9% (OTC_DJI) and
48.9% (frxXAUUSD). The payouts quoted on the first live trades imply a breakeven of about
57.5–59.3%.

## Where it runs

| | Role |
|---|---|
| **Railway** | The only production worker. It builds the `Dockerfile` from branch `chore/production-deploy-prep` and runs one replica. |
| **Supabase** | Production database: Postgres through the Session pooler (port 5432), managed by Alembic. |
| **Deriv** | Account `DOT90734760`, a demo account, reached through the Options API (`api.derivws.com`). |
| **Mac** | Development and testing only. The launchd job in `com.macsv2.daemon.plist` must stay unloaded: it would be a second worker on the same account. |

Status as of 2026-09-15:
- Railway ran commit `6c574e79` against the database at revision `0003`. Its logs showed
  `Preflight OK` and three frxXAUUSD contracts bought, recorded and reconciled.
- The changes described below are on the branch but **not deployed**. They include migration
  `0004` and the candle-aligned scheduler.

`MACS_MODE` (default `PAPER`) is only a label written on each trade. It does **not** stop trading:
`run.py analyze` without `--dry-run` buys real contracts on the configured account.

## Environment variables

Set these in Railway's service settings. The image contains no secrets, and `.env` is excluded by
`.dockerignore`.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Supabase **Session pooler** URI, copied from the dashboard. `sslmode=require` is added automatically. The direct `db.<ref>.supabase.co` host is IPv6-only, and the transaction pooler (port 6543) cannot hold the trading lock; the preflight refuses it. URL-encode special characters in the password. |
| `DERIV_API_TOKEN` | yes | |
| `DERIV_APP_ID` | yes | |
| `DISCORD_WEBHOOK_URL` | no | Trade alerts, heartbeats and reconciliation alerts. Unquoted. |
| `MACS_ALLOW_SQLITE` | no | Local development only. Never set it in production. |

The Deriv account ID and the stake live in code (`execution/deriv_engine.py`, `core/data_deriv.py`,
`core/pipeline.py`). The threshold and duration live in `config/settings.py`.

## One cycle

`start_macs.sh` loops forever:

1. **Wait for the candle to close.** `python -m scripts.wait_for_cycle` (`core/schedule.py`) sleeps
   until 10 s after the next 15-minute boundary.
   - The wait is recomputed from the clock every time, so cycle length never shifts the start time.
   - If a cycle would start more than 120 s after its boundary (slow restart, paused container,
     long previous cycle), that candle is skipped and it waits for the next one.
   - A boundary is never run twice, even if the clock steps backwards.
   - If the scheduler itself fails, the loop waits 60 s and runs no cycle.
2. **Preflight** (`scripts/preflight.py`). The cycle is skipped if a Deriv credential is missing, the
   database is SQLite without `MACS_ALLOW_SQLITE=1` or on port 6543, the database is unreachable
   (10 s timeout), or the schema is not at the Alembic head.
3. **`run.py analyze`** runs `TradingPipeline.run`:
   1. Take the single-worker lock. If another worker holds it, or it can't be checked, the cycle is
      skipped.
   2. If an OPEN contract expires within 60 s, wait until 10 s after its expiry.
   3. Reconcile. Record `UNRECORDED` intents, then settle every OPEN contract from Deriv.
   4. Reload risk state and check it. Anything unsafe blocks the whole cycle.
   5. For each symbol:
      - fetch candles and keep **closed candles only**;
      - compute indicators, the regime and the signal on the last closed candle;
      - store candles and the signal.
   6. For a BUY/SELL, stop unless all of these hold:
      - the signal row was saved;
      - the candle closed at most 120 s ago;
      - risk state, re-read right then, allows it.
   7. Claim a trade intent, buy, and record the trade.

## Trading safety

Every item below is covered by `pytest`.

| Condition | Outcome |
|---|---|
| Database unreachable | Preflight skips the cycle. A failure mid-cycle makes risk state unavailable: no buy. |
| Risk state can't be read, or is more than 5 minutes old | No buy. Nothing is assumed to be zero. |
| A trade intent is `PENDING`, `AMBIGUOUS` or `UNRECORDED` | No buy until it is resolved. |
| An OPEN contract is at or past expiry (30 s margin), or has no recorded expiry | No buy until it is reconciled. |
| Circuit breaker (3 consecutive losses, 4 h) or daily loss limit (−$500) | No buy (unchanged). |
| Signal row could not be saved | No buy. |
| Same symbol, candle and direction already claimed | `DUPLICATE_SKIPPED`, before Deriv is contacted. Unique constraint on `trade_intents (symbol, candle_time, side)`. |
| Signal's candle closed more than 120 s ago | `STALE_SIGNAL`, no buy. |
| Forming (unclosed) candle | Dropped before indicators; never evaluated. |
| Another worker holds the lock, or the lock can't be checked | Cycle skipped. |
| Trade intent can't be saved | No buy. |
| Deriv refuses the buy (a reply identified as the buy's) | Intent `FAILED`, nothing bought, trading continues. |
| Buy sent, but the reply times out, is malformed, or can't be matched to the buy | Intent `AMBIGUOUS`, or `UNRECORDED` if a contract ID was seen. Never retried; the cycle stops. |
| Buy confirmed, but the trade row can't be saved | Contract ID logged at CRITICAL; intent `UNRECORDED` with the full row; Discord alert; the cycle stops. The next cycle records it without buying. |
| Process stopped during a buy | The intent stays `PENDING` and blocks until someone checks it (below). |

Network waits are bounded: 10 s for the OTP request, 15 s for every websocket connect, send and
receive.

## Deriv replies

Checked on 2026-09-15 against real Options API replies: `buy` errors on the public websocket, and
`proposal_open_contract` replies for two settled demo contracts. Recorded copies are in
`tests/fixtures/`.

- Every request carries a `req_id`. Buy replies (public websocket) and settlement replies (demo
  account) were verified to echo it, together with `msg_type` and `echo_req`. Whether a proposal
  reply to an authenticated request echoes `req_id` has **not** been verified. If it doesn't, the
  proposal check below stops every attempt before the buy is sent: nothing is bought, but nothing
  trades. Watch the first BUY-qualified cycle after deployment for `reply not identifiable as the
  proposal response`.
- A buy reply counts only if its `req_id`, `msg_type` `buy` and echoed proposal ID all match.
  - A matched `error` is a refusal.
  - Any other reply leaves the buy's outcome unknown.
- A proposal reply counts only if its `req_id` and `msg_type` `proposal` match. Before the buy,
  a mismatch just ends the attempt.
- A settlement reply counts only if its `req_id` and contract ID match. Otherwise the contract
  stays OPEN.
- Settlement fields come from `proposal_open_contract`:
  - `profit` is the result. A settled reply without it leaves the contract OPEN.
  - `sell_price` is the settlement amount, and `payout` what the contract pays on a win.
  - `entry_spot` and `exit_spot` are the spots. The older v3 names `entry_tick`, `exit_tick`
    and `sell_spot` are also read.

## Trade record fields

| Column | Meaning |
|---|---|
| `price` | Stake Deriv charged (`buy_price`). |
| `quoted_payout` | Deriv's payout quote at buy; never changed afterwards. |
| `payout` | What the contract pays on a win: the quote, confirmed at settlement. The same won or lost. |
| `sell_price` | What the contract settled for: the payout on a win, 0 on a loss. |
| `pnl` | Deriv's `profit`. |
| `entry_spot` | The proposal-time spot at buy; replaced by Deriv's actual entry spot at settlement. |
| `exit_spot` | Deriv's exit spot at settlement; NULL if Deriv doesn't report one. |

The first three trades were reconciled before these fixes, and they are left as recorded:
- their `exit_spot` is NULL;
- the losing trade has `payout` 0;
- their `entry_spot` is the proposal-time spot.

Their `quoted_payout`, `sell_price` and `pnl` are correct. `python cli.py performance` reads
`quoted_payout`.

## Database and migrations

Create and upgrade the schema with Alembic only. `scripts/run_init.py` and the API server's
`init_db()` use `create_all`, which bypasses Alembic and breaks later upgrades.

| Revision | Change |
|---|---|
| `0001` | Schema: trades, signals, market_snapshots, model_versions, model_predictions, risk_events. |
| `0002` | Row level security on every table. The worker connects as the table owner, which bypasses it. |
| `0003` | `trade_intents`. Production is at this revision. |
| `0004` | Unique index on `trades.contract_id`. Changes no rows; NULLs don't collide. **Not yet applied to production.** |

**Rolling out `0004` pauses trading briefly.** Railway's code and the database must both reach
`0004` before the preflight passes again:
1. Deploy the new code. Its preflight fails ("schema at 0003, expected 0004") and it skips cycles.
2. Run `alembic upgrade head` against production.
3. The next cycle passes the preflight.

The reverse order also pauses: the old code refuses a database at `0004`.

```bash
alembic upgrade head
```

## Resolving a blocked worker

The log names the intent IDs, and `risk_events` gets a `RECONCILIATION_REQUIRED` row.

```sql
SELECT id, created_at, symbol, side, candle_time, status, contract_id, error
FROM trade_intents WHERE status IN ('PENDING', 'AMBIGUOUS', 'UNRECORDED');
```

- **`UNRECORDED` with a contract ID:** nothing to do; the next cycle records and settles it.
- **`PENDING` or `AMBIGUOUS`:** find the contract in the Deriv statement (symbol, direction, time
  around `created_at`). Then:
  - it exists: `UPDATE trade_intents SET status = 'UNRECORDED', contract_id = '<id>' WHERE id = <intent id>;`
  - it does not exist: `UPDATE trade_intents SET status = 'RESOLVED', error = 'No contract on Deriv statement' WHERE id = <intent id>;`

Never mark an intent `RESOLVED` without checking the Deriv statement.

## Local development and dry runs

**Never run the worker, or `analyze`, against the production database.** It would write signals
and candles into production, reconcile production trades, and take the same single-worker lock,
which can make Railway skip a cycle. Point `DATABASE_URL` at a local database, migrated with
`alembic upgrade head`.

`python run.py analyze --dry-run` never calls the buy path. `--dry-run` sets `execute=False`, and
the pipeline then logs `DRY_RUN` instead of calling `DerivEngine.execute_signal`, the only code
that sends a buy (covered by
`test_dry_run_evaluates_a_buy_signal_but_never_reaches_the_engine`). A dry run still:
- uses `DERIV_API_TOKEN` to fetch candles (read-only);
- reconciles any OPEN trades in the database it points at;
- writes signals and candles to that database.

A BUY/SELL evaluated more than 120 s after its candle closed shows `STALE_SIGNAL`, not `DRY_RUN`.

```bash
DATABASE_URL=sqlite:///local-dev.db MACS_ALLOW_SQLITE=1 alembic upgrade head
```
```bash
DATABASE_URL=sqlite:///local-dev.db MACS_ALLOW_SQLITE=1 python run.py analyze --dry-run
```
```bash
pytest -q
```

No end-to-end local dry run was performed as part of these changes; they are verified by the test
suite.

## Build and run

```bash
docker build -t macs-v2 .
```

`constraints.txt` pins every transitive dependency. Regenerate it from a working venv with
`pip freeze` whenever `requirements.txt` changes.

## Remaining limitations

1. **Ambiguous buys need an operator.** A `PENDING` or `AMBIGUOUS` intent without a contract ID
   blocks trading until someone checks the Deriv statement; the code does not look contracts up by
   time.
2. **A redeploy during a buy** leaves a `PENDING` intent, with the same manual step.
3. **The lock covers a cycle, not a lost connection.** If its database connection drops mid-cycle, a
   second worker could start. Trade intents still stop a second buy on the same symbol, candle
   and direction.
4. **Open exposure is not limited.** The daily loss limit counts settled contracts only.
5. **A slow settlement blocks the next cycle.** If Deriv hasn't settled a contract 10 s after
   expiry, that cycle is blocked and the following one continues.
6. **Eight contracts from 2026-09-14 are not in the database:**
   - Railway: `12898580519`, one at about 10:17 UTC (ID not in the logs), `12929687739`,
     `12929699399`.
   - Mac: `12948938079`, `12950630439`, `12952479199`, `12957857359`.

   Reconcile them by hand from the Deriv statement.
