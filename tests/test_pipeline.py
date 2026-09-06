import pytest
from core.risk_management import RiskManager

def test_risk_consecutive_losses(monkeypatch):
    manager = RiskManager(db_path=":memory:")
    # We need to manually inject these into the manager to simulate the state
    import datetime
    manager.cooldown_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=4)
    status = manager.can_trade()
    assert status['allowed'] == False
    assert "Circuit breaker" in status['reason']
