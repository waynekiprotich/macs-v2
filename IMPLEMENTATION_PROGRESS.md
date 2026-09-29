# MACS demo restructuring progress

## Direction

Railway runs the deployed bot. Local code is the review candidate, not evidence
of its currently deployed version. The supplied September 22 export is the
analysis source. Goal: evaluate profitability honestly and control demo losses.
Near-100% wins are not an implementation promise.

## Implemented

- Explicit per-index approval and observation rules; candidate OTC_DJI BUY at 7/8.
- Broker proposal payout guard; configurable whole-minute contract durations.
- Demo balance sizing, stake cap, open exposure/count, projected daily loss checks.
- Settlement-based loss accounting; persistent candle cooldown; existing
  reconciliation, ambiguous-buy protection and trading lock retained.
- Optional causal one-hour confirmation, default off.
- Recorded experiment configuration and decision reasons; observation during cooldowns.
- Offline combined-export evaluation with chronological splits, baselines,
  earlier-period parameter grid, integrity checks and uncertainty labels.
- Dashboard score labels, decision reasons and experiment status; corrected
  performance field mapping and removed fabricated fallback profits.

## Validation

Original baseline: 132 tests passed. Initial completed change set: 174 tests passed;
dashboard production build passed. Final verification is recorded at handoff.

Offline export evaluation reproduced 58 settlements, 28 wins, -1360.38 account
units, and maximum drawdown 2246.57. One contract was open at export time.
After indicator warmup, the OTC_DJI candle series contained only 242 rows. The
frozen candidate produced zero earlier-period simulations and three later-period
simulations (two wins). This is insufficient to validate profitability. It is not
an estimate of future performance, and it uses an assumed 80% net payout.

## Remaining / intentionally inactive

- More independent history and a frozen prospective demo evaluation.
- Authenticated Deriv balance/proposal compatibility check on the deployed version.
- Calibrated probability model and automatic market promotion require evidence.
- Indicator score redesign requires its own experiment and cannot be bundled
  with an unexplained change to the eight-condition score.
- Railway rollout and explicit demo activation remain separate from the authorized GitHub push.

## September 28 verification

Read-only current database: 99 closed trades, 46 wins, net -2836.60; Gold -2646.96.
All stakes remain 170 in the deployed history. Local regression suite: 187 passed.
Authenticated demo balance lookup passed without placing orders. Invalid settlement
values now block risk decisions; CI verifies the worker image and dashboard build.
