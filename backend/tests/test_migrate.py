import threading
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url

from ptax.config import Settings
from ptax.db.session import get_engine
from ptax.migrate import run_migrations

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_password_with_percent_encoded_characters_reaches_alembic(settings: Settings) -> None:
    """Aurora-generated passwords contain '=' etc.; URL-encoding yields '%', which Alembic's
    ConfigParser rejects unless escaped."""
    engine = get_engine(settings.database_url)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP ROLE IF EXISTS ptax_pct"))
        conn.execute(text("CREATE ROLE ptax_pct WITH LOGIN SUPERUSER PASSWORD 'pu=s=d%5'"))
    try:
        url = make_url(settings.database_url).set(username="ptax_pct", password="pu=s=d%5")
        run_migrations(url.render_as_string(hide_password=False), BACKEND_DIR)
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("DROP ROLE IF EXISTS ptax_pct"))


def test_concurrent_migrators_both_finish_at_head(settings: Settings) -> None:
    errors: list[BaseException] = []

    def go() -> None:
        try:
            run_migrations(settings.database_url, BACKEND_DIR)
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    with get_engine(settings.database_url).connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        # Nobody left the lock held.
        held = conn.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = 7241001")
        ).scalar_one()
    assert current == head
    assert held == 0
