import json
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ptax import cli
from ptax.config import Settings
from ptax.county import parcels as county_parcels
from ptax.county.profile import load_profile
from ptax.db.models import Parcel, ParcelLayer, Tenant
from ptax.storage import get_s3_client

from .conftest import drain_queue

runner = CliRunner()

PROFILE_DIR = Path(__file__).parents[1] / "counties"
PARCEL_URL = "https://county.test/arcgis/rest/services/Tax_Parcels/FeatureServer/5"
AREA_URL = "https://county.test/arcgis/rest/services/Reference/MapServer/2"
ALLOWED = {
    "PIN",
    "year_built",
    "eff_year_built",
    "total_living_area",
    "gar_area",
    "det_gar_area",
    "PropClass",
}
PII = {"owner_name": "JANE DOE", "ADDR1": "1 SECRET LN", "prop_street": "2 PRIVATE RD"}

# The area is one 0.01-degree square; three parcels sit inside it, one well outside.
AREA = [[-89.60, 40.75], [-89.59, 40.75], [-89.59, 40.76], [-89.60, 40.76], [-89.60, 40.75]]


def _square(x: float, y: float, size: float = 0.001) -> dict:
    ring = [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
    return {"type": "Polygon", "coordinates": [ring]}


def _parcel(pin: str, x: float, y: float) -> dict:
    return {
        "type": "Feature",
        "geometry": _square(x, y),
        "properties": {
            "OBJECTID": int(pin[-1]),
            "PIN": pin,
            "year_built": 1978,
            "eff_year_built": 1990,
            "total_living_area": 1850,
            "gar_area": 480,
            "det_gar_area": 0,
            "PropClass": "0040",
            **PII,
        },
    }


PARCELS = [
    _parcel("14-01-000-001", -89.599, 40.751),
    _parcel("14-01-000-002", -89.597, 40.753),
    _parcel("14-01-000-003", -89.595, 40.755),
    _parcel("14-01-000-004", -89.50, 40.70),  # outside the area
    # The county stores a parcel split by a road as one feature per piece, on a later page.
    _parcel("14-01-000-002", -89.592, 40.758),
]


class FakeArcgis:
    """The two ArcGIS query endpoints the loader calls, recording each request."""

    def __init__(self, page_size: int) -> None:
        self.page_size = page_size
        self.parcel_requests: list[dict[str, list[str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == httpx.URL(f"{AREA_URL}/query").path:
            area = {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [AREA]},
                "properties": {"POL_TWP_NAME": "RICHWOODS"},
            }
            return httpx.Response(200, json={"type": "FeatureCollection", "features": [area]})
        if request.url.path == httpx.URL(f"{PARCEL_URL}/query").path:
            assert request.method == "POST", "the area geometry is too long for a URL"
            form = parse_qs(request.content.decode())
            self.parcel_requests.append(form)
            offset = int(form["resultOffset"][0])
            page = PARCELS[offset : offset + self.page_size]
            return httpx.Response(200, json={"type": "FeatureCollection", "features": page})
        return httpx.Response(404)


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "test-county.json"
    path.write_text(
        json.dumps(
            {
                "name": "Test County",
                "state": "IL",
                "parcels": {
                    "url": PARCEL_URL,
                    "id_field": "PIN",
                    "fields": {name: name for name in sorted(ALLOWED)},
                },
                "areas": {"richwoods": {"url": AREA_URL, "where": "POL_TWP_NAME = 'RICHWOODS'"}},
                "imagery": {},
            }
        )
    )
    return path


@pytest.fixture
def fake_arcgis(monkeypatch: pytest.MonkeyPatch) -> FakeArcgis:
    fake = FakeArcgis(page_size=2)
    monkeypatch.setattr(county_parcels, "PAGE_SIZE", 2)
    monkeypatch.setattr(
        county_parcels, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake))
    )
    return fake


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, db: Session, settings: Settings) -> Settings:
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_session", lambda: nullcontext(db))
    return settings


def test_county_parcels_ingests_only_area_parcels_and_mapped_fields(
    cli_db: Settings,
    db: Session,
    tenant_with_admin: dict,
    profile_path: Path,
    fake_arcgis: FakeArcgis,
) -> None:
    tenant: Tenant = tenant_with_admin["tenant"]
    db.commit()

    result = runner.invoke(
        cli.app,
        ["county-parcels", str(profile_path), "--area", "richwoods", "--tenant-fips", tenant.fips],
    )
    assert result.exit_code == 0, result.output

    # Paged across more than one response, asking only for the mapped fields.
    assert len(fake_arcgis.parcel_requests) >= 2
    for form in fake_arcgis.parcel_requests:
        assert set(form["outFields"][0].split(",")) == ALLOWED

    drain_queue(db)

    layer = db.execute(select(ParcelLayer).where(ParcelLayer.tenant_id == tenant.id)).scalar_one()
    assert layer.status == "ready", layer.error
    assert layer.parcel_id_field == "PIN"
    db.refresh(tenant)
    assert tenant.current_parcel_layer_id == layer.id

    stored = db.execute(select(Parcel).where(Parcel.layer_id == layer.id)).scalars().all()
    assert sorted(p.parcel_ref for p in stored) == [
        "14-01-000-001",
        "14-01-000-002",
        "14-01-000-003",
    ]
    # Both pieces of the split parcel are kept, as one multipolygon.
    split = next(p for p in stored if p.parcel_ref == "14-01-000-002")
    assert len(to_shape(split.geom).geoms) == 2
    for parcel in stored:
        assert set(parcel.attributes) == ALLOWED
        assert parcel.attributes["year_built"] == 1978

    uploaded = (
        get_s3_client(cli_db)
        .get_object(Bucket=cli_db.s3_bucket, Key=layer.s3_key)["Body"]
        .read()
        .decode()
    )
    for field, value in PII.items():
        assert field not in uploaded
        assert value not in uploaded


def test_county_parcels_refuses_an_unknown_area(
    cli_db: Settings, tenant_with_admin: dict, profile_path: Path, fake_arcgis: FakeArcgis
) -> None:
    fips = tenant_with_admin["tenant"].fips
    result = runner.invoke(
        cli.app, ["county-parcels", str(profile_path), "--area", "nowhere", "--tenant-fips", fips]
    )
    assert result.exit_code == 1
    assert "richwoods" in result.output
    assert fake_arcgis.parcel_requests == []


def test_peoria_profile_maps_only_the_allowed_fields() -> None:
    profile = load_profile(PROFILE_DIR / "peoria-il.json")
    assert set(profile.fields.values()) == ALLOWED
    assert profile.id_field == "PIN"
    assert profile.areas["richwoods"].where == "POL_TWP_NAME = 'RICHWOODS'"
    assert 2015 in profile.imagery
    assert not set(profile.fields) & set(PII)
