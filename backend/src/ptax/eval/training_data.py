"""Training chips for the building segmenter, drawn only from outside the evaluation area.

The segmenter learns "this pixel is a building" from NAIP imagery paired with Microsoft's
building footprints (`ptax.eval.footprints`). Two properties decide whether what it learns
can be trusted, and this module enforces both in code rather than by care:

- **No leakage.** The 300 visual labels in ``nw-hennepin`` are the only trusted truth, so
  no training chip may come from anywhere near it. Every training AOI must lie at least
  ``MIN_DISJOINT_M`` from every evaluation AOI, measured in metres on a projected grid.
- **Labels that are true in every year used.** The footprints are one snapshot from
  imagery of roughly 2014-2021, while training draws on NAIP back to 2010 -- the
  evaluation's base-year vintage. So training AOIs are neighbourhoods built out before
  2000, checked against the assessor's build years, where a footprint marks a building
  that stands in every capture. Drawing several years over the same ground also teaches
  the model that a roof in 2010 imagery is the same roof in 2021 imagery.

Chips are read through ``read_parcel_uris`` -- the run job's own reader -- with each chip's
box standing in for a parcel, so training imagery is resampled onto the 1.0 m comparison
grid exactly as inference imagery is. Footprints are then burned onto that same grid, so
image and label align by construction.
"""

import hashlib
import json
import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely import STRtree
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from ptax.detection.detector import ParcelRaster
from ptax.eval import sources
from ptax.eval.cache import (
    LOCAL_ENV,
    entry_path,
    fetch_item,
    read_manifest,
    uris_for,
    write_manifest,
)
from ptax.eval.dataset import AOIS, build_year
from ptax.eval.footprints import (
    FOOTPRINTS_BYTES,
    FOOTPRINTS_URL,
    ensure_footprints,
    rasterise,
    read_footprints,
)
from ptax.imagery.reader import read_parcel_uris
from ptax.parcels.footprint import utm_epsg_for

#: Training areas, as (min_lon, min_lat, max_lon, max_lat): long-established Hennepin
#: neighbourhoods, each checked on 2026-09-23 to have >= 90% of dated parcels built by
#: 2000 and NAIP in 2010/2015/2019/2021. Edina (-93.375, 44.895, -93.345, 44.920) was a
#: candidate and measured 0.820 -- its teardown-rebuilds carry post-2000 years -- so it is
#: not here. Rural west-Hennepin candidates measured 0.44-0.72 and are not here either,
#: which means training sees little open field or bare soil: the classical detector's
#: named false positives are under-represented, and the evaluation will show the cost.
TRAIN_AOIS: dict[str, tuple[float, float, float, float]] = {
    "richfield": (-93.300, 44.865, -93.270, 44.890),
    "minnetonka": (-93.470, 44.920, -93.440, 44.945),
    "brooklyn-park": (-93.370, 45.070, -93.340, 45.095),
    "crystal": (-93.370, 45.020, -93.340, 45.045),
}
DEFAULT_YEARS = (2010, 2015, 2019, 2021)

#: Minimum planar gap between a training AOI and any evaluation AOI.
MIN_DISJOINT_M = 1000.0
#: UTM 15N on NAD83: a projected metric grid covering all of Hennepin County. Degree
#: deltas would mis-scale longitude by ~30% at 45 N.
DISTANCE_EPSG = 26915
#: Share of dated parcels that must predate this year for an AOI to count as built out.
BUILT_OUT_BY = 2000
MIN_BUILT_OUT_SHARE = 0.90

CHIP_PX = 256
CHIP_RESOLUTION_M = 1.0
#: Trailing share of each AOI's chip columns held out for validation. A contiguous strip
#: of whole tiles, the same strip in every year, so no validation pixel's ground -- or
#: its immediate neighbour -- is ever seen in training.
VALIDATION_FRACTION = 0.2
#: Chips with less imagery than this (edge of a NAIP item) are left out.
MIN_VALID_FRACTION = 0.95
#: Label screen: a tile whose building fraction sits further than this from its AOI's
#: median is excluded as a likely footprint gap or misregistration. Loose on purpose -- a
#: residential tile runs ~0.1-0.3 building -- so it catches only the gross cases. It also
#: removes whole parks, which is recorded in the manifest rather than hidden.
SCREEN_MAX_DEVIATION = 0.15

TRAINING_DIR = Path("eval") / "cache" / "training"


def _projected(bbox: tuple[float, float, float, float], epsg: int) -> BaseGeometry:
    to_grid = Transformer.from_crs(4326, epsg, always_xy=True).transform
    projected: BaseGeometry = shapely_transform(to_grid, box(*bbox))
    return projected


def assert_disjoint_from_eval(
    aoi: tuple[float, float, float, float],
    eval_aois: dict[str, tuple[float, float, float, float]] = AOIS,
) -> None:
    """Raise ``ValueError`` if ``aoi`` lies within ``MIN_DISJOINT_M`` of an evaluation AOI."""
    candidate = _projected(aoi, DISTANCE_EPSG)
    for name, bbox in eval_aois.items():
        gap = candidate.distance(_projected(bbox, DISTANCE_EPSG))
        if gap < MIN_DISJOINT_M:
            raise ValueError(
                f"training AOI {aoi} is {gap:.0f} m from evaluation AOI {name!r};"
                f" at least {MIN_DISJOINT_M:.0f} m is required"
            )


def built_out_share(parcels: Iterable[dict[str, Any]]) -> tuple[float, int]:
    """(share of dated parcels built by ``BUILT_OUT_BY``, number of dated parcels)."""
    years = [build_year(p.get("BUILD_YR")) for p in parcels]
    dated = [y for y in years if y > 0]
    if not dated:
        return 0.0, 0
    return sum(1 for y in dated if y <= BUILT_OUT_BY) / len(dated), len(dated)


@dataclass(frozen=True)
class ChipBox:
    """One chip's place in its AOI's grid, and its footprint on the ground (EPSG:4326)."""

    row: int
    col: int
    geometry: BaseGeometry
    #: The chip's nominal north-west corner on the UTM grid, for burning footprints.
    origin: tuple[float, float]
    epsg: int


def chip_boxes(aoi: tuple[float, float, float, float]) -> list[ChipBox]:
    """Tile ``aoi`` into whole ``CHIP_PX``-metre squares on its UTM grid, north-west first."""
    epsg = utm_epsg_for(box(*aoi))
    minx, miny, maxx, maxy = _projected(aoi, epsg).bounds
    span = CHIP_PX * CHIP_RESOLUTION_M
    columns = int((maxx - minx) // span)
    rows = int((maxy - miny) // span)
    to_wgs84 = Transformer.from_crs(epsg, 4326, always_xy=True).transform
    boxes: list[ChipBox] = []
    for row in range(rows):
        for col in range(columns):
            west, north = minx + col * span, maxy - row * span
            square = box(west, north - span, west + span, north)
            boxes.append(
                ChipBox(row, col, shapely_transform(to_wgs84, square), (west, north), epsg)
            )
    return boxes


def split_for(col: int, columns: int) -> str:
    """``"validation"`` for the trailing ``VALIDATION_FRACTION`` of columns, else ``"train"``."""
    held_out = max(1, round(columns * VALIDATION_FRACTION))
    return "validation" if col >= columns - held_out else "train"


@dataclass(frozen=True)
class Chip:
    image: np.ndarray  # (3, CHIP_PX, CHIP_PX) uint8, RGB, zero where invalid
    label: np.ndarray  # (CHIP_PX, CHIP_PX) bool, building
    valid: np.ndarray  # (CHIP_PX, CHIP_PX) bool, imagery present


def _fit(array: np.ndarray, fill: Any) -> np.ndarray:
    """Crop or pad the trailing two axes to ``CHIP_PX``, padding with ``fill``."""
    out = np.full((*array.shape[:-2], CHIP_PX, CHIP_PX), fill, dtype=array.dtype)
    h, w = min(CHIP_PX, array.shape[-2]), min(CHIP_PX, array.shape[-1])
    out[..., :h, :w] = array[..., :h, :w]
    return out


def make_chip(raster: ParcelRaster, footprints: Sequence[BaseGeometry]) -> Chip:
    """Pair a chip's RGB with its footprints burned onto the raster's own grid.

    ``read_parcel_uris`` rounds the chip's extent up to whole pixels, so a read can come
    back a pixel larger than ``CHIP_PX``; it is cropped from the north-west corner, which
    is where the grid is anchored. A short read is padded and the padding marked invalid.
    """
    height, width = raster.data.shape[1:]
    label = rasterise(footprints, raster.transform, (height, width), raster.crs)
    valid = _fit(raster.mask.astype(bool), False)
    image = _fit(np.asarray(raster.data[:3], dtype=np.uint8), 0)
    image[:, ~valid] = 0
    return Chip(image=image, label=_fit(label, False) & valid, valid=valid)


def screen_tiles(
    fractions: dict[tuple[int, int], float],
) -> dict[tuple[int, int], str]:
    """Tiles whose building fraction is too far from the AOI median, with the reason."""
    if not fractions:
        return {}
    median = statistics.median(fractions.values())
    return {
        key: f"building fraction {value:.3f} vs AOI median {median:.3f}"
        for key, value in fractions.items()
        if abs(value - median) > SCREEN_MAX_DEVIATION
    }


def _tile_fraction(chip_box: ChipBox, footprints: Sequence[BaseGeometry]) -> float:
    """Building fraction of a tile from the footprints alone, on its nominal grid."""
    transform = from_origin(*chip_box.origin, CHIP_RESOLUTION_M, CHIP_RESOLUTION_M)
    return float(rasterise(footprints, transform, (CHIP_PX, CHIP_PX), chip_box.epsg).mean())


class _FootprintIndex:
    """Footprints near a chip, without rasterising the whole AOI's thousands per chip."""

    def __init__(self, footprints: list[BaseGeometry]) -> None:
        self._footprints = footprints
        self._tree = STRtree(footprints)

    def near(self, geometry: BaseGeometry) -> list[BaseGeometry]:
        return [self._footprints[i] for i in self._tree.query(geometry)]


def build_training_data(
    out_dir: Path = TRAINING_DIR,
    years: Sequence[int] = DEFAULT_YEARS,
    aois: dict[str, tuple[float, float, float, float]] = TRAIN_AOIS,
    echo: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Fetch, tile and write every training AOI's chips; return the manifest written.

    Shards are one ``.npz`` per AOI and year under ``out_dir/shards``. The manifest records
    what went in and what was left out -- AOIs dropped by the built-out check, years with
    no imagery, tiles dropped by the label screen or for missing imagery -- so the model
    card can cite exactly which data trained it.
    """
    footprints_zip = ensure_footprints()
    signer = sources.SasSigner()
    manifest: dict[str, Any] = {
        "footprints": {"url": FOOTPRINTS_URL, "bytes": FOOTPRINTS_BYTES},
        "chip_px": CHIP_PX,
        "resolution_m": CHIP_RESOLUTION_M,
        "validation_fraction": VALIDATION_FRACTION,
        "min_built_out_share": MIN_BUILT_OUT_SHARE,
        "screen_max_deviation": SCREEN_MAX_DEVIATION,
        "requested_years": list(years),
        "aois": {},
    }
    for name, bbox in aois.items():
        assert_disjoint_from_eval(bbox)
        share, dated = built_out_share(sources.fetch_parcels(bbox))
        record: dict[str, Any] = {
            "bbox": list(bbox),
            "built_out_share": round(share, 4),
            "dated_parcels": dated,
        }
        manifest["aois"][name] = record
        echo(f"{name}: {share:.3f} of {dated} dated parcels built by {BUILT_OUT_BY}")
        if share < MIN_BUILT_OUT_SHARE:
            record["dropped"] = f"built-out share {share:.3f} < {MIN_BUILT_OUT_SHARE}"
            echo(f"  dropped: {record['dropped']}")
            continue

        boxes = chip_boxes(bbox)
        # Read footprints over the whole tiled extent, which the AOI box contains.
        footprints = read_footprints(footprints_zip, bbox)
        index = _FootprintIndex(footprints)
        within = index.near

        fractions = {(b.row, b.col): _tile_fraction(b, within(b.geometry)) for b in boxes}
        screened = screen_tiles(fractions)
        columns = max(b.col for b in boxes) + 1
        record.update(
            {
                "footprints": len(footprints),
                "tiles": len(boxes),
                "columns": columns,
                "median_building_fraction": round(statistics.median(fractions.values()), 4),
                "screened_out": {f"{r},{c}": why for (r, c), why in sorted(screened.items())},
                "years": {},
            }
        )
        echo(f"  {len(boxes)} tiles, {len(footprints)} footprints, {len(screened)} screened out")

        imagery_dir = out_dir / "imagery" / name
        entries = read_manifest(imagery_dir / "manifest.json")
        for year in years:
            items = sources.naip_items(bbox, year)
            if not items:
                record["years"][str(year)] = {"missing": "no NAIP items"}
                echo(f"  {year}: no NAIP items")
                continue
            for item in items:
                destination = entry_path(imagery_dir, item)
                if item.id in entries and destination.exists():
                    continue
                echo(f"  writing {item.id} ({item.gsd_m} m) ...")
                entries[item.id] = fetch_item(item, bbox, destination, signer)
                write_manifest(imagery_dir / "manifest.json", entries)

            images, labels, valids, keys, splits = [], [], [], [], []
            no_imagery = 0
            for b in boxes:
                if (b.row, b.col) in screened:
                    continue
                raster = read_parcel_uris(
                    LOCAL_ENV,
                    uris_for(entries, year, b.geometry),
                    b.geometry,
                    resolution_m=CHIP_RESOLUTION_M,
                )
                chip = make_chip(raster, within(b.geometry)) if raster is not None else None
                if chip is None or chip.valid.mean() < MIN_VALID_FRACTION:
                    no_imagery += 1
                    continue
                images.append(chip.image)
                labels.append(chip.label)
                valids.append(chip.valid)
                keys.append((b.row, b.col))
                splits.append(split_for(b.col, columns))

            shard = out_dir / "shards" / f"{name}-{year}.npz"
            shard.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                shard,
                images=np.stack(images) if images else np.zeros((0, 3, CHIP_PX, CHIP_PX), np.uint8),
                labels=np.stack(labels) if labels else np.zeros((0, CHIP_PX, CHIP_PX), bool),
                valid=np.stack(valids) if valids else np.zeros((0, CHIP_PX, CHIP_PX), bool),
                keys=np.array(keys, dtype=np.int32).reshape(-1, 2),
                split=np.array(splits),
                year=np.int32(year),
            )
            train = splits.count("train")
            record["years"][str(year)] = {
                "items": sorted(i.id for i in items),
                "gsd_m": sorted({i.gsd_m for i in items}),
                "shard": str(shard),
                "shard_sha256": hashlib.sha256(shard.read_bytes()).hexdigest(),
                "train_tiles": train,
                "validation_tiles": len(splits) - train,
                "excluded_no_imagery": no_imagery,
            }
            echo(
                f"  {year}: {train} train, {len(splits) - train} validation,"
                f" {no_imagery} without imagery"
            )

    manifest_path = out_dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
