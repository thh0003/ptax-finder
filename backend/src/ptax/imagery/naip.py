"""NAIP discovery through the Earth Search STAC API.

Items carry ``naip:year``, ``gsd``, ``proj:epsg``, ``proj:shape`` and an ``image`` asset
under ``s3://naip-analytic/.../rgbir_cog/*.tif`` (4-band uint8 COG, requester pays,
us-west-2). Discovery itself needs no AWS credentials; only opening the hrefs does.
"""

from typing import Any

import httpx
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from ptax.config import Settings, get_settings
from ptax.imagery.gdal import naip_env
from ptax.imagery.sources import NaipItem, NaipYear, group_years

PAGE_LIMIT = 200
MAX_PAGES = 50  # 10,000 items: far more than any county's NAIP tiles across all years
TIMEOUT_SECONDS = 30.0


class CatalogError(Exception):
    """The STAC catalog could not be queried."""


class NaipStacSource:
    def __init__(
        self,
        stac_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._stac_url = stac_url.rstrip("/")
        self._transport = transport
        self._settings = settings

    def list_years(self, footprint: BaseGeometry) -> list[NaipYear]:
        return group_years(self._search(footprint), footprint)

    def items_for(self, year: int, footprint: BaseGeometry) -> list[NaipItem]:
        return sorted(
            (i for i in self._search(footprint, year=year) if i.year == year),
            key=lambda i: i.id,
        )

    def gdal_env(self) -> dict[str, str]:
        return naip_env(self._settings or get_settings())

    def _search(self, footprint: BaseGeometry, year: int | None = None) -> list[NaipItem]:
        minx, miny, maxx, maxy = footprint.bounds
        params: dict[str, Any] | None = {
            "bbox": f"{minx},{miny},{maxx},{maxy}",
            "limit": PAGE_LIMIT,
        }
        if year is not None:
            params["datetime"] = f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z"
        url: str | None = f"{self._stac_url}/collections/naip/items"
        items: list[NaipItem] = []
        pages = 0
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT_SECONDS) as client:
                while url:
                    pages += 1
                    if pages > MAX_PAGES:
                        raise CatalogError(f"more than {MAX_PAGES} result pages")
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    page = response.json()
                    for feature in page.get("features", []):
                        item = _parse_item(feature)
                        if item is not None and item.geometry.intersects(footprint):
                            items.append(item)
                    url = _next_link(page)
                    # The next link carries its own query; an empty dict would strip it.
                    params = None
        except httpx.HTTPError as exc:
            raise CatalogError(str(exc) or type(exc).__name__) from exc
        return items


def _next_link(page: dict[str, Any]) -> str | None:
    for link in page.get("links", []):
        if link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None


def _parse_item(feature: dict[str, Any]) -> NaipItem | None:
    props = feature.get("properties", {})
    image = feature.get("assets", {}).get("image", {})
    try:
        height, width = props["proj:shape"]
        return NaipItem(
            id=feature["id"],
            year=int(props["naip:year"]),
            href=image["href"],
            geometry=shape(feature["geometry"]),
            gsd_m=float(props["gsd"]),
            epsg=int(props["proj:epsg"]),
            width=int(width),
            height=int(height),
        )
    except (KeyError, TypeError, ValueError):
        # An item without the fields we need cannot be ingested; skip it rather than fail
        # discovery for the whole county.
        return None
