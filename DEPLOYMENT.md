# Deploying MACS-V2

MACS-V2 runs as a background worker with no HTTP port. `start_macs.sh` loops forever:
preflight check, then `python3 run.py analyze` (one pipeline cycle), then sleep 900 s.

**Current status: do not let a deployment trade yet.**

- The production database is at revision `0002`; this code needs `0003`. Until
  `alembic upgrade head` runs, the preflight skips every cycle.
- The safety mechanisms below are implemented and covered by tests, but have not run against
  the production database or Deriv.
- The strategy is unproven. On Deriv's own 15m candles it measured about 47.9% (OTC_DJI) and
  48.9% (frxXAUUSD), against a breakeven of about 55.6%.

## Environment variables

Set these on the host at runtime. The image contains no secrets, and `.env` is excluded by
`.dockerignore`.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Supabase **Session pooler** URI: user `postgres.<project-ref>`, host `aws-X-<region>.pooler.supabase.com`, port `5432`. Copy it from the dashboard. `sslmode=require` is appended automatically. Do not use the direct `db.<ref>.supabase.co` host (IPv6-only) or the transaction pooler on port `6543` (it cannot hold the trading lock). URL-encode special characters in the password. |
| `DERIV_API_TOKEN` | yes | |
| `DERIV_APP_ID` | yes | |
| `DISCORD_WEBHOOK_URL` | no | Leave unset to disable alerts. Do not quote values in a Docker `--env-file`: quotes are kept literally. |
| `MACS_ALLOW_SQLITE` | no | Local development only. Never set it in production. |

The Deriv account ID is hardcoded in `execution/deriv_engine.py` and `core/data_deriv.py`.
The stake ($170) and contract duration (15m) come from code and `config/settings.py`.

## Database schema

Create and upgrade the schema with Alembic, and only with Alembic. `scripts/run_init.py` and the
API server's startup `init_db()` use `create_all`, which creates tables without an
`alembic_version` row.

```bash
docker run --rm --env-file .env macs-v2 alembic upgrade head
```

Migration `0003` adds the `trade_intents` table (with row level security). It creates a new
table and changes no existing ones. Re-running at head does nothing.

## Preflight

`scripts/preflight.py` runs before every cycle. When any of these is true, it skips the cycle
and does not trade:

- a Deriv credential is missing;
- the database is SQLite without `MACS_ALLOW_SQLITE=1`;
- the database is on port 6543;
- the database is unreachable (10 s connect timeout);
- the schema is not at the Alembic head.

```bash
docker run --rm --env-file .env macs-v2 python -m scripts.preflight
```

## Trading safety mechanisms

Each of these is covered by tests in `tests/`.

1. **Every buy is claimed before it is sent.** `execute_signal` commits a `trade_intents` row
   (`PENDING`) before contacting Deriv. If that write fails, nothing is bought.
2. **One trade per symbol, closed candle and direction.** A unique constraint on
   `trade_intents (symbol, candle_time, side)` turns a second attempt (a restarted worker, a
   second worker, a re-run cycle) into `DUPLICATE_SKIPPED`, before Deriv is contacted.
3. **A bought contract never silently disappears.** The contract ID is logged as soon as Deriv
   confirms the buy. The `trades` row and the intent's `EXECUTED` status are written in one
   transaction. If that fails:
   - the worker logs `RECONCILIATION REQUIRED` at CRITICAL, with the contract ID;
   - it marks the intent `UNRECORDED`, with the contract ID and the full row it meant to write;
   - it sends a Discord alert and stops the cycle.

   If even that update fails, the intent stays `PENDING`. Either way it keeps trading blocked.
4. **An ambiguous buy is never retried.** If the buy request was sent but no usable reply came
   back (timeout, dropped connection, malformed reply), the intent becomes `AMBIGUOUS` (or
   `UNRECORDED` if a contract ID was seen). The cycle stops, and trading stays blocked. An error
   reply counts as a refusal (`FAILED`, nothing bought) only when it identifies itself as the
   reply to this buy: `msg_type` `buy`, with `echo_req.buy` equal to the proposal ID. Any other
   error is `AMBIGUOUS`.
5. **Reconciliation records what it can.** At the start of each cycle, `UNRECORDED` intents are
   written to `trades` from the stored contract ID, without buying anything. Then every `OPEN`
   contract is settled from Deriv. A settlement reply for a different contract ID is ignored.
6. **The risk manager fails closed.** `can_trade()` blocks when:
   - risk state could not be read, instead of assuming zero PnL and zero losses;
   - the state is more than 5 minutes old;
   - any intent is `PENDING`, `AMBIGUOUS` or `UNRECORDED`;
   - any `OPEN` contract is at or past expiry (with a 30 s margin), or has no recorded expiry.
     Its outcome would be missing from the loss limits, so the cycle waits for reconciliation;
   - the circuit breaker or the daily loss limit applies.

   State is re-read after reconciliation, and again right before every trade.
7. **No trade without a recorded signal.** If the `signals` row cannot be written, the BUY/SELL
   is not executed.
8. **Closed candles only.** Bars whose close time is in the future (Deriv's forming bar) are
   dropped before indicators are computed. The signal is evaluated on the last closed bar, and
   the strategy itself is unchanged. Live signals now fire on the same bars the backtest scores,
   one bar later than the previous live behaviour.
9. **One worker at a time.** Each cycle holds a Postgres session advisory lock (a file lock on
   SQLite). A second worker skips its cycle. The server releases the lock if the worker dies.
10. **Bounded network waits.** Deriv OTP requests have a 10 s timeout. Every websocket connect,
    send and receive on the buy and reconcile paths has a 15 s timeout.

## Resolving a blocked worker

`risk_events` gets a `RECONCILIATION_REQUIRED` row, and the log names the intent IDs. For each
unresolved intent:

```sql
SELECT id, created_at, symbol, side, candle_time, status, contract_id, error
FROM trade_intents WHERE status IN ('PENDING', 'AMBIGUOUS', 'UNRECORDED');
```

- **`UNRECORDED` with a contract ID:** nothing to do; the next cycle records and settles it.
- **`PENDING` or `AMBIGUOUS`:** find the contract in the Deriv statement (symbol, direction, time
  around `created_at`). Then run one of:
  - it exists: `UPDATE trade_intents SET status = 'UNRECORDED', contract_id = '<id>' WHERE id = <intent id>;`
    (the next cycle records it);
  - it does not exist: `UPDATE trade_intents SET status = 'RESOLVED', error = 'No contract on Deriv statement' WHERE id = <intent id>;`

Never mark an intent `RESOLVED` without checking the Deriv statement.

## Build and run

```bash
docker build -t macs-v2 .
docker run -d --name macs --restart unless-stopped --env-file .env macs-v2
```

`constraints.txt` pins every transitive dependency. Regenerate it from a working venv with
`pip freeze` whenever `requirements.txt` changes.

## Railway

- Railway uses the `Dockerfile` automatically when one is present. The start command is the
  image `CMD`, so do not override it.
- Run one replica. The trading lock and trade intents stop duplicate trades, but a second replica
  only wastes cycles.
- Never run the Mac launchd daemon (`com.macsv2.daemon`) against the same account.
- Set the variables above in the service settings, not in a committed file.
- Run `alembic upgrade head` against the production database before the first start.

## Remaining limitations

1. **Operator action is needed after an ambiguous buy.** A `PENDING` or `AMBIGUOUS` intent
   without a contract ID blocks trading until someone checks the Deriv statement (see above). The
   code does not look contracts up by time.
2. **Shutdown during a buy** (`docker stop`, a redeploy) can leave a `PENDING` intent. That now
   blocks trading instead of losing the contract, but it still needs the manual step.
3. **The lock covers a cycle, not a connection loss.** If the lock's database connection drops
   mid-cycle, a second worker could start. Trade intents still stop it buying the same symbol,
   candle and direction.
4. **Open exposure is not limited.** The daily loss limit counts settled contracts only.
5. **`MACS_MODE` is only a label.** It defaults to `PAPER`, but `analyze` without `--dry-run`
   places real Deriv contracts on the configured account.
6. **No end-to-end run yet.** None of this has run against the production database or Deriv.
   Watch the first cycles in dry run.

### Unrecorded contracts from earlier runs

These were bought while the database was unreachable, before trade intents existed, and they
are not in `trades`. Reconcile them by hand from the Deriv statement.

- Railway: `12898580519`, one contract at about 10:17 UTC on 2026-09-14 (ID not in the logs),
  `12929687739` and `12929699399`.
- Mac daemon: `12948938079`, `12950630439`, `12952479199` and `12957857359`.
