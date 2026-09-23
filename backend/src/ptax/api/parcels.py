import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.auth.deps import CurrentUser, get_current_user
from ptax.db.models import Parcel, Tenant
from ptax.db.session import get_db

router = APIRouter(prefix="/parcels")

MAX_FEATURES = 5000


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        minx, miny, maxx, maxy = (float(p) for p in value.split(","))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "bbox must be minx,miny,maxx,maxy"
        ) from exc
    if minx >= maxx or miny >= maxy:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "bbox is empty")
    return minx, miny, maxx, maxy


def _feature(row: Any) -> dict[str, Any]:
    parcel_id, layer_id, parcel_ref, attributes, geojson = row
    return {
        "type": "Feature",
        "id": str(parcel_id),
        "geometry": json.loads(geojson),
        "properties": {
            "parcel_ref": parcel_ref,
            "layer_id": str(layer_id),
            "attributes": attributes,
        },
    }


_COLUMNS = (
    Parcel.id,
    Parcel.layer_id,
    Parcel.parcel_ref,
    Parcel.attributes,
    func.ST_AsGeoJSON(Parcel.geom, 7),
)


@router.get("")
def parcels_in_bbox(
    bbox: str = Query(..., description="minx,miny,maxx,maxy in EPSG:4326"),
    limit: int = Query(MAX_FEATURES, ge=1, le=MAX_FEATURES),
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Current-layer parcels intersecting the bbox as a GeoJSON FeatureCollection."""
    minx, miny, maxx, maxy = _parse_bbox(bbox)
    tenant = db.get_one(Tenant, user.tenant_id)
    if tenant.current_parcel_layer_id is None:
        return {"type": "FeatureCollection", "features": []}

    envelope = func.ST_MakeEnvelope(minx, miny, maxx, maxy, 4326)
    filters = (
        Parcel.tenant_id == user.tenant_id,
        Parcel.layer_id == tenant.current_parcel_layer_id,
        Parcel.geom.intersects(envelope),
    )
    count = db.execute(select(func.count()).select_from(Parcel).where(*filters)).scalar_one()
    if count > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"{count} parcels in bbox exceeds limit {limit}"
        )
    rows = db.execute(select(*_COLUMNS).where(*filters).order_by(Parcel.parcel_ref)).all()
    return {"type": "FeatureCollection", "features": [_feature(r) for r in rows]}


@router.get("/by-ref/{parcel_ref}")
def parcel_by_ref(
    parcel_ref: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant = db.get_one(Tenant, user.tenant_id)
    row = db.execute(
        select(*_COLUMNS).where(
            Parcel.tenant_id == user.tenant_id,
            Parcel.layer_id == tenant.current_parcel_layer_id,
            Parcel.parcel_ref == parcel_ref,
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "parcel not found in current layer")
    return _feature(row)
