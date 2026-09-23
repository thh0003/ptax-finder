"""Generate synthetic imagery over the Plan A parcel grid (see make_fixtures.py).

    uv run python tests/fixtures/make_imagery_fixtures.py        # into tests/fixtures/imagery/

Writes three small Cloud Optimized GeoTIFFs, deterministically (no randomness):

- ``naip_2021.tif``  EPSG:26915, 1.0 m/px, 4 bands (RGB+NIR): green field texture with
  "existing" roofs on parcels 5 and 10.
- ``naip_2023.tif``  same, plus new roofs on parcels 3, 7 and 12, and a ``RECAPTURE_GAIN``
  applied so the year pair differs radiometrically the way two real captures do.
- ``ortho_2025_partial.tif``  EPSG:3857, 0.5 m/px, 3 bands, covering only grid columns
  0-2 (the western 60%): the 2023 scene plus a new roof on parcel 1.

Parcel *n* (1-based, PIN ``27-053-{n:06d}``) sits at row/col ``divmod(n - 1, 5)`` of the
grid; roofs are 20 m x 15 m rectangles centred in the parcel. The detector tests and the
E2E scenarios rely on exactly these parcel numbers.
"""

import argparse
import math
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

ORIGIN_LON, ORIGIN_LAT = -93.70, 45.05  # must match make_fixtures.py
CELL_DEG = 0.0015
PARCEL_FRACTION = 0.9
COLS = 5
MARGIN_M = 40.0

# Approximate metres per degree at 45 N, used only to size the roofs in degrees.
M_PER_DEG_LAT = 111_000.0
M_PER_DEG_LON = 78_800.0
ROOF_W_M, ROOF_H_M = 20.0, 15.0

FIELD = (70, 120, 60, 180)
ROOF = (150, 150, 150, 90)
TEXTURE_AMPLITUDE = 4

EXISTING_ROOFS = (5, 10)
NEW_ROOFS_2023 = (3, 7, 12)
NEW_ROOFS_2025 = (1,)

MAX_FILE_BYTES = 1_000_000


def parcel_bounds(n: int) -> tuple[float, float, float, float]:
    r, c = divmod(n - 1, COLS)
    minx = ORIGIN_LON + c * CELL_DEG
    miny = ORIGIN_LAT + r * CELL_DEG
    return minx, miny, minx + CELL_DEG * PARCEL_FRACTION, miny + CELL_DEG * PARCEL_FRACTION


def roof_bounds(n: int) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = parcel_bounds(n)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    hw, hh = ROOF_W_M / M_PER_DEG_LON / 2, ROOF_H_M / M_PER_DEG_LAT / 2
    return cx - hw, cy - hh, cx + hw, cy + hh


def grid_bounds(cols: int = COLS) -> tuple[float, float, float, float]:
    """Lon/lat extent of the first ``cols`` grid columns, all five rows."""
    return ORIGIN_LON, ORIGIN_LAT, ORIGIN_LON + cols * CELL_DEG, ORIGIN_LAT + 5 * CELL_DEG


#: Per-band multiplier applied to the 2023 render. Two captures of the same ground differ
#: in more than their roofs -- on real NAIP, NIR falls between years while brightness
#: barely moves, which is what drags vegetation across an absolute NDVI cutoff. Without an
#: offset here the fixtures would exercise none of the radiometric normalisation path.
RECAPTURE_GAIN = (1.06, 1.04, 1.02, 0.84)


def render(
    epsg: int,
    resolution_m: float,
    lonlat_bounds: tuple[float, float, float, float],
    margin_m: float,
    roofs: tuple[int, ...],
    bands: int,
    gain: tuple[float, ...] | None = None,
) -> tuple[np.ndarray, rasterio.Affine]:
    to_proj = Transformer.from_crs(4326, epsg, always_xy=True)
    to_lonlat = Transformer.from_crs(epsg, 4326, always_xy=True)
    xs, ys = to_proj.transform(
        [lonlat_bounds[0], lonlat_bounds[2], lonlat_bounds[0], lonlat_bounds[2]],
        [lonlat_bounds[1], lonlat_bounds[1], lonlat_bounds[3], lonlat_bounds[3]],
    )
    minx, maxx = min(xs) - margin_m, max(xs) + margin_m
    miny, maxy = min(ys) - margin_m, max(ys) + margin_m
    width = int(math.ceil((maxx - minx) / resolution_m))
    height = int(math.ceil((maxy - miny) / resolution_m))
    transform = from_origin(minx, maxy, resolution_m, resolution_m)

    cols_px = np.arange(width) + 0.5
    rows_px = np.arange(height) + 0.5
    px_x = minx + cols_px * resolution_m
    px_y = maxy - rows_px * resolution_m
    grid_x, grid_y = np.meshgrid(px_x, px_y)
    lon, lat = to_lonlat.transform(grid_x, grid_y)

    # Field everywhere, with a coarse checker texture so it is not perfectly flat.
    checker = ((np.arange(width)[None, :] // 3 + np.arange(height)[:, None] // 3) % 2) * (
        2 * TEXTURE_AMPLITUDE
    ) - TEXTURE_AMPLITUDE
    data = np.empty((bands, height, width), dtype=np.uint8)
    for b in range(bands):
        data[b] = np.clip(FIELD[b] + checker, 1, 255)

    roof_mask = np.zeros((height, width), dtype=bool)
    for n in roofs:
        rminx, rminy, rmaxx, rmaxy = roof_bounds(n)
        roof_mask |= (lon >= rminx) & (lon <= rmaxx) & (lat >= rminy) & (lat <= rmaxy)
    for b in range(bands):
        data[b][roof_mask] = ROOF[b]
    if gain is not None:
        for b in range(bands):
            data[b] = np.clip(data[b].astype(np.float32) * gain[b], 1, 255).astype(np.uint8)
    return data, transform


def write_cog(path: Path, data: np.ndarray, transform: rasterio.Affine, epsg: int) -> None:
    bands, height, width = data.shape
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": bands,
        "height": height,
        "width": width,
        "crs": f"EPSG:{epsg}",
        "transform": transform,
        "nodata": 0,
        # Without an explicit photometric, GDAL tags a 4th uint8 band as alpha and every
        # reader would then mask on NIR.
        "photometric": "RGB",
    }
    tmp = path.with_suffix(".plain.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data)
        if bands == 4:
            dst.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.undefined,
            )
    cog_translate(tmp, path, cog_profiles.get("deflate"), quiet=True)
    tmp.unlink()
    size = path.stat().st_size
    assert size < MAX_FILE_BYTES, f"{path.name} is {size} bytes; keep fixtures under 1 MB"
    print(f"wrote {path} ({size} bytes, {width}x{height}, {bands} bands)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "imagery")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    full = grid_bounds()
    data, transform = render(26915, 1.0, full, MARGIN_M, EXISTING_ROOFS, 4)
    write_cog(args.out / "naip_2021.tif", data, transform, 26915)

    data, transform = render(
        26915, 1.0, full, MARGIN_M, EXISTING_ROOFS + NEW_ROOFS_2023, 4, gain=RECAPTURE_GAIN
    )
    write_cog(args.out / "naip_2023.tif", data, transform, 26915)

    # Columns 0-2 only, no margin on the east so columns 3-4 stay entirely uncovered.
    west = grid_bounds(cols=3)
    data, transform = render(
        3857, 0.5, west, 0.0, EXISTING_ROOFS + NEW_ROOFS_2023 + NEW_ROOFS_2025, 3
    )
    write_cog(args.out / "ortho_2025_partial.tif", data, transform, 3857)


if __name__ == "__main__":
    main()
