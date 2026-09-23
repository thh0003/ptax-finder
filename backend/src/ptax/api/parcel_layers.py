import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ptax.auth.deps import CurrentUser, get_current_user, require_role
from ptax.db.models import ParcelLayer, Tenant, UserRole
from ptax.db.session import get_db
from ptax.jobs.queue import enqueue

router = APIRouter(prefix="/parcel-layers")
admin_only = require_role(UserRole.admin)


class ParcelLayerOut(BaseModel):
    id: uuid.UUID
    status: str
    original_filename: str
    s3_key: str
    parcel_id_field: str | None
    fields: list[dict[str, Any]] | None
    source_crs: str | None
    feature_count: int | None
    skipped_count: int | None
    error: str | None
    is_current: bool
    created_at: datetime


class ParcelLayerCreate(BaseModel):
    upload_key: str = Field(min_length=1, max_length=1024)
    original_filename: str = Field(min_length=1, max_length=255)


class IngestIn(BaseModel):
    parcel_id_field: str = Field(min_length=1, max_length=255)


def _out(layer: ParcelLayer, current_id: uuid.UUID | None) -> ParcelLayerOut:
    return ParcelLayerOut(
        id=layer.id,
        status=layer.status,
        original_filename=layer.original_filename,
        s3_key=layer.s3_key,
        parcel_id_field=layer.parcel_id_field,
        fields=layer.fields,
        source_crs=layer.source_crs,
        feature_count=layer.feature_count,
        skipped_count=layer.skipped_count,
        error=layer.error,
        is_current=layer.id == current_id,
        created_at=layer.created_at,
    )


def _get_layer(db: Session, user: CurrentUser, layer_id: uuid.UUID) -> ParcelLayer:
    layer = db.get(ParcelLayer, layer_id)
    if layer is None or layer.tenant_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel layer not found")
    return layer


@router.post("", response_model=ParcelLayerOut, status_code=status.HTTP_201_CREATED)
def create_layer(
    body: ParcelLayerCreate,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ParcelLayerOut:
    """Register an uploaded file as a parcel layer and queue its inspection."""
    expected_prefix = f"tenants/{user.tenant_id}/parcel_layer/"
    if not body.upload_key.startswith(expected_prefix):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "upload_key is not an upload of this tenant"
        )
    layer = ParcelLayer(
        tenant_id=user.tenant_id,
        uploaded_by=user.id,
        s3_key=body.upload_key,
        original_filename=body.original_filename,
        status="uploaded",
    )
    db.add(layer)
    db.flush()
    enqueue(db, "parcel_layer.inspect", user.tenant_id, {"layer_id": str(layer.id)})
    db.commit()
    db.refresh(layer)
    return _out(layer, None)


@router.get("", response_model=list[ParcelLayerOut])
def list_layers(
    user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ParcelLayerOut]:
    tenant = db.get_one(Tenant, user.tenant_id)
    stmt = (
        select(ParcelLayer)
        .where(ParcelLayer.tenant_id == user.tenant_id)
        .order_by(ParcelLayer.created_at.desc())
    )
    return [_out(layer, tenant.current_parcel_layer_id) for layer in db.execute(stmt).scalars()]


@router.get("/{layer_id}", response_model=ParcelLayerOut)
def get_layer(
    layer_id: uuid.UUID,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ParcelLayerOut:
    layer = _get_layer(db, user, layer_id)
    tenant = db.get_one(Tenant, user.tenant_id)
    return _out(layer, tenant.current_parcel_layer_id)


@router.post(
    "/{layer_id}/ingest", response_model=ParcelLayerOut, status_code=status.HTTP_202_ACCEPTED
)
def ingest_layer(
    layer_id: uuid.UUID,
    body: IngestIn,
    user: CurrentUser = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ParcelLayerOut:
    """Choose the parcel-ID field and queue the ingest job."""
    layer = _get_layer(db, user, layer_id)
    if layer.status != "awaiting_field":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"layer is {layer.status}, not awaiting_field"
        )
    known = {f["name"] for f in (layer.fields or [])}
    if body.parcel_id_field not in known:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"field {body.parcel_id_field!r} is not one of {sorted(known)}",
        )
    layer.parcel_id_field = body.parcel_id_field
    layer.status = "ingesting"
    enqueue(db, "parcel_layer.ingest", user.tenant_id, {"layer_id": str(layer.id)})
    db.commit()
    db.refresh(layer)
    tenant = db.get_one(Tenant, user.tenant_id)
    return _out(layer, tenant.current_parcel_layer_id)
