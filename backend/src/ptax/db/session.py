from collections.abc import Iterator
from functools import lru_cache

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session


@lru_cache
def get_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def get_db(request: Request) -> Iterator[Session]:
    engine = get_engine(request.app.state.settings.database_url)
    with Session(engine) as session:
        yield session
