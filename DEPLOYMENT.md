# Deploying MACS-V2

MACS-V2 runs as a background worker with no HTTP port. `start_macs.sh` loops forever:
preflight check, then `python3 run.py analyze` (one pipeline cycle), then sleep 900 s.

**Current status: do not let a deployment trade yet.** The open safety issues at the end of
this file allow real $170 contracts to be bought without being recorded. The strategy is also
unproven. On Deriv's own 15m candles it measured about 47.9% (OTC_DJI) and 48.9% (frxXAUUSD)
against a breakeven of about 55.6%.

## Environment variables

Set these on the host at runtime. The image contains no secrets, and `.env` is excluded by
`.dockerignore`.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | yes | Supabase **Session pooler** URI: user `postgres.<project-ref>`, host `aws-0-<region>.pooler.supabase.com`, port `5432`. `sslmode=require` is appended automatically. URL-encode special characters in the password. |
| `DERIV_API_TOKEN` | yes | |
| `DERIV_APP_ID` | yes | |
| `DISCORD_WEBHOOK_URL` | no | Leave unset to disable alerts. Do not quote values in a Docker `--env-file`: quotes are kept literally. |
| `MACS_ALLOW_SQLITE` | no | Local development only. Never set it in production. |

The Deriv account ID is hardcoded in `execution/deriv_engine.py` and `core/data_deriv.py`.
The stake ($170) and contract duration (15m) come from code and `config/settings.py`.

## One-time database initialisation

Create the schema with Alembic, and only with Alembic. `scripts/run_init.py` and the API
server's startup `init_db()` use `create_all`, which creates tables without an
`alembic_version` row. After that, `alembic upgrade head` fails and the preflight refuses to run.

```bash
docker run --rm --env-file .env macs-v2 alembic upgrade head
```

For an empty database this creates every table and enables row level security (migration
`0002`). It is safe to re-run: at head it does nothing.

## Preflight

`scripts/preflight.py` runs before every cycle. When any of these is true, it skips the cycle
and does not trade:

- a Deriv credential is missing;
- the database is SQLite without `MACS_ALLOW_SQLITE=1`;
- the database is unreachable (10 s connect timeout);
- the schema is not at the Alembic head.

It checks once per cycle, not per trade. A database that fails *during* a cycle is still
unprotected (see issue 1).

```bash
docker run --rm --env-file .env macs-v2 python -m scripts.preflight
```

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
- Run exactly **one** replica, and never run the Mac launchd daemon (`com.macsv2.daemon`) at the
  same time. Nothing in the code prevents two instances from trading the same account (issue 4).
- Set the variables above in the service settings, not in a committed file.
- Run the Alembic initialisation step against the production database before the first start.

## Open safety issues (not fixed)

These are documented, not solved. Fix them, with tests, before a deployment is allowed to trade.

1. **Trades can be bought but not recorded.** `execution/deriv_engine.py` buys the contract and
   only then writes the `trades` row. If that write fails, the error is logged
   (`Failed to log Deriv trade`), nothing retries, and the Discord alert is skipped, so the
   contract is invisible to the database, reconciliation and the risk manager.
2. **The risk manager fails open.** `core/risk_management.py` `_load_state` logs a database error
   and keeps `daily_pnl = 0` and `consecutive_losses = 0`, so `can_trade()` allows trading. Its
   state is also loaded in `TradingPipeline.__init__`, *before* `run()` reconciles open
   contracts, so losses settled since the last cycle are missed for one cycle.
3. **A failed signal write does not stop a trade.** `core/pipeline.py` `_log_signal` returns
   `None` on error and execution continues with `signal_id=None`.
4. **No single-instance or duplicate-trade protection.** There is no lock, no unique constraint on
   `trades.contract_id`, and no idempotency key per `(symbol, candle_time)`. Two workers, or a
   restarted worker inside the same 15m bar, can buy twice on one signal.
5. **Signals use the forming candle.** Deriv's `ticks_history` returns the still-forming bar
   last, and `core/pipeline.py` evaluates `df.iloc[-1]`. Closed-bar and forming-bar signals
   disagreed on more than half of the bars measured. Changing this changes which trades fire, so
   it needs its own reviewed change.
6. **No network timeouts on the buy path.** In `execution/deriv_engine.py`, the OTP
   `requests.post` and every `ws.recv()` (proposal, buy, reconcile) have no timeout, so a hung
   connection stalls the worker indefinitely.
7. **Reconciliation is best-effort.** Contracts whose status cannot be read stay `OPEN`, and
   responses are matched to requests by order, not by `req_id`.
8. **Shutdown can interrupt a buy.** `docker stop` or a redeploy during a cycle can kill the
   process between the Deriv buy and the database write (the same outcome as issue 1).
9. **`MACS_MODE` is only a label.** It defaults to `PAPER`, but `analyze` without `--dry-run`
   places real Deriv contracts on the configured account.

### Unrecorded contracts from earlier runs

These were bought while the database was unreachable, and they are not in `trades`. Reconcile
them by hand from the Deriv statement; the code cannot recover them.

- Railway: `12898580519`, one contract at about 10:17 UTC on 2026-09-14 (ID not in the logs),
  `12929687739` and `12929699399`.
- Mac daemon: `12948938079`, `12950630439`, `12952479199` and `12957857359`.
