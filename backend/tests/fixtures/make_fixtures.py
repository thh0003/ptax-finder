"""Generate synthetic parcel layers for tests.

    uv run python tests/fixtures/make_fixtures.py                 # 25 parcels into tests/fixtures/
    uv run python tests/fixtures/make_fixtures.py --count 50000 --out /tmp/perf

Writes ``parcels_small.geojson`` (EPSG:4326) and ``parcels_small_26915.zip`` (a zipped
shapefile in EPSG:26915, i.e. UTM 15N covering Minnesota) with ``PIN`` and ``OWNER``
attributes. The committed 25-parcel files are what the suite uses; the generator exists
so they can be regenerated and so the perf check can build a large layer on demand.
"""

import argparse
import math
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

# South-west corner of the grid, in a rural part of Hennepin County, MN.
ORIGIN_LON, ORIGIN_LAT = -93.70, 45.05
CELL_DEG = 0.0015  # ~110 m N-S, ~115 m E-W at this latitude


def build(count: int) -> gpd.GeoDataFrame:
    cols = max(1, int(math.ceil(math.sqrt(count))))
    rows = []
    for i in range(count):
        r, c = divmod(i, cols)
        minx = ORIGIN_LON + c * CELL_DEG
        miny = ORIGIN_LAT + r * CELL_DEG
        rows.append(
            {
                "PIN": f"27-053-{i + 1:06d}",
                "OWNER": f"Owner {i + 1}",
                "geometry": box(minx, miny, minx + CELL_DEG * 0.9, miny + CELL_DEG * 0.9),
            }
        )
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def write(gdf: gpd.GeoDataFrame, out: Path, stem: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out / f"{stem}.geojson", driver="GeoJSON")

    shp_dir = out / f"{stem}_26915_shp"
    shp_dir.mkdir(exist_ok=True)
    gdf.to_crs("EPSG:26915").to_file(shp_dir / f"{stem}.shp", driver="ESRI Shapefile")
    with zipfile.ZipFile(out / f"{stem}_26915.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for part in sorted(shp_dir.iterdir()):
            zf.write(part, arcname=part.name)
    for part in shp_dir.iterdir():
        part.unlink()
    shp_dir.rmdir()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--out", type=Path, default=Path(__file__).parent)
    parser.add_argument("--stem", default="parcels_small")
    args = parser.parse_args()
    write(build(args.count), args.out, args.stem)
    print(f"wrote {args.count} parcels to {args.out}/{args.stem}.geojson and {args.stem}_26915.zip")


if __name__ == "__main__":
    main()
