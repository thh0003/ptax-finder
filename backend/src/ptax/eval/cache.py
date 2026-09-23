"""Local imagery cache: the NAIP items of an AOI, stored exactly as production stores them.

Each NAIP item is downloaded once, clipped to the AOI, and written through
``ptax.imagery.cog.to_cog`` — the same writer the ingest job uses. The harness then reads
parcels out of those files with ``read_parcel_uris``, so it is reading the artifact the
production pipeline would have stored, not an imitation of it.

That fidelity is the whole point, and an earlier per-parcel crop design failed it. A crop
of a few hundred pixels carries no overviews, while a real quarter-quad COG carries
``[2, 4, 8, 16, 32]``. Reading a 0.6 m year at a 1.0 m comparison grid resamples from the
``/2`` overview when one exists and from full resolution when it does not, so the crops
disagreed with the source on roughly 90% of samples — on the target year, whose built-up
fraction is the defect this harness exists to measure.

Storing whole items also keeps the cache small: an AOI is covered by a couple of
quarter-quads per year, not by one file per parcel.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import rasterio
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.warp import transform_bounds
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from ptax.eval.sources import NaipItem, SasSigner
from ptax.imagery.cog import to_cog

#: GDAL options for reading a signed Planetary Computer URL. Deliberately *without*
#: ``CPL_VSIL_CURL_ALLOWED_EXTENSIONS``: the signed href carries a query string, so an
#: extension allow-list would reject every blob URL.
REMOTE_ENV: dict[str, Any] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "VSI_CACHE": "TRUE",
}

#: Reading a plain local file needs no credentials or listing suppression.
LOCAL_ENV: dict[str, Any] = {}


@dataclass(frozen=True)
class CacheEntry:
    """One cached NAIP item: where it is, which catalog item it came from, and its grid."""

    path: str
    item_id: str
    year: int
    gsd_m: float
    epsg: int
    bounds_4326: tuple[float, float, float, float]


def entry_path(root: Path, item: NaipItem) -> Path:
    return root / f"{item.id}.tif"


#: Metres of imagery kept beyond the AOI box. Parcels are selected by *intersecting* that
#: box, so a parcel on the edge extends past it; clipping to the box exactly left those
#: parcels with no imagery over their far end. The largest eligible parcel is 40 000 m2
#: (about 200 m across), so this covers the worst case with room to spare.
AOI_CLIP_BUFFER_M = 300.0


def _clip_bounds(
    bbox_4326: tuple[float, float, float, float], epsg: int, buffer_m: float
) -> tuple[float, float, float, float]:
    to_native = Transformer.from_crs(4326, epsg, always_xy=True).transform
    minx, miny, maxx, maxy = box(*bbox_4326).bounds
    xs, ys = to_native([minx, maxx, minx, maxx], [miny, miny, maxy, maxy])
    return (
        min(xs) - buffer_m,
        min(ys) - buffer_m,
        max(xs) + buffer_m,
        max(ys) + buffer_m,
    )


def fetch_item(
    item: NaipItem,
    bbox_4326: tuple[float, float, float, float],
    destination: Path,
    signer: SasSigner,
    *,
    buffer_m: float = AOI_CLIP_BUFFER_M,
) -> CacheEntry:
    """Clip ``item`` to the buffered AOI and write it with the production COG writer."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    to_cog(
        signer.sign(item.href),
        destination,
        env=REMOTE_ENV,
        clip_bounds=_clip_bounds(bbox_4326, item.epsg, buffer_m),
    )
    # The written extent is what the reader may draw on, and `to_cog` snaps the clip to
    # whole source pixels and to the item's own footprint, so record what actually landed
    # rather than the requested box.
    with rasterio.open(destination) as written:
        west, south, east, north = transform_bounds(
            written.crs, CRS.from_epsg(4326), *written.bounds
        )
    return CacheEntry(
        path=str(destination),
        item_id=item.id,
        year=item.year,
        gsd_m=item.gsd_m,
        epsg=item.epsg,
        bounds_4326=(west, south, east, north),
    )


def uris_for(entries: dict[str, CacheEntry], year: int, geom_4326: BaseGeometry) -> list[str]:
    """Cached files of ``year`` whose footprint meets the parcel, mosaicked in item order.

    Mirrors ``ptax.detection.run._intersecting``: a parcel straddling two quarter-quads is
    read from both, in a stable order, exactly as the run job reads two stored assets.
    """
    envelope = box(*geom_4326.bounds)
    return [
        entry.path
        for _, entry in sorted(entries.items())
        if entry.year == year and box(*entry.bounds_4326).intersects(envelope)
    ]


def read_manifest(path: Path) -> dict[str, CacheEntry]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {
        key: CacheEntry(
            path=body["path"],
            item_id=body["item_id"],
            year=body["year"],
            gsd_m=body["gsd_m"],
            epsg=body["epsg"],
            bounds_4326=tuple(body["bounds_4326"]),  # type: ignore[arg-type]
        )
        for key, body in payload.items()
    }


def write_manifest(path: Path, entries: dict[str, CacheEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: asdict(entry) for key, entry in sorted(entries.items())}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def parcel_geometry(parcel: dict[str, Any]) -> BaseGeometry:
    return shape(parcel["geometry"])
