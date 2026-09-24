"""`tenant_scope`: the one way tenant work reaches the RLS-protected `pipeline` schema.

Inside a scope the session's transaction runs as the `ptax_tenant` role with
`app.tenant_id` set, so every `pipeline` table shows and accepts only that tenant's rows.
The app's own user owns those tables (and is a superuser locally), and owners and
superusers bypass RLS -- hence the role switch rather than the setting alone.

Two ways a scope could quietly leak, both handled here:

- `SET LOCAL` reverts only at a real COMMIT or ROLLBACK. A savepoint release (a nested or
  test-fixture "commit") does not revert it, and a request may mix scoped and unscoped work
  in one transaction. So the scope resets role and setting itself on exit.
- A real commit *inside* a scope ends the transaction, and with it `SET LOCAL`. The session
  re-applies the scope at the start of every transaction while the scope is open.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, SessionTransaction

TENANT_ROLE = "ptax_tenant"
_SCOPE = "ptax.tenant_scope"


class TenantScopeError(RuntimeError):
    """A scope for one tenant was opened inside a scope for another."""


def _apply(connection: Connection, tenant_id: uuid.UUID) -> None:
    connection.execute(text(f"SET LOCAL ROLE {TENANT_ROLE}"))
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)}
    )


def _reapply_on_begin(
    session: Session, transaction: SessionTransaction, connection: Connection
) -> None:
    tenant_id = session.info.get(_SCOPE)
    if tenant_id is not None:
        _apply(connection, tenant_id)


@contextmanager
def tenant_scope(session: Session, tenant_id: uuid.UUID) -> Iterator[Session]:
    """Run the block as ``tenant_id``: `pipeline` rows of other tenants are invisible.

    Re-entering the same tenant's scope is a no-op; opening another tenant's scope inside
    one raises ``TenantScopeError`` rather than silently switching tenants mid-work.
    """
    current = session.info.get(_SCOPE)
    if current is not None:
        if current != tenant_id:
            raise TenantScopeError(f"a scope for tenant {current} is already open")
        yield session
        return

    _apply(session.connection(), tenant_id)
    session.info[_SCOPE] = tenant_id
    event.listen(session, "after_begin", _reapply_on_begin)
    try:
        yield session
    finally:
        session.info.pop(_SCOPE, None)
        event.remove(session, "after_begin", _reapply_on_begin)
        try:
            connection = session.connection()
            connection.execute(text("RESET ROLE"))
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        except DBAPIError:
            # The transaction is already aborted (the block failed mid-statement); rolling
            # it back is what ends `SET LOCAL`, and the caller's work is lost either way.
            session.rollback()
