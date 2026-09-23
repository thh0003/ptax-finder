"""``python -m ptax.migrate``: run Alembic to head under a Postgres advisory lock.

Only the API task sets ``PTAX_RUN_MIGRATIONS=1`` (the worker never migrates), and
the lock makes concurrent starts serialize instead of racing on DDL.
"""

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from ptax.config import get_settings

log = logging.getLogger("ptax.migrate")

# Arbitrary but fixed; every migrator must agree on it.
ADVISORY_LOCK_KEY = 7241001


def run_migrations(database_url: str, alembic_dir: Path | None = None) -> None:
    base = alembic_dir or Path.cwd()
    cfg = Config(str(base / "alembic.ini"))
    cfg.set_main_option("script_location", str(base / "alembic"))
    # Alembic's ConfigParser interpolates '%', which URL-encoded passwords contain.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

    engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as lock_conn:
        lock_conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        try:
            command.upgrade(cfg, "head")
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
    engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # alembic.ini's logging config sets the root level to WARN; keep our own lines visible.
    logging.getLogger("ptax").setLevel(logging.INFO)
    settings = get_settings()
    assert settings.database_url is not None  # composed by Settings' validator
    log.info("migrating database to head")
    run_migrations(settings.database_url)
    log.info("migrations complete")


if __name__ == "__main__":
    main()
