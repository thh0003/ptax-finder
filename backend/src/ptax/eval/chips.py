"""Base/target image chips for auditing labels by eye.

`BUILD_YR` is objective but noisy in known directions: a parcel that gained a garage, an
addition or a barn keeps its old year and is labelled negative, and a teardown-rebuild is
labelled positive while showing a building in both captures. Neither is detector error, so
reporting precision without knowing how often they happen states a number with an unknown
error bar.

This module renders one PNG per parcel with the base year left and the target year right
at a common scale, plus a contact sheet of the whole audit sample, so a reader can settle
each case by looking. PNGs are written through rasterio's driver over a numpy array -- no
new dependency.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

from ptax.detection.detector import ParcelRaster

#: Pixels of padding drawn between and around chips in a pair or contact sheet.
GUTTER = 4
#: Grey value of that padding: mid-grey reads as a border against both dark and bright
#: imagery, where black or white would blend into one of them.
GUTTER_VALUE = 128
#: Parcel outline brightness. Drawn on every chip so the audit judges the right ground:
#: a new house one parcel over is not this parcel's change.
OUTLINE_VALUE = 255


@dataclass(frozen=True)
class ChipEntry:
    """One audited parcel's position in the contact sheet."""

    pid: str
    stratum: str
    row: int
    column: int
    path: str


def _rgb(raster: ParcelRaster) -> np.ndarray:
    """(h, w, 3) uint8 RGB with the parcel outlined and missing pixels greyed."""
    data = raster.data[:3].astype(np.float32)
    # NAIP is captured wide-dynamic; a 2-98 percentile stretch per chip makes roofs and
    # soil distinguishable by eye where a raw render is uniformly grey.
    inside = raster.mask
    if inside.any():
        lo, hi = np.percentile(data[:, inside], (2, 98))
        if hi > lo:
            data = np.clip((data - lo) * 255.0 / (hi - lo), 0, 255)
    rgb = np.transpose(data.astype(np.uint8), (1, 2, 0)).copy()
    rgb[~raster.mask] = GUTTER_VALUE
    rgb[_boundary(raster.parcel_mask)] = OUTLINE_VALUE
    return rgb


def _boundary(mask: np.ndarray) -> np.ndarray:
    """One-pixel outline of ``mask``: the mask minus its own erosion."""
    eroded = mask.copy()
    eroded[1:] &= mask[:-1]
    eroded[:-1] &= mask[1:]
    eroded[:, 1:] &= mask[:, :-1]
    eroded[:, :-1] &= mask[:, 1:]
    return mask & ~eroded


def _pad_to(chip: np.ndarray, height: int, width: int) -> np.ndarray:
    out = np.full((height, width, 3), GUTTER_VALUE, dtype=np.uint8)
    h, w = min(height, chip.shape[0]), min(width, chip.shape[1])
    out[:h, :w] = chip[:h, :w]
    return out


def pair(base: ParcelRaster, target: ParcelRaster) -> np.ndarray:
    """Base left, target right, on one canvas at a common scale."""
    left, right = _rgb(base), _rgb(target)
    height = max(left.shape[0], right.shape[0])
    width = max(left.shape[1], right.shape[1])
    canvas = np.full((height, width * 2 + GUTTER, 3), GUTTER_VALUE, dtype=np.uint8)
    canvas[:, :width] = _pad_to(left, height, width)
    canvas[:, width + GUTTER :] = _pad_to(right, height, width)
    return canvas


def zoom(image: np.ndarray, factor: int) -> np.ndarray:
    """Enlarge by nearest-neighbour replication.

    A 1 m parcel chip is 30-40 px across, which is unreadable on screen, and the point of
    the audit is to judge by eye. Replication rather than interpolation keeps every
    displayed pixel a real measured pixel, so the auditor is not looking at invented detail.
    """
    if factor <= 1:
        return image
    return np.repeat(np.repeat(image, factor, axis=0), factor, axis=1)


def fit(image: np.ndarray, target_px: int) -> np.ndarray:
    """Scale a chip toward ``target_px`` on its longest side, by whole-pixel steps only.

    Parcels in the set span a 60 px suburban lot to a 2300 px strip, and an audit that
    renders both at one zoom factor produces chips that are either unreadable or enormous.
    Enlargement replicates and reduction decimates, so every displayed pixel remains a
    measured pixel either way.
    """
    longest = max(image.shape[0], image.shape[1])
    if longest == 0:
        return image
    if longest < target_px:
        return zoom(image, max(1, target_px // longest))
    step = max(1, longest // target_px)
    return image[::step, ::step]


def letterbox(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Scale ``image`` to fit a fixed cell, preserving aspect, centred on the gutter grey.

    Contact sheets size their cells to the largest tile, so a grid of parcels whose shapes
    range from a thin roadside strip to a square five-acre block renders the small ones
    almost too small to judge. Giving every parcel the same cell keeps each one legible,
    which is the whole point when the sheet is the labelling surface.
    """
    source_h, source_w = image.shape[:2]
    if source_h == 0 or source_w == 0:
        return np.full((height, width, 3), GUTTER_VALUE, dtype=np.uint8)
    step = max(1, -(-max(source_h / height, source_w / width) // 1))
    scaled = image[:: int(step), :: int(step)]
    factor = min(height // max(scaled.shape[0], 1), width // max(scaled.shape[1], 1))
    if factor > 1:
        scaled = zoom(scaled, int(factor))

    out = np.full((height, width, 3), GUTTER_VALUE, dtype=np.uint8)
    h, w = min(height, scaled.shape[0]), min(width, scaled.shape[1])
    top, left = (height - h) // 2, (width - w) // 2
    out[top : top + h, left : left + w] = scaled[:h, :w]
    return out


def write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = image.shape[:2]
    with rasterio.open(
        path, "w", driver="PNG", height=height, width=width, count=3, dtype="uint8"
    ) as dst:
        dst.write(np.transpose(image, (2, 0, 1)))


def contact_sheet(chips: list[np.ndarray], columns: int = 5) -> np.ndarray:
    """Tile chips into one image so a whole audit sample can be read in a single look."""
    if not chips:
        return np.zeros((1, 1, 3), dtype=np.uint8)
    cell_h = max(c.shape[0] for c in chips)
    cell_w = max(c.shape[1] for c in chips)
    rows = (len(chips) + columns - 1) // columns
    sheet = np.full(
        (rows * (cell_h + GUTTER) + GUTTER, columns * (cell_w + GUTTER) + GUTTER, 3),
        GUTTER_VALUE,
        dtype=np.uint8,
    )
    for index, chip in enumerate(chips):
        row, column = divmod(index, columns)
        top = GUTTER + row * (cell_h + GUTTER)
        left = GUTTER + column * (cell_w + GUTTER)
        sheet[top : top + cell_h, left : left + cell_w] = _pad_to(chip, cell_h, cell_w)
    return sheet


def write_index(path: Path, entries: list[ChipEntry], columns: int) -> None:
    """Map contact-sheet positions back to parcels, so an audit can name what it saw."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "columns": columns,
        "layout": "each cell is one parcel: base year left, target year right",
        "chips": [
            {
                "pid": e.pid,
                "stratum": e.stratum,
                "row": e.row,
                "column": e.column,
                "path": e.path,
            }
            for e in entries
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
