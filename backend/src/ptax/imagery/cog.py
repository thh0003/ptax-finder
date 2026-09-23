"""Raster inspection and Cloud Optimized GeoTIFF conversion.

Every stored asset is a uint8 RGB or RGB+NIR COG (DEFLATE, 512 px blocks, overviews,
nodata 0). ``to_cog`` streams window-by-window through a ``WarpedVRT`` so a full NAIP
quarter-quad never has to fit in memory; a ``clip_bounds`` window (source CRS) turns
the VRT into a pure clip.
"""

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.errors import RasterioIOError
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from rio_cogeo.cogeo import cog_translate, cog_validate
from rio_cogeo.profiles import cog_profiles
from shapely.geometry import box

from ptax.parcels.footprint import utm_epsg_for

MAX_BANDS = 4
MIN_BANDS = 3
SUPPORTED_DTYPES = ("uint8", "uint16")
STRETCH_PERCENTILES = (2, 98)
STRETCH_SAMPLE_PX = 512  # per side, read through overviews
WINDOW_PX = 2048


class RasterError(Exception):
    """A problem with a raster file that the admin needs to see."""


@dataclass(frozen=True)
class RasterInfo:
    epsg: int | None
    width: int
    height: int
    band_count: int
    dtype: str
    resolution_m: float
    bounds_4326: tuple[float, float, float, float]
    is_cog: bool


def inspect_raster(path: str, env: dict[str, str] | None = None) -> RasterInfo:
    """Read a raster's metadata; ``RasterError`` when it is not a georeferenced GeoTIFF."""
    try:
        with rasterio.Env(**(env or {})), rasterio.open(path) as src:
            if src.crs is None:
                raise RasterError("has no coordinate reference system")
            bounds_4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            return RasterInfo(
                epsg=src.crs.to_epsg(),
                width=src.width,
                height=src.height,
                band_count=src.count,
                dtype=str(src.dtypes[0]),
                resolution_m=_resolution_m(src, bounds_4326),
                bounds_4326=bounds_4326,
                is_cog=_is_cog(path, env),
            )
    except RasterioIOError as exc:
        raise RasterError("is not a readable GeoTIFF") from exc


def _is_cog(path: str, env: dict[str, str] | None) -> bool:
    with rasterio.Env(**(env or {})):
        try:
            return bool(cog_validate(path, quiet=True)[0])
        except Exception:  # noqa: BLE001 - any validation problem just means "not a COG"
            return False


def _resolution_m(src: rasterio.DatasetReader, bounds_4326: tuple) -> float:
    """Ground pixel size in metres, measured in the local UTM zone for geographic rasters."""
    if src.crs.is_projected:
        factor = src.crs.linear_units_factor[1] if src.crs.linear_units_factor else 1.0
        return round(max(abs(src.res[0]), abs(src.res[1])) * factor, 4)
    utm = utm_epsg_for(box(*bounds_4326))
    minx, miny, maxx, maxy = transform_bounds(src.crs, f"EPSG:{utm}", *src.bounds)
    return round(max((maxx - minx) / src.width, (maxy - miny) / src.height), 4)


def to_cog(
    src_path: str,
    dst_path: Path,
    *,
    env: dict[str, str] | None = None,
    clip_bounds: tuple[float, float, float, float] | None = None,
) -> None:
    """Write ``src_path`` (optionally clipped, in its own CRS) as a uint8 RGB[+NIR] COG."""
    with rasterio.Env(**(env or {})), rasterio.open(src_path) as src:
        if src.count < MIN_BANDS:
            raise RasterError(f"has {src.count} band(s); 3 or 4 required")
        dtype = str(src.dtypes[0])
        if dtype not in SUPPORTED_DTYPES:
            raise RasterError(f"{dtype} pixels are not supported (uint8/uint16 only)")
        bands = min(src.count, MAX_BANDS)
        with WarpedVRT(src, **_clip_kwargs(src, clip_bounds)) as vrt:
            plain = dst_path.with_suffix(".plain.tif")
            _write_uint8(vrt, plain, bands, _stretch(src, bands) if dtype == "uint16" else None)
        try:
            profile = cog_profiles.get("deflate")
            # Without an explicit photometric a 4th uint8 band would be tagged as alpha.
            profile.update(blockxsize=512, blockysize=512, photometric="RGB")
            cog_translate(plain, dst_path, profile, nodata=0, in_memory=False, quiet=True)
        finally:
            plain.unlink(missing_ok=True)


def _clip_kwargs(
    src: rasterio.DatasetReader, clip_bounds: tuple[float, float, float, float] | None
) -> dict:
    kwargs: dict = {"crs": src.crs}
    if clip_bounds is None:
        return kwargs
    minx, miny, maxx, maxy = clip_bounds
    sminx, sminy, smaxx, smaxy = src.bounds
    minx, maxx = max(minx, sminx), min(maxx, smaxx)
    miny, maxy = max(miny, sminy), min(maxy, smaxy)
    if minx >= maxx or miny >= maxy:
        raise RasterError("does not intersect the requested area")
    # Snap to whole source pixels so the clip keeps the exact source resolution.
    window = from_bounds(minx, miny, maxx, maxy, src.transform)
    col_off, row_off = int(math.floor(window.col_off)), int(math.floor(window.row_off))
    width = max(1, int(math.ceil(window.col_off + window.width)) - col_off)
    height = max(1, int(math.ceil(window.row_off + window.height)) - row_off)
    window = Window(col_off, row_off, width, height)
    kwargs.update(transform=src.window_transform(window), width=width, height=height)
    return kwargs


def _write_uint8(
    vrt: WarpedVRT, path: Path, bands: int, scales: list[tuple[int, int]] | None
) -> None:
    """Stream the VRT into a tiled uint8 GeoTIFF, stretching 16-bit sources per band."""
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": bands,
        "width": vrt.width,
        "height": vrt.height,
        "crs": vrt.crs,
        "transform": vrt.transform,
        "nodata": 0,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "photometric": "RGB",
    }
    indexes = list(range(1, bands + 1))
    with rasterio.open(path, "w", **profile) as dst:
        for row in range(0, vrt.height, WINDOW_PX):
            for col in range(0, vrt.width, WINDOW_PX):
                window = Window(
                    col, row, min(WINDOW_PX, vrt.width - col), min(WINDOW_PX, vrt.height - row)
                )
                data = vrt.read(indexes=indexes, window=window)
                if scales is not None:
                    data = _apply_stretch(data, scales)
                dst.write(data.astype(np.uint8), window=window)
        if bands == 4:
            dst.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.undefined,
            )


def _apply_stretch(data: np.ndarray, scales: list[tuple[int, int]]) -> np.ndarray:
    out = np.empty(data.shape, dtype=np.uint8)
    for b, (low, high) in enumerate(scales):
        band = data[b].astype(np.float32)
        scaled = (band - low) * (254.0 / (high - low)) + 1.0  # keep 0 for nodata
        out[b] = np.clip(np.rint(scaled), 1, 255).astype(np.uint8)
        out[b][data[b] == 0] = 0
    return out


def _stretch(src: rasterio.DatasetReader, bands: int) -> list[tuple[int, int]]:
    """Per-band (low, high) source values mapped onto 1-255, from a coarse sample."""
    factor = max(1, min(src.width, src.height) // STRETCH_SAMPLE_PX)
    sample = src.read(
        indexes=list(range(1, bands + 1)),
        out_shape=(bands, max(1, src.height // factor), max(1, src.width // factor)),
    )
    scales = []
    for b in range(bands):
        values = sample[b][sample[b] > 0]
        if values.size == 0:
            scales.append((0, 65535))
            continue
        low, high = np.percentile(values, STRETCH_PERCENTILES)
        scales.append((int(low), max(int(high), int(low) + 1)))
    return scales


def bounds_to_crs(
    bounds_4326: tuple[float, float, float, float], epsg: int
) -> tuple[float, float, float, float]:
    """A 4326 bbox in another CRS (bbox of the transformed edges)."""
    return transform_bounds("EPSG:4326", f"EPSG:{epsg}", *bounds_4326, densify_pts=21)
