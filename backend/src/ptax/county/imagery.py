"""Mosaic a county's cached ArcGIS orthophoto tiles into georeferenced GeoTIFF blocks.

County orthophotos are commonly published as an Esri tile cache (a MapServer with
``/tile/{level}/{row}/{col}``) rather than as downloadable GeoTIFFs, and GDAL cannot open
such a service directly. So the ingest does the tile math itself, from the service's
``tileInfo``: it takes the finest level, finds the tiles that touch the county's parcel
footprint, and writes them in square blocks of ``BLOCK_TILES`` x ``BLOCK_TILES`` tiles.
Each block becomes one stored COG, so a township whose parcels are scattered over a large
bounding box costs only the tiles its parcels touch, and an interrupted ingest resumes at
the next unstored block.
"""

import io
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import rasterio
import shapely
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.windows import Window
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from ptax.county.parcels import TIMEOUT_SECONDS, CountySourceError, arcgis_json

log = logging.getLogger("ptax.county")

#: Tiles per block side: 16 x 256 px at 0.15 m is a block about 610 m square.
BLOCK_TILES = 16
#: Pause between tile requests, so an ingest never hammers a county's server.
TILE_DELAY_SECONDS = 0.05
TILE_ATTEMPTS = 3
WEB_MERCATOR_WKIDS = (102100, 102113, 3857, 900913)


@dataclass(frozen=True)
class TileGrid:
    """The finest level of an Esri tile cache, in EPSG:3857."""

    level: int
    resolution: float
    origin_x: float
    origin_y: float
    tile_px: int

    @property
    def tile_m(self) -> float:
        return self.tile_px * self.resolution

    def tile_at(self, x: float, y: float) -> tuple[int, int]:
        """(row, col) of the tile containing the EPSG:3857 point."""
        col = math.floor((x - self.origin_x) / self.tile_m)
        row = math.floor((self.origin_y - y) / self.tile_m)
        return row, col

    def tile_box(self, row: int, col: int) -> BaseGeometry:
        x0 = self.origin_x + col * self.tile_m
        y0 = self.origin_y - row * self.tile_m
        return box(x0, y0 - self.tile_m, x0 + self.tile_m, y0)


def http_client() -> httpx.Client:
    """The client every tile request uses; tests replace it with a mock transport."""
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def tile_grid(client: httpx.Client, service_url: str) -> TileGrid:
    info = arcgis_json(client.get(service_url, params={"f": "json"}))
    tile_info = info.get("tileInfo")
    if not tile_info or not tile_info.get("lods"):
        raise CountySourceError(f"{service_url} is not a cached (tiled) map service")
    reference = tile_info.get("spatialReference") or {}
    if not {reference.get("wkid"), reference.get("latestWkid")} & set(WEB_MERCATOR_WKIDS):
        raise CountySourceError(f"{service_url}: only Web Mercator tile caches are supported")
    finest = min(tile_info["lods"], key=lambda lod: lod["resolution"])
    if tile_info["rows"] != tile_info["cols"]:
        raise CountySourceError(f"{service_url}: tiles are not square")
    return TileGrid(
        level=int(finest["level"]),
        resolution=float(finest["resolution"]),
        origin_x=float(tile_info["origin"]["x"]),
        origin_y=float(tile_info["origin"]["y"]),
        tile_px=int(tile_info["rows"]),
    )


def to_web_mercator(geometry_4326: BaseGeometry) -> BaseGeometry:
    to_3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform
    return shapely_transform(to_3857, geometry_4326)


def plan_blocks(
    grid: TileGrid, footprint_4326: BaseGeometry
) -> dict[tuple[int, int], list[tuple[int, int]]]:
    """The tiles touching the footprint, grouped by block: (block row, block col) -> tiles."""
    footprint = to_web_mercator(footprint_4326)
    shapely.prepare(footprint)
    minx, miny, maxx, maxy = footprint.bounds
    top, left = grid.tile_at(minx, maxy)
    bottom, right = grid.tile_at(maxx, miny)
    blocks: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for row in range(top, bottom + 1):
        for col in range(left, right + 1):
            if footprint.intersects(grid.tile_box(row, col)):
                blocks.setdefault((row // BLOCK_TILES, col // BLOCK_TILES), []).append((row, col))
    return blocks


def block_ref(service_url: str, grid: TileGrid, block: tuple[int, int]) -> str:
    """The stored asset's ``source_ref``: which service, level and block it was built from."""
    return f"arcgis:{service_url}:{grid.level}:block{block[0]}_{block[1]}"


def _decode(content: bytes) -> np.ndarray:
    """An RGB tile as (3, h, w) uint8, whatever format the cache served ("Mixed" caches mix
    JPEG and PNG). Transparent PNG pixels become nodata 0."""
    with Image.open(io.BytesIO(content)) as image:
        rgb = np.asarray(image.convert("RGB")).transpose(2, 0, 1).copy()
        if "A" in image.getbands() or "transparency" in image.info:
            alpha = np.asarray(image.convert("RGBA"))[:, :, 3]
            rgb[:, alpha == 0] = 0
    return rgb


def _fetch_tile(client: httpx.Client, service_url: str, level: int, row: int, col: int) -> bytes:
    """The tile's bytes, or ``b""`` when the cache has no tile there."""
    url = f"{service_url}/tile/{level}/{row}/{col}"
    for attempt in range(TILE_ATTEMPTS):
        try:
            response = client.get(url)
        except httpx.TransportError as exc:
            if attempt == TILE_ATTEMPTS - 1:
                raise CountySourceError(f"{url}: {exc}") from exc
        else:
            if response.status_code in (204, 404):
                return b""
            if response.status_code < 500:
                if response.status_code != 200:
                    raise CountySourceError(f"{url}: HTTP {response.status_code}")
                return response.content
            if attempt == TILE_ATTEMPTS - 1:
                raise CountySourceError(f"{url}: HTTP {response.status_code}")
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def write_block(
    client: httpx.Client,
    service_url: str,
    grid: TileGrid,
    block: tuple[int, int],
    tiles: list[tuple[int, int]],
    dst: Path,
) -> int:
    """Write one block as an EPSG:3857 GeoTIFF; return how many of ``tiles`` the cache had.

    Tiles outside ``tiles``, and tiles the cache lacks, stay nodata 0.
    """
    row0, col0 = block[0] * BLOCK_TILES, block[1] * BLOCK_TILES
    size = BLOCK_TILES * grid.tile_px
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 3,
        "width": size,
        "height": size,
        "crs": "EPSG:3857",
        "transform": from_origin(
            grid.origin_x + col0 * grid.tile_m,
            grid.origin_y - row0 * grid.tile_m,
            grid.resolution,
            grid.resolution,
        ),
        "nodata": 0,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "photometric": "RGB",
    }
    fetched = 0
    with rasterio.open(dst, "w", **profile) as out:
        for row, col in tiles:
            content = _fetch_tile(client, service_url, grid.level, row, col)
            time.sleep(TILE_DELAY_SECONDS)
            if not content:
                continue
            try:
                data = _decode(content)
            except OSError as exc:
                raise CountySourceError(f"tile {row}/{col} is not an image: {exc}") from exc
            if data.shape[1:] != (grid.tile_px, grid.tile_px):
                raise CountySourceError(f"tile {row}/{col} is {data.shape[2]}x{data.shape[1]} px")
            window = Window(
                (col - col0) * grid.tile_px, (row - row0) * grid.tile_px, grid.tile_px, grid.tile_px
            )
            out.write(data, window=window)
            fetched += 1
    return fetched
