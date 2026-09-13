import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from config.settings import normalize_database_url
from models.database import Base, PaperTrade, SystemLog
from scripts.copy_database import copy_database

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# paper_trades / system_logs exactly as the pre-Alembic app left them in macs.db.
LEGACY_DDL = [
    """CREATE TABLE paper_trades (
        id INTEGER NOT NULL, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL,
        quantity FLOAT NOT NULL, price FLOAT NOT NULL, status VARCHAR NOT NULL,
        order_id VARCHAR, reason VARCHAR, pnl FLOAT, timestamp DATETIME,
        proposal_id VARCHAR, contract_id VARCHAR, result VARCHAR, payout FLOAT,
        closed_timestamp DATETIME, tech_score FLOAT, ai_score FLOAT, confidence FLOAT,
        regime VARCHAR, error_reason VARCHAR,
        PRIMARY KEY (id), UNIQUE (order_id))""",
    "CREATE INDEX ix_paper_trades_id ON paper_trades (id)",
    "CREATE INDEX ix_paper_trades_symbol ON paper_trades (symbol)",
    """CREATE TABLE system_logs (
        id INTEGER NOT NULL, timestamp DATETIME, symbol VARCHAR NOT NULL,
        tech_score FLOAT, ai_score FLOAT, combined_confidence FLOAT, regime VARCHAR,
        is_volatile INTEGER, signal VARCHAR NOT NULL, error_warning VARCHAR,
        PRIMARY KEY (id))""",
    "CREATE INDEX ix_system_logs_id ON system_logs (id)",
    "CREATE INDEX ix_system_logs_symbol ON system_logs (symbol)",
]


def _alembic(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["db_url"] = url
    return cfg


def test_migrated_empty_database_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    command.upgrade(_alembic(url), "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    engine.dispose()
    assert diff == []


def test_legacy_sqlite_upgrade_keeps_history_and_downgrades(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    for ddl in LEGACY_DDL:
        conn.execute(ddl)
    conn.execute(
        "INSERT INTO paper_trades (symbol, side, quantity, price, status, pnl) "
        "VALUES ('OTC_DJI', 'BUY', 170, 170, 'CLOSED', -170)"
    )
    conn.execute("INSERT INTO system_logs (symbol, signal, is_volatile) VALUES ('OTC_DJI', 'BUY', 1)")
    conn.commit()
    conn.close()

    url = f"sqlite:///{path}"
    cfg = _alembic(url)
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    assert {"paper_trades", "system_logs"}.isdisjoint(inspect(engine).get_table_names())
    with Session(engine) as db:
        trade = db.query(PaperTrade).one()
        assert (trade.pnl, trade.signal_id) == (-170.0, None)
        assert db.query(SystemLog).one().is_volatile is True
    engine.dispose()

    command.downgrade(cfg, "base")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT symbol, pnl FROM paper_trades").fetchall() == [("OTC_DJI", -170.0)]
    conn.close()


@pytest.mark.parametrize("raw, expected", [
    ("postgres://db.example:5432/app", "postgresql://db.example:5432/app?sslmode=require"),
    ("postgresql://db.example/app?application_name=macs", "postgresql://db.example/app?application_name=macs&sslmode=require"),
    ("postgresql://db.example/app?sslmode=disable", "postgresql://db.example/app?sslmode=disable"),
    ("sqlite:///macs.db", "sqlite:///macs.db"),
])
def test_normalize_database_url(raw, expected):
    assert normalize_database_url(raw) == expected


def test_copy_database_keeps_ids_and_refuses_a_second_copy(tmp_path):
    source, target = (f"sqlite:///{tmp_path / name}" for name in ("source.db", "target.db"))
    for url in (source, target):
        command.upgrade(_alembic(url), "head")

    engine = create_engine(source)
    with Session(engine) as db:
        db.add(SystemLog(id=7, symbol="OTC_DJI", signal="BUY"))
        db.flush()
        db.add(PaperTrade(symbol="OTC_DJI", side="BUY", quantity=170.0, price=170.0,
                          status="CLOSED", pnl=-170.0, signal_id=7))
        db.commit()
    engine.dispose()

    assert copy_database(source, target)["trades"] == 1
    engine = create_engine(target)
    with Session(engine) as db:
        assert db.query(PaperTrade).one().signal_id == 7
    engine.dispose()

    with pytest.raises(SystemExit, match="already has rows"):
        copy_database(source, target)
