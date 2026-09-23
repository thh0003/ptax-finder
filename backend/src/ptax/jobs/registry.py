from collections.abc import Callable

from sqlalchemy.orm import Session

from ptax.db.models import Job

Handler = Callable[[Session, Job], None]

_handlers: dict[str, Handler] = {}


def job_handler(job_type: str) -> Callable[[Handler], Handler]:
    """Register ``fn`` as the handler for ``job_type`` (e.g. ``parcel_layer.inspect``)."""

    def decorate(fn: Handler) -> Handler:
        _handlers[job_type] = fn
        return fn

    return decorate


def get_handler(job_type: str) -> Handler | None:
    return _handlers.get(job_type)
