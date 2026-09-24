import io
import json
import re
from contextlib import nullcontext
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ptax import cli
from ptax.config import Settings
from ptax.county import imagery as county_imagery
from ptax.db.models import ImageryAsset, ImageryYear
from ptax.jobs.queue import enqueue

from .conftest import drain_queue

runner = CliRunner()

SERVICE = "https://county.test/arcgis/rest/services/RL/Orthos2015/MapServer"
ORIGIN_X, ORIGIN_Y = -20037508.342787, 20037508.342787
TILE_PX = 256
#: The fake cache's finest level. Coarse, so the fixture county spans only a few tiles.
LEVEL, RES = 3, 2.0


def colour_for(row: int, col: int) -> tuple[int, int, int]:
    return (30 + row * 37 % 200, 30 + col * 53 % 200, 128)


def is_jpeg(col: int) -> bool:
    """The cache is "Mixed": alternate columns are served as JPEG and PNG."""
    return col % 2 == 0


class FakeTileCache:
    """An Esri MapServer tile cache: service JSON plus /tile/{level}/{row}/{col}."""

    def __init__(self, missing: frozenset[tuple[int, int]] = frozenset()) -> None:
        self.missing = missing
        self.tile_requests: list[tuple[int, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == httpx.URL(SERVICE).path:
            lods = [
                {"level": 1, "resolution": 8.0, "scale": 1},
                {"level": LEVEL, "resolution": RES, "scale": 1},
            ]
            return httpx.Response(
                200,
                json={
                    "tileInfo": {
                        "rows": TILE_PX,
                        "cols": TILE_PX,
                        "origin": {"x": ORIGIN_X, "y": ORIGIN_Y},
                        "spatialReference": {"wkid": 102100, "latestWkid": 3857},
                        "lods": lods,
                    }
                },
            )
        match = re.fullmatch(r".*/tile/(\d+)/(\d+)/(\d+)", path)
        if match is None:
            return httpx.Response(404)
        level, row, col = (int(g) for g in match.groups())
        assert level == LEVEL, "tiles come from the finest level"
        self.tile_requests.append((row, col))
        if (row, col) in self.missing:
            return httpx.Response(404)
        image = Image.new("RGB", (TILE_PX, TILE_PX), colour_for(row, col))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG" if is_jpeg(col) else "PNG")
        # The server's content type is not trusted for the format.
        return httpx.Response(200, content=buffer.getvalue(), headers={"Content-Type": "image/x"})


def _mock(monkeypatch: pytest.MonkeyPatch, fake: FakeTileCache) -> None:
    monkeypatch.setattr(county_imagery, "TILE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(county_imagery, "BLOCK_TILES", 2)
    monkeypatch.setattr(
        county_imagery, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake))
    )


def test_block_mosaic_is_georeferenced_and_decodes_mixed_tiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeTileCache(missing=frozenset({(1001, 2001)}))
    _mock(monkeypatch, fake)
    with county_imagery.http_client() as client:
        grid = county_imagery.tile_grid(client, SERVICE)
        assert (grid.level, grid.resolution) == (LEVEL, RES)
        tiles = [(1000, 2000), (1000, 2001), (1001, 2000), (1001, 2001)]
        dst = tmp_path / "block.tif"
        fetched = county_imagery.write_block(client, SERVICE, grid, (500, 1000), tiles, dst)
    assert fetched == 3

    with rasterio.open(dst) as src:
        assert src.crs.to_epsg() == 3857
        assert (src.width, src.height) == (2 * TILE_PX, 2 * TILE_PX)
        # The centre of pixel (row 10, col 300) lies in tile (1000, 2001).
        x, y = src.xy(10, 300)
        assert x == pytest.approx(ORIGIN_X + (2000 * TILE_PX + 300 + 0.5) * RES, abs=RES / 2)
        assert y == pytest.approx(ORIGIN_Y - (1000 * TILE_PX + 10 + 0.5) * RES, abs=RES / 2)
        data = src.read()

    def pixel(row: int, col: int) -> np.ndarray:
        return data[:, row, col].astype(int)

    # JPEG is lossy, PNG exact; the 404 tile is nodata.
    assert np.abs(pixel(10, 10) - colour_for(1000, 2000)).max() <= 3
    assert tuple(pixel(10, 300)) == colour_for(1000, 2001)
    assert np.abs(pixel(300, 10) - colour_for(1001, 2000)).max() <= 3
    assert tuple(pixel(300, 300)) == (0, 0, 0)


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, db: Session, settings: Settings) -> Settings:
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_session", lambda: nullcontext(db))
    return settings


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    path = tmp_path / "test-county.json"
    path.write_text(
        json.dumps(
            {
                "name": "Test County",
                "state": "MN",
                "parcels": {
                    "url": "https://county.test/p",
                    "id_field": "PIN",
                    "fields": {"PIN": "PIN"},
                },
                "areas": {},
                "imagery": {"2015": SERVICE},
            }
        )
    )
    return path


def _county_imagery(profile_path: Path, fips: str, year: int = 2015) -> None:
    args = ["county-imagery", str(profile_path), "--year", str(year), "--tenant-fips", fips]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output


def test_county_imagery_stores_blocks_covering_the_county_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
    cli_db: Settings,
    db: Session,
    tenant_with_admin: dict,
    ingested_layer: dict,
    profile_path: Path,
) -> None:
    fake = FakeTileCache()
    _mock(monkeypatch, fake)
    tenant = tenant_with_admin["tenant"]
    db.commit()

    _county_imagery(profile_path, tenant.fips)
    drain_queue(db)

    year = db.execute(
        select(ImageryYear).where(ImageryYear.tenant_id == tenant.id, ImageryYear.year == 2015)
    ).scalar_one()
    assert year.status == "ready", year.error
    assert (year.source, year.provider) == ("arcgis", SERVICE)
    assert year.resolution_m == pytest.approx(RES)
    assert year.coverage_pct == 100
    assert year.parcels_uncovered == 0
    assets = db.execute(select(ImageryAsset).where(ImageryAsset.year_id == year.id)).scalars().all()
    assert len(assets) >= 2 and all(a.status == "ready" for a in assets)
    assert all(a.source_ref.startswith(f"arcgis:{SERVICE}:{LEVEL}:block") for a in assets)
    first_run = set(fake.tile_requests)

    # Tiles are requested only where the county's parcels are, never twice.
    assert len(fake.tile_requests) == len(first_run)

    # Resume after an interruption that lost one block: only that block is fetched again.
    lost = assets[0]
    db.delete(lost)
    year.status = "processing"
    enqueue(db, "imagery.ingest_arcgis", tenant.id, {"year_id": str(year.id)})
    db.commit()
    fake.tile_requests.clear()
    drain_queue(db)

    db.refresh(year)
    assert year.status == "ready", year.error
    refetched = set(fake.tile_requests)
    assert refetched and refetched < first_run
    restored = (
        db.execute(select(ImageryAsset).where(ImageryAsset.year_id == year.id)).scalars().all()
    )
    assert sorted(a.source_ref for a in restored) == sorted(a.source_ref for a in assets)


def test_county_imagery_refuses_a_year_the_profile_lacks(
    cli_db: Settings, tenant_with_admin: dict, profile_path: Path
) -> None:
    args = ["county-imagery", str(profile_path), "--year", "1999", "--tenant-fips", "27053"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "2015" in result.output
