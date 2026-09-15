"""At most one MACS worker trades at a time.

Postgres: a session advisory lock held on its own connection for the whole
cycle. The server releases it when the process or connection dies, so a
crashed worker never leaves a stale lock. It needs a session: Supabase's
transaction pooler (port 6543) doesn't keep one, which is why
scripts/preflight.py refuses that port.

SQLite (local development): an exclusive flock on a file beside the database.
"""
import contextlib
import fcntl
import logging
import os

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Fixed key every MACS worker agrees on ("MACS-V2" as a 64-bit integer).
LOCK_KEY = 0x4D4143532D5632


@contextlib.contextmanager
def trading_lock(engine):
    """Yield True while this process holds the lock, False when another
    worker holds it. Raises if the lock can't be checked at all."""
    if engine.dialect.name == "postgresql":
        with _advisory_lock(engine) as acquired:
            yield acquired
    else:
        with _file_lock(engine.url.database) as acquired:
            yield acquired


@contextlib.contextmanager
def _advisory_lock(engine):
    conn = engine.connect()
    acquired = False
    try:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}).scalar())
        # The lock belongs to the session, not the transaction: committing
        # keeps it while not holding a transaction open all cycle.
        conn.commit()
        yield acquired
    finally:
        if acquired:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to release the trading lock; discarding the connection: {e}")
                # Never hand a lock-holding session back to the pool.
                conn.invalidate()
        conn.close()


@contextlib.contextmanager
def _file_lock(database):
    if not database or database == ":memory:":
        yield True
        return
    fd = os.open(f"{database}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        # Closing the descriptor releases the flock.
        os.close(fd)
