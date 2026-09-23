import json
from pathlib import Path

import httpx
import pytest
import rasterio
from fastapi.testclient import TestClient
from rio_cogeo.cogeo import cog_validate
from shapely.geometry import MultiPolygon
from sqlalchemy.orm import Session

from ptax.imagery.fixture import FixtureNaipSource
from ptax.imagery.naip import NaipStacSource
from ptax.imagery.sources import get_naip_source
from ptax.parcels.footprint import footprint_for
from tests.conftest import FIXTURES_DIR

IMAGERY_DIR = FIXTURES_DIR / "imagery"
STAC_PAGES = json.loads((FIXTURES_DIR / "stac_naip_pages.json").read_text())
STAC_URL = "https://earth-search.aws.element84.com/v1"


class RecordingTransport(httpx.MockTransport):
    """Serves the two recorded Earth Search pages; page 2 only via the ``next`` link."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path != "/v1/collections/naip/items":
            return httpx.Response(404)
        page = "page2" if request.url.params.get("next") == "page2" else "page1"
        return httpx.Response(200, json=STAC_PAGES[page])


@pytest.fixture
def fixture_footprint(db: Session, tenant_with_admin, ingested_layer) -> MultiPolygon:
    return footprint_for(db, tenant_with_admin["tenant"])


def test_stac_source_groups_items_by_year_and_pages(fixture_footprint: MultiPolygon) -> None:
    transport = RecordingTransport()
    source = NaipStacSource(STAC_URL, transport=transport)

    years = source.list_years(fixture_footprint)

    assert [y.year for y in years] == [2021, 2023]
    # The recorded pages hold the SW and SE quarter-quads for each year, but only the SW
    # one touches the fixture parcels: the SE quad is filtered out by geometry.
    expected_ids = {
        2021: ["mn_m_4509359_sw_15_060_20210618"],
        2023: ["mn_m_4509359_sw_15_030_20230731"],
    }
    for year in years:
        assert [i.id for i in year.items] == expected_ids[year.year]
        assert year.coverage_pct == 100
        assert year.estimated_bytes == sum(i.height * i.width * 2 for i in year.items)
        assert year.items[0].href.startswith("s3://naip-analytic/mn/")
    assert years[0].gsd_m == 0.6 and years[1].gsd_m == 0.3
    # Two requests: the first page, then the `next` link.
    assert len(transport.requests) == 2
    assert transport.requests[0].url.params["bbox"].startswith("-93.7")
    assert transport.requests[1].url.params["next"] == "page2"
    naip_session = source.gdal_env()["session"].get_credential_options()
    assert naip_session["AWS_REQUEST_PAYER"] == "requester"
    assert naip_session["AWS_REGION"] == "us-west-2"


def test_stac_items_for_a_single_year(fixture_footprint: MultiPolygon) -> None:
    source = NaipStacSource(STAC_URL, transport=RecordingTransport())
    items = source.items_for(2023, fixture_footprint)
    assert [i.year for i in items] == [2023]
    assert items[0].epsg == 26915 and items[0].gsd_m == 0.3
    assert source.items_for(1999, fixture_footprint) == []


def test_fixture_source_lists_committed_years(fixture_footprint: MultiPolygon) -> None:
    source = FixtureNaipSource(IMAGERY_DIR)
    years = source.list_years(fixture_footprint)
    assert [y.year for y in years] == [2021, 2023]
    for year in years:
        assert year.coverage_pct == 100
        assert len(year.items) == 1
        assert Path(year.items[0].href).exists()
        assert year.items[0].gsd_m == 1.0
    assert source.gdal_env() == {}
    # ortho_2025_partial.tif does not match naip_<year>.tif and is not a NAIP year.
    assert source.items_for(2025, fixture_footprint) == []


def test_get_naip_source_honours_settings(settings) -> None:
    assert isinstance(get_naip_source(settings), FixtureNaipSource)
    stac = get_naip_source(settings.model_copy(update={"naip_source": "stac"}))
    assert isinstance(stac, NaipStacSource)


def test_fixture_rasters_are_small_valid_cogs() -> None:
    for name, bands, res, epsg in [
        ("naip_2021.tif", 4, 1.0, 26915),
        ("naip_2023.tif", 4, 1.0, 26915),
        ("ortho_2025_partial.tif", 3, 0.5, 3857),
    ]:
        path = IMAGERY_DIR / name
        assert path.stat().st_size < 1_000_000
        assert cog_validate(str(path))[0]
        with rasterio.open(path) as src:
            assert src.count == bands
            assert src.res == (res, res)
            assert src.crs.to_epsg() == epsg
            assert "alpha" not in [c.name for c in src.colorinterp]


def test_naip_available_endpoint(
    client: TestClient, db: Session, tenant_with_admin, ingested_layer, make_token
) -> None:
    headers = tenant_with_admin["admin_headers"]
    response = client.get("/api/imagery/naip/available", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [y["year"] for y in body] == [2021, 2023]
    with rasterio.open(IMAGERY_DIR / "naip_2021.tif") as src:
        expected_gb = src.width * src.height * 4 * 0.5 / 1e9
    assert body[0] == {
        "year": 2021,
        "item_count": 1,
        "coverage_pct": 100,
        "estimated_gb": pytest.approx(expected_gb, rel=1e-3),
        "gsd_m": 1.0,
        "existing": None,
    }
    # Reviewers can read availability too.
    assert (
        client.get(
            "/api/imagery/naip/available", headers=tenant_with_admin["reviewer_headers"]
        ).status_code
        == 200
    )


def test_naip_available_without_layer_is_409(client: TestClient, tenant_with_admin) -> None:
    response = client.get("/api/imagery/naip/available", headers=tenant_with_admin["admin_headers"])
    assert response.status_code == 409
    assert "no parcel layer" in response.json()["detail"]


def test_naip_available_catalog_failure_is_502(
    app, client: TestClient, tenant_with_admin, ingested_layer
) -> None:
    from ptax.imagery.sources import get_source_dependency

    def boom() -> NaipStacSource:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("catalog down")

        return NaipStacSource(STAC_URL, transport=httpx.MockTransport(handler))

    app.dependency_overrides[get_source_dependency] = boom
    response = client.get("/api/imagery/naip/available", headers=tenant_with_admin["admin_headers"])
    assert response.status_code == 502
    assert "NAIP catalog unavailable" in response.json()["detail"]
