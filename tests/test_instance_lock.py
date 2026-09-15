from types import SimpleNamespace

from core.instance_lock import trading_lock
from models.database import engine


def test_sqlite_lock_admits_one_worker_at_a_time():
    with trading_lock(engine) as first:
        assert first is True
        with trading_lock(engine) as second:
            assert second is False
    with trading_lock(engine) as again:
        assert again is True


class FakeConnection:
    def __init__(self, acquired, unlock_fails=False):
        self.acquired, self.unlock_fails = acquired, unlock_fails
        self.statements, self.invalidated, self.closed = [], False, False

    def execute(self, statement, params):
        sql = str(statement)
        self.statements.append(sql)
        if "unlock" in sql and self.unlock_fails:
            raise RuntimeError("connection lost")
        return SimpleNamespace(scalar=lambda: self.acquired)

    def commit(self):
        pass

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.closed = True


def _postgres(conn):
    return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: conn)


def test_postgres_advisory_lock_is_released_after_the_cycle():
    conn = FakeConnection(acquired=True)
    with trading_lock(_postgres(conn)) as acquired:
        assert acquired is True
    assert any("pg_try_advisory_lock" in s for s in conn.statements)
    assert any("pg_advisory_unlock" in s for s in conn.statements)
    assert conn.closed and not conn.invalidated


def test_postgres_lock_held_elsewhere_is_reported_and_not_unlocked():
    conn = FakeConnection(acquired=False)
    with trading_lock(_postgres(conn)) as acquired:
        assert acquired is False
    assert not any("pg_advisory_unlock" in s for s in conn.statements)
    assert conn.closed


def test_connection_that_cannot_unlock_is_discarded_not_pooled():
    conn = FakeConnection(acquired=True, unlock_fails=True)
    with trading_lock(_postgres(conn)):
        pass
    assert conn.invalidated and conn.closed
