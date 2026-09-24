"""Read and write a tenant's configuration in `pipeline.tenant_configs`, under its scope."""

import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from ptax.db.tenancy import tenant_scope
from ptax.pipeline.models import TenantConfigRow
from ptax.tenancy.config import TenantConfig


def put_config(db: Session, tenant_id: uuid.UUID, config: TenantConfig) -> int:
    """Store ``config`` for the tenant (flushed, not committed); returns its new version."""
    with tenant_scope(db, tenant_id):
        row = db.get(TenantConfigRow, tenant_id)
        payload = config.model_dump(mode="json")
        if row is None:
            row = TenantConfigRow(tenant_id=tenant_id, config=payload, version=1)
            db.add(row)
        else:
            row.config = payload
            row.version += 1
            row.updated_at = func.now()
        db.flush()
        return row.version


def get_config(db: Session, tenant_id: uuid.UUID) -> TenantConfig | None:
    with tenant_scope(db, tenant_id):
        row = db.get(TenantConfigRow, tenant_id, populate_existing=True)
        return None if row is None else TenantConfig.model_validate(row.config)
