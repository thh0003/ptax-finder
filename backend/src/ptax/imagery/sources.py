"""The imagery-source boundary (PRD: room for provider adapters later).

v1 has two members: ``NaipStacSource`` (Earth Search STAC + requester-pays S3) and
``FixtureNaipSource`` (committed synthetic COGs for local dev and tests). Both hand the
ingest job the same ``NaipItem``: an ``href`` rasterio can open inside ``gdal_env()``.
"""

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request
from pyproj import Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from ptax.config import Settings
from ptax.parcels.footprint import utm_epsg_for

# 8-bit DEFLATE imagery compresses to roughly half its raw size.
COMPRESSION_RATIO = 0.5


@dataclass(frozen=True)
class NaipItem:
    id: str
    year: int
    href: str
    geometry: BaseGeometry  # EPSG:4326
    gsd_m: float
    epsg: int
    width: int
    height: int

    @property
    def estimated_bytes(self) -> int:
        return int(self.width * self.height * 4 * COMPRESSION_RATIO)


@dataclass(frozen=True)
class NaipYear:
    year: int
    items: list[NaipItem]
    coverage_pct: int
    estimated_bytes: int
    gsd_m: float


class ImagerySource(Protocol):
    def list_years(self, footprint: BaseGeometry) -> list[NaipYear]: ...

    def items_for(self, year: int, footprint: BaseGeometry) -> list[NaipItem]: ...

    def gdal_env(self) -> dict[str, str]:
        """GDAL config needed to open the items' ``href`` values."""
        ...


def coverage_percent(footprint: BaseGeometry, geometries: list[BaseGeometry]) -> int:
    """Percent of the footprint covered by the union of ``geometries`` (all EPSG:4326)."""
    if not geometries:
        return 0
    to_utm = Transformer.from_crs(4326, utm_epsg_for(footprint), always_xy=True).transform
    fp = transform(to_utm, footprint)
    covered = transform(to_utm, unary_union(geometries))
    if fp.area == 0:
        return 0
    return int(round(100 * fp.intersection(covered).area / fp.area))


def group_years(items: list[NaipItem], footprint: BaseGeometry) -> list[NaipYear]:
    by_year: dict[int, list[NaipItem]] = {}
    for item in items:
        by_year.setdefault(item.year, []).append(item)
    years = []
    for year, year_items in sorted(by_year.items()):
        year_items.sort(key=lambda i: i.id)
        years.append(
            NaipYear(
                year=year,
                items=year_items,
                coverage_pct=coverage_percent(footprint, [i.geometry for i in year_items]),
                estimated_bytes=sum(i.estimated_bytes for i in year_items),
                gsd_m=max(i.gsd_m for i in year_items),
            )
        )
    return years


def get_naip_source(settings: Settings) -> ImagerySource:
    # Imported here: the concrete sources import this module for the dataclasses.
    from ptax.imagery.fixture import FixtureNaipSource
    from ptax.imagery.naip import NaipStacSource

    if settings.naip_source == "fixture":
        return FixtureNaipSource(settings.naip_fixture_dir)
    return NaipStacSource(settings.naip_stac_url, settings=settings)


def get_source_dependency(request: Request) -> ImagerySource:
    """FastAPI dependency; tests override it to inject a failing or recorded catalog."""
    return get_naip_source(request.app.state.settings)
