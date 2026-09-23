import uuid

import geopandas as gpd
import pytest
from shapely.geometry import MultiPolygon
from sqlalchemy import event
from sqlalchemy.orm import Session

from ptax.db.models import ParcelLayer, Tenant
from ptax.parcels.footprint import NoParcelLayer, footprint_for, utm_epsg_for
from tests.conftest import PARCELS_GEOJSON


def test_footprint_is_union_of_parcels_and_is_persisted(
    db: Session, tenant_with_admin, ingested_layer
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    expected = gpd.read_file(PARCELS_GEOJSON).union_all()

    # The ingest drain already ran imagery.recompute_coverage, which derives the footprint;
    # clear it so this test exercises the compute-and-persist path explicitly.
    layer = db.get_one(ParcelLayer, uuid.UUID(ingested_layer["id"]))
    layer.footprint = None
    db.commit()

    statements: list[str] = []
    event.listen(
        db.get_bind(), "before_cursor_execute", lambda *a: statements.append(a[2]), named=False
    )

    footprint = footprint_for(db, tenant)
    assert isinstance(footprint, MultiPolygon)
    assert abs(footprint.area - expected.area) < 1e-9
    assert footprint.symmetric_difference(expected).area < 1e-9

    db.refresh(layer)
    assert layer.footprint is not None
    first_union_count = sum("ST_Union" in s for s in statements)
    assert first_union_count == 1

    # Second call reads the stored geometry: no new ST_Union.
    again = footprint_for(db, tenant)
    assert again.equals_exact(footprint, 1e-12)
    assert sum("ST_Union" in s for s in statements) == first_union_count

    assert utm_epsg_for(footprint) == 32615


def test_footprint_requires_a_current_layer(db: Session, tenant_with_admin) -> None:
    with pytest.raises(NoParcelLayer):
        footprint_for(db, tenant_with_admin["tenant"])
