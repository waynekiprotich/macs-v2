import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest
from pydantic import ValidationError

from config.settings import MarketRule, Settings, settings
from core.experiment import stake_for_balance, hourly_trend, signal_filter, config_fingerprint
from core.risk_management import RiskManager
from execution import deriv_engine
from execution.deriv_engine import DerivEngine
from models.database import PaperTrade, SessionLocal, SystemLog, TradeIntent
from tests.test_deriv_engine import deriv, BUY_OK, CANDLE, _buy, _trades, _intents
from tests.test_pipeline_logging import run_pipeline, FakeEngine, SYMBOL


@pytest.mark.parametrize('balance,expected', [(10000, 25), (5000, 12.5), (999.99, 2.49), (100000, 25)])
def test_balance_sizing_is_capped_and_rounds_down(balance, expected, monkeypatch):
    monkeypatch.setattr(settings, 'MACS_RISK_PER_TRADE', 0.0025)
    monkeypatch.setattr(settings, 'MACS_MAX_STAKE', 25)
    assert stake_for_balance(balance) == expected


@pytest.mark.parametrize('balance', [0, -1, float('nan'), float('inf'), 1])
def test_unknown_or_insufficient_balance_never_falls_back_to_170(balance):
    with pytest.raises(ValueError):
        stake_for_balance(balance)


@pytest.mark.parametrize('fields', [dict(min_conditions=9), dict(directions=['CALL']),
                                   dict(duration_minutes=17), dict(min_payout=float('nan'))])
def test_invalid_market_rules_rejected(fields):
    with pytest.raises(ValidationError):
        MarketRule(**fields)


def test_defaults_are_experimental_observation_until_enabled():
    config = Settings(_env_file=None)
    assert not config.MACS_EXECUTION_ENABLED
    assert config.MACS_MARKET_RULES['OTC_DJI'].min_conditions == 7
    assert config.MACS_MARKET_RULES['OTC_DJI'].directions == ['BUY']
    assert not config.MACS_MARKET_RULES['frxXAUUSD'].enabled


def test_configuration_fingerprint_changes_with_rules(monkeypatch):
    old = config_fingerprint()
    monkeypatch.setattr(settings, 'MACS_MARKET_RULES', {'OTC_DJI': MarketRule(duration_minutes=60)})
    assert config_fingerprint() != old


def test_hourly_confirmation_uses_only_complete_past_hours():
    index = pd.date_range('2026-01-01', periods=120, freq='15min', tz='UTC')
    data = pd.DataFrame({'Close': range(120)}, index=index)
    first = hourly_trend(data)
    assert first.iloc[:103].eq('unknown').all()
    assert first.iloc[103] == 'bullish'
    changed = data.copy()
    changed.iloc[111:] = -100000
    assert hourly_trend(changed).iloc[:111].equals(first.iloc[:111])
    missing = hourly_trend(data.drop(index[101]))
    assert missing.loc[index[103]] == 'unknown'


def test_optional_hourly_filter_requires_direction_agreement():
    rule = MarketRule(enabled=True, hourly_confirmation=True)
    assert signal_filter(rule, 'BUY', 'bullish') is None
    assert signal_filter(rule, 'BUY', 'unknown') == 'HOURLY_FILTER'
    assert signal_filter(rule, 'BUY', 'bearish') == 'HOURLY_FILTER'
    assert signal_filter(rule, 'SELL', 'bearish') == 'DIRECTION_BLOCKED'


def test_observation_symbol_never_reaches_executor(run_pipeline, monkeypatch):
    monkeypatch.setattr(settings, 'MACS_MARKET_RULES', {SYMBOL: MarketRule(enabled=False)})
    assert run_pipeline('BUY')[SYMBOL]['action'] == 'OBSERVATION_ONLY'
    assert FakeEngine.calls == []
    with SessionLocal() as db:
        signal = db.query(SystemLog).filter_by(symbol=SYMBOL).order_by(SystemLog.id.desc()).first()
        assert signal.action_taken == 'OBSERVATION_ONLY'
        assert 'experiment' in signal.indicators


def test_sell_is_filtered_before_executor(run_pipeline, monkeypatch):
    monkeypatch.setattr(settings, 'MACS_MARKET_RULES', {SYMBOL: MarketRule(enabled=True)})
    assert run_pipeline('SELL')[SYMBOL]['action'] == 'DIRECTION_BLOCKED'
    assert FakeEngine.calls == []


def test_disabled_execution_still_records_signals(run_pipeline, monkeypatch):
    monkeypatch.setattr(settings, 'MACS_EXECUTION_ENABLED', False)
    assert run_pipeline('BUY')[SYMBOL]['action'] == 'OBSERVATION_ONLY'
    assert FakeEngine.calls == []


def test_disallowed_market_cannot_be_enabled_by_cli_symbol(run_pipeline):
    assert run_pipeline('BUY', symbols=('UNAPPROVED',))['UNAPPROVED']['action'] == 'OBSERVATION_ONLY'
    assert FakeEngine.calls == []


@pytest.mark.parametrize('payout,buys', [(305.99, 0), (306, 1), (float('nan'), 0), (-1, 0)])
def test_payout_is_checked_before_buy(deriv, payout, buys):
    fake = deriv(BUY_OK)
    original_recv = fake.recv

    async def recv():
        value = json.loads(await original_recv())
        if 'proposal' in value:
            value['proposal']['payout'] = payout
        return json.dumps(value)

    fake.recv = recv
    result = _buy()
    assert len(fake.buys) == buys
    assert result['status'] == ('success' if buys else 'blocked')
    if not buys:
        assert _trades() == []
        assert _intents()[0].status == 'FAILED'


def test_signal_that_ages_during_quote_is_not_bought(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    monkeypatch.setattr(deriv_engine, '_now', lambda: CANDLE + timedelta(minutes=18))
    assert _buy()['status'] == 'blocked'
    assert fake.buys == []


def test_stale_balance_blocks_before_purchase(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    clock = {'time': 100.0}
    monkeypatch.setattr(deriv_engine.time, 'monotonic', lambda: clock['time'])
    original = fake.recv

    async def delayed():
        reply = await original()
        clock['time'] = 131.0
        return reply

    fake.recv = delayed
    assert _buy()['status'] == 'blocked'
    assert fake.buys == []


def test_own_pending_intent_does_not_hide_another_unresolved_order(deriv):
    fake = deriv(BUY_OK)
    original = fake.recv

    async def concurrent_intent():
        with SessionLocal() as db:
            db.add(TradeIntent(symbol='OTHER', side='BUY', candle_time=CANDLE, stake=25, status='PENDING'))
            db.commit()
        return await original()

    fake.recv = concurrent_intent
    assert _buy()['status'] == 'blocked'
    assert fake.buys == []


def test_disabled_engine_does_not_fetch_balance_or_buy(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    monkeypatch.setattr(settings, 'MACS_EXECUTION_ENABLED', False)
    assert _buy()['status'] == 'blocked'
    assert fake.sent == []
    assert _intents() == []


def test_duration_comes_from_the_market_rule(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    monkeypatch.setattr(settings, 'MACS_MARKET_RULES', {'OTC_DJI': MarketRule(enabled=True, duration_minutes=60)})
    assert _buy()['status'] == 'success'
    assert fake.sent[0]['duration'] == 60
    assert fake.sent[0]['duration_unit'] == 'm'
    assert _trades()[0].duration == 60


def test_dynamic_stake_reaches_broker_and_trade_record(deriv, monkeypatch):
    fake = deriv(lambda req: {'echo_req': req, 'msg_type': 'buy', 'req_id': 2,
                             'buy': {'contract_id': 900, 'buy_price': 12.5}})
    monkeypatch.setattr(DerivEngine, 'get_account_summary', lambda self: {'balance': 5000.0})
    result = DerivEngine().execute_signal('OTC_DJI', 'BUY', None, 45000, candle_time=CANDLE)
    assert result['status'] == 'success'
    assert fake.sent[0]['amount'] == 12.5
    assert fake.buys[0]['price'] == 12.5
    assert _trades()[0].quantity == 12.5


def test_real_endpoint_is_rejected_before_connection(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    reply = MagicMock()
    reply.json.return_value = {'data': {'url': 'wss://api.derivws.com/trading/v1/options/ws/real?otp=test'}}
    monkeypatch.setattr(deriv_engine.requests, 'post', lambda *a, **kw: reply)
    assert _buy()['status'] == 'blocked'
    assert fake.sent == []


@pytest.mark.usefixtures('clean_trading_tables')
def test_daily_budget_includes_proposed_stake_and_open_exposure():
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        db.add(PaperTrade(symbol='OTC_DJI', side='BUY', quantity=170, price=170,
                         status='CLOSED', pnl=-190, timestamp=now, closed_timestamp=now))
        db.commit()
    check = RiskManager().check_order(25, 10000, 'OTC_DJI', 'BUY', datetime.now(timezone.utc))
    assert not check['allowed'] and 'daily loss' in check['reason']


@pytest.mark.usefixtures('clean_trading_tables')
def test_settlement_after_midnight_counts_in_the_new_day():
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with SessionLocal() as db:
        db.add(PaperTrade(symbol='OTC_DJI', side='BUY', quantity=25, price=25, status='CLOSED', pnl=-25,
                         timestamp=midnight-timedelta(minutes=10), expiry_time=midnight+timedelta(minutes=5)))
        db.commit()
    assert RiskManager().daily_pnl == -25


@pytest.mark.usefixtures('clean_trading_tables')
def test_open_contract_count_and_exposure_are_enforced(monkeypatch):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(PaperTrade(symbol='OTC_DJI', side='BUY', quantity=90, price=90,
                         status='OPEN', expiry_time=now+timedelta(minutes=5)))
        db.commit()
    rm = RiskManager()
    check = rm.check_order(25, 10000, 'OTC_DJI', 'BUY', now)
    assert not check['allowed'] and 'Maximum open' in check['reason']
    monkeypatch.setattr(settings, 'MACS_MAX_OPEN_TRADES', 2)
    check = rm.check_order(25, 10000, 'OTC_DJI', 'BUY', now)
    assert not check['allowed'] and 'exposure' in check['reason']


@pytest.mark.usefixtures('clean_trading_tables')
def test_cooldown_is_persisted_across_worker_restarts():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(TradeIntent(symbol='OTC_DJI', side='BUY', candle_time=now, stake=25, status='EXECUTED'))
        db.commit()
    assert not RiskManager().check_order(25, 10000, 'OTC_DJI', 'BUY', now+timedelta(minutes=15), 1)['allowed']
    assert RiskManager().check_order(25, 10000, 'OTC_DJI', 'BUY', now+timedelta(minutes=30), 1)['allowed']


@pytest.mark.parametrize('balance,currency,request_id,allowed', [
    (10000, 'USD', 3, True), (0, 'USD', 3, False), ('nan', 'USD', 3, False),
    (10000, 'EUR', 3, False), (10000, 'USD', 999, False),
])
def test_authenticated_balance_validation(monkeypatch, balance, currency, request_id, allowed):
    response = MagicMock()
    response.json.return_value = {'data': {'url': 'wss://api.derivws.com/trading/v1/options/ws/demo?otp=test'}}
    monkeypatch.setattr(deriv_engine.requests, 'post', lambda *a, **kw: response)

    class Socket:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, payload):
            assert json.loads(payload) == {'balance': 1, 'req_id': 3}
        async def recv(self):
            return json.dumps({'msg_type': 'balance', 'req_id': request_id,
                               'balance': {'balance': balance, 'currency': currency}})

    monkeypatch.setattr(deriv_engine.websockets, 'connect', lambda *a, **kw: Socket())
    if allowed:
        assert DerivEngine().get_account_summary()['balance'] == 10000
    else:
        with pytest.raises(ValueError):
            DerivEngine().get_account_summary()
