"""Offline evaluation of a frozen experiment using a Beekeeper JSON export.

No broker calls, database imports, training, or automatic promotion. Candle
simulations are approximations: actual entry ticks and historical quotes are
not available for unexecuted signals.
"""
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from config.settings import MarketRule
from core import technical_strategy
from core.experiment import experiment_config, config_fingerprint, hourly_trend, signal_filter
from core.indicators import compute_indicators
from core.regime import detect_regime


def load_export(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw)
    if isinstance(data, list):
        if len(data) != 1 or 'macs_complete_export' not in data[0]:
            raise ValueError("Expected the combined Beekeeper export")
        data = data[0]['macs_complete_export']
    elif 'macs_complete_export' in data:
        data = data['macs_complete_export']
    if isinstance(data, str):
        data = json.loads(data)
    for table in ('trades', 'signals', 'market_snapshots'):
        if not isinstance(data.get(table), list):
            raise ValueError(f"Export is missing {table}")
    return data, hashlib.sha256(raw).hexdigest()


def metrics(trades):
    values = [float(t['pnl']) for t in trades]
    wins = sum(p > 0 for p in values)
    losses = sum(p < 0 for p in values)
    gross_win = sum(p for p in values if p > 0)
    gross_loss = -sum(p for p in values if p < 0)
    equity = peak = drawdown = 0.0
    for p in values:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    n = len(values)
    interval = None
    if n:
        z = 1.96
        p = wins / n
        denominator = 1 + z*z/n
        center = (p + z*z/(2*n)) / denominator
        margin = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / denominator
        interval = [round(100*(center-margin), 2), round(100*(center+margin), 2)]
    return {
        'trades': n, 'wins': wins, 'losses': losses,
        'win_rate_pct': round(100*wins/n, 2) if n else None,
        'pnl': round(sum(values), 4),
        'expectancy': round(sum(values)/n, 4) if n else None,
        'profit_factor': round(gross_win/gross_loss, 4) if gross_loss else None,
        'max_drawdown': round(drawdown, 4),
        'win_rate_95pct_iid_interval': interval,
    }


def prepare_snapshots(snapshots, symbol):
    rows = [s for s in snapshots if s['symbol'] == symbol and s['granularity'] == 900]
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame['candle_time'] = pd.to_datetime(frame['candle_time'], utc=True)
    # Conflicting candles must not silently be resolved by row order.
    for _, group in frame.groupby('candle_time'):
        if len(group[['open', 'high', 'low', 'close']].drop_duplicates()) > 1:
            raise ValueError(f"Conflicting duplicate candles for {symbol}")
    frame = frame.drop_duplicates('candle_time').sort_values('candle_time').set_index('candle_time')
    frame = frame.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close'})
    frame = frame[['Open', 'High', 'Low', 'Close']].astype(float)
    if frame.isna().any().any() or not frame.map(math.isfinite).all().all():
        raise ValueError(f"Invalid candle prices for {symbol}")
    frame['Volume'] = 1000.0  # Compatibility only; strategy never votes on volume.
    hourly = hourly_trend(frame)
    # Same causal indicators as the trading pipeline. Preserve warmup history
    # when splitting; never warm up on future observations.
    frame = technical_strategy.prepare(detect_regime(compute_indicators(frame)))
    frame['Hourly_Trend'] = hourly.reindex(frame.index)
    return frame


def simulate(frame, rule, payout, start=None, end=None, baseline=None):
    """One contract at a time, whole durations, no outcomes crossing a split.

    Entry is approximated by the signal candle close. Ties lose for strict
    CALL/PUT. No interpolation over missing bars or market closures.
    """
    if frame.empty or payout < rule.min_payout:
        return []
    trades = []
    available_at = None
    previous = {}
    duration = pd.Timedelta(minutes=rule.duration_minutes)
    quarter = pd.Timedelta(minutes=15)
    for timestamp, row in frame.iterrows():
        entry_time = timestamp + quarter
        expiry_time = entry_time + duration
        if start is not None and entry_time < start:
            continue
        if end is not None and expiry_time >= end:
            continue
        if available_at is not None and entry_time <= available_at:
            continue  # Match the broker contract starting seconds after a candle.
        expected = pd.date_range(timestamp, timestamp + duration, freq='15min')
        if not expected.isin(frame.index).all():
            continue
        if baseline:
            direction = baseline
        else:
            result = technical_strategy.generate_signal(row, min_conditions=rule.min_conditions,
                                                       is_volatile=bool(row.get('Is_Volatile', False)))
            direction = result['signal']
            if direction not in ('BUY', 'SELL') or signal_filter(rule, direction, row.get('Hourly_Trend', 'unknown')):
                continue
        if direction in previous and timestamp <= previous[direction] + quarter * rule.cooldown_bars:
            continue
        exit_price = frame.loc[timestamp + duration, 'Close']
        won = exit_price > row['Close'] if direction == 'BUY' else exit_price < row['Close']
        trades.append({'entry_time': entry_time.isoformat(), 'expiry_time': expiry_time.isoformat(),
                       'side': direction, 'pnl': payout if won else -1.0})
        available_at = expiry_time
        previous[direction] = timestamp
    return trades


def evaluate_export(data, digest, payout=0.80, research_grid=False):
    if not math.isfinite(payout) or payout <= 0:
        raise ValueError("Assumed net payout must be positive and finite")
    config = experiment_config()
    closed = sorted([t for t in data['trades'] if t['status'].upper() == 'CLOSED' and t['pnl'] is not None],
                    key=lambda t: t.get('expiry_time') or t.get('closed_timestamp') or t['timestamp'])
    ids = [t['contract_id'] for t in data['trades'] if t.get('contract_id')]
    symbols = sorted({s['symbol'] for s in data['market_snapshots']} | set(config['rules']))
    report = {
        'status': 'RESEARCH_ONLY_NOT_VALIDATED', 'source_sha256': digest,
        'exported_at': data.get('exported_at_utc'), 'config': config,
        'config_fingerprint': config_fingerprint(), 'assumed_net_payout': payout,
        'warnings': [
            'This export informed the candidate selection; none of it is a genuinely untouched holdout.',
            'Chronological splits check methodology only. Confirm on newly collected data.',
            'Candle-close simulations omit real entry delays and use assumed, not historical, payouts.',
            'Simulations are per-symbol unit-stake diagnostics, not a full account or risk-manager replay.',
            'Win-rate intervals assume independent trades; correlated outcomes can make them too narrow.',
            'No probability model is trained or promoted, and no profitability claim is made.',
        ],
        'counts': {k: len(v) for k, v in data.items() if isinstance(v, list)},
        'integrity': {'duplicate_contract_ids': len(ids)-len(set(ids)),
                      'missing_trade_signals': sum(t.get('signal_id') not in {s['id'] for s in data['signals']} for t in closed),
                      'settlement_mismatches': sum(abs(t['sell_price']-t['price']-t['pnl']) > 0.011
                                                   for t in closed if t.get('sell_price') is not None)},
        'actual_closed_account_units': metrics(closed),
        'actual_by_symbol': {s: metrics([t for t in closed if t['symbol'] == s]) for s in symbols},
        'simulations_unit_stake': {},
    }
    for symbol in symbols:
        frame = prepare_snapshots(data['market_snapshots'], symbol)
        if len(frame) < 100:
            report['simulations_unit_stake'][symbol] = {'status': 'INSUFFICIENT_CANDLES', 'prepared_bars': len(frame)}
            continue
        split = frame.index[int(len(frame)*0.7)] + pd.Timedelta(minutes=15)
        candidate = MarketRule(**config['rules'].get(symbol, {}))
        baseline_rule = MarketRule(enabled=True, directions=['BUY', 'SELL'], min_conditions=6,
                                   min_payout=0, cooldown_bars=0)
        variants = [('original_6_conditions', baseline_rule, None),
                    ('frozen_candidate', candidate, None),
                    ('always_buy', baseline_rule, 'BUY'), ('always_sell', baseline_rule, 'SELL')]
        entry = {'prepared_bars': len(frame), 'split_utc': split.isoformat(), 'variants': {}}
        for name, rule, baseline in variants:
            entry['variants'][name] = {
                'earlier': metrics(simulate(frame, rule, payout, end=split, baseline=baseline)),
                'later': metrics(simulate(frame, rule, payout, start=split, baseline=baseline)),
            }
        if research_grid:
            entry['earlier_only_research_grid'] = []
            for threshold in (6, 7, 8):
                for minutes in (15, 30, 60, 120):
                    for hourly in (False, True):
                        rule = candidate.model_copy(update={'enabled': True, 'min_conditions': threshold,
                                                            'duration_minutes': minutes, 'hourly_confirmation': hourly})
                        entry['earlier_only_research_grid'].append({
                            'rule': rule.model_dump(), 'metrics': metrics(simulate(frame, rule, payout, end=split))})
        report['simulations_unit_stake'][symbol] = entry
    return report
