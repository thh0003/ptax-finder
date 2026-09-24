"""The county footprint: the union of the current parcel layer's parcels.

Plan A left this derived. It is persisted on ``parcel_layers.footprint`` the first time
it is needed because a 100k-parcel union takes seconds and NAIP discovery, both ingest
jobs, and the coverage report all need it.
"""

import math
from typing import cast

import shapely
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ptax.db.models import Parcel, ParcelLayer, Tenant


class NoParcelLayer(LookupError):
    """The tenant has no current parcel layer, so there is no footprint."""


def footprint_for(db: Session, tenant: Tenant) -> shapely.MultiPolygon:
    if tenant.current_parcel_layer_id is None:
        raise NoParcelLayer("no parcel layer")
    layer = db.get_one(ParcelLayer, tenant.current_parcel_layer_id)
    if layer.footprint is None:
        union = db.execute(
            select(func.ST_Multi(func.ST_Buffer(func.ST_Union(Parcel.geom), 0))).where(
                Parcel.layer_id == layer.id
            )
        ).scalar_one()
        if union is None:
            raise NoParcelLayer("parcel layer has no parcels")
        layer.footprint = union
        db.commit()
    geom = to_shape(cast(WKBElement, layer.footprint))
    if geom.geom_type == "Polygon":
        geom = shapely.MultiPolygon([geom])
    return geom


def utm_epsg_for(geom: BaseGeometry) -> int:
    """EPSG of the WGS84 / UTM zone (northern hemisphere) containing the centroid."""
    lon = geom.centroid.x
    zone = int(math.floor((lon + 180) / 6)) + 1
    return 32600 + zone


def as_wkb_element(geom: BaseGeometry):  # noqa: ANN201 - geoalchemy2 WKBElement
    return from_shape(geom, srid=4326)
