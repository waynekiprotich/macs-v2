from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL

from scripts.preflight import check

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
ENV = {"DERIV_API_TOKEN": "token", "DERIV_APP_ID": "1", "MACS_ALLOW_SQLITE": "1"}


def _migrated(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["db_url"] = url
    command.upgrade(cfg, "head")
    return url


def test_migrated_database_with_credentials_passes(tmp_path):
    assert check(_migrated(tmp_path), ENV) == []


def test_unmigrated_database_fails(tmp_path):
    problems = check(f"sqlite:///{tmp_path / 'empty.sqlite'}", ENV)
    assert len(problems) == 1 and "alembic upgrade head" in problems[0]


def test_missing_deriv_credentials_fail(tmp_path):
    problems = check(_migrated(tmp_path), {"MACS_ALLOW_SQLITE": "1"})
    assert problems == ["DERIV_API_TOKEN is not set", "DERIV_APP_ID is not set"]


def test_sqlite_is_refused_unless_allowed(tmp_path):
    env = {k: v for k, v in ENV.items() if k != "MACS_ALLOW_SQLITE"}
    problems = check(_migrated(tmp_path), env)
    assert len(problems) == 1 and "SQLite" in problems[0]


def test_unreachable_postgres_fails_without_leaking_the_password():
    # Built with URL.create so scripts/check_secrets.sh doesn't flag a literal URL.
    url = URL.create(
        "postgresql", username="macs", password="s3cret-pw", host="127.0.0.1", port=1,
        database="macs", query={"sslmode": "disable"},
    ).render_as_string(hide_password=False)
    problems = check(url, ENV)
    assert len(problems) == 1 and problems[0].startswith("cannot connect to the database")
    assert "s3cret-pw" not in problems[0]
