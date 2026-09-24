"""Measure a structure inventory run against parcels labelled by eye.

Three steps, each a ``ptax-eval`` command:

1. ``draw_sample`` takes a stratified sample of the tenant's parcels, stratified by the
   county record (detached garage, attached garage only, a house with no garage, vacant),
   so the rarer structure kinds are not left to chance.
2. ``render_sheets`` draws the sample as numbered contact sheets for labelling. The sheets
   are blind: they never show what the model found.
3. ``score_inventory`` compares the run with the filled labels, per structure kind,
   against the accepted bar, and with the county record.

Nothing here stores an owner or address: the sample and labels hold the PIN, the stratum
and the counts only.
"""

import json
import random
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from ptax.detection.detector import ParcelRaster
from ptax.eval.chips import GUTTER, GUTTER_VALUE, _boundary, _rgb, write_png
from ptax.vision.inventory import KINDS, OUTLINE_RGB

STRATA = ("detached_garage", "attached_garage", "house_no_garage", "vacant")
SAMPLE_SIZE = 200
PER_STRATUM = 50
#: The accepted bar: presence precision and recall, per kind (PRD).
BARS: dict[str, float] = {"house": 0.95, "garage": 0.85, "shed": 0.70, "pool": 0.80}
NO_STRUCTURES_BAR = 0.95
#: Below this many positive labels a kind's precision and recall say too little to judge.
MIN_LABELS = 10
TOO_FEW = "too few labels to judge"

LABEL_RULES = [
    "Count what lies inside the parcel outline in the image; ignore neighbouring parcels.",
    "house: the main dwelling. An attached garage is part of the house, not a garage.",
    "garage: a detached garage or outbuilding (barn, pole building, workshop).",
    "shed: a small detached storage shed.",
    "pool: a swimming pool, in-ground or above-ground.",
    "other: any other structure, e.g. a commercial building or a large tank.",
    "Enter 0 for none. Every count must be filled in; use the note for anything unclear.",
]


def _number(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def stratum_of(attributes: dict[str, Any]) -> str:
    if _number(attributes.get("det_gar_area")) > 0:
        return "detached_garage"
    if _number(attributes.get("gar_area")) > 0:
        return "attached_garage"
    built = _number(attributes.get("year_built")) > 0
    if built or _number(attributes.get("total_living_area")) > 0:
        return "house_no_garage"
    return "vacant"


def draw_sample(
    parcels: Iterable[tuple[str, dict[str, Any]]],
    *,
    seed: int,
    size: int = SAMPLE_SIZE,
    per_stratum: int = PER_STRATUM,
) -> list[dict[str, Any]]:
    """``per_stratum`` parcels from each stratum (all of a smaller one), topped up to
    ``size`` from the rest. Deterministic for a seed whatever order ``parcels`` come in."""
    by_stratum: dict[str, list[str]] = {name: [] for name in STRATA}
    for pin, attributes in parcels:
        by_stratum[stratum_of(attributes)].append(pin)
    rng = random.Random(seed)
    chosen: list[tuple[str, str]] = []
    rest: list[tuple[str, str]] = []
    for name in STRATA:
        pins = sorted(by_stratum[name])
        rng.shuffle(pins)
        chosen += [(pin, name) for pin in pins[:per_stratum]]
        rest += [(pin, name) for pin in pins[per_stratum:]]
    rest.sort()
    rng.shuffle(rest)
    chosen += rest[: max(0, size - len(chosen))]
    chosen.sort(key=lambda item: (STRATA.index(item[1]), item[0]))
    return [{"n": n, "PIN": pin, "stratum": name} for n, (pin, name) in enumerate(chosen, 1)]


def labels_template(sample: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rules": LABEL_RULES,
        "kinds": list(KINDS),
        "parcels": [
            {"n": p["n"], "PIN": p["PIN"], **{kind: None for kind in KINDS}, "note": ""}
            for p in sample
        ],
    }


class LabelError(ValueError):
    """The labels file is incomplete or malformed."""


def read_labels(path: Path) -> dict[str, dict[str, int]]:
    """PIN -> count per kind; ``LabelError`` naming every unfilled or invalid entry."""
    payload = json.loads(path.read_text())
    labels: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    for entry in payload.get("parcels", []):
        pin = str(entry.get("PIN"))
        counts: dict[str, int] = {}
        for kind in KINDS:
            value = entry.get(kind)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"{pin}: {kind} is {value!r}, not a count")
            else:
                counts[kind] = value
        labels[pin] = counts
    if problems:
        shown = "; ".join(problems[:20])
        more = f" (and {len(problems) - 20} more)" if len(problems) > 20 else ""
        raise LabelError(f"{len(problems)} label problem(s): {shown}{more}")
    return labels


@dataclass(frozen=True)
class KindResult:
    kind: str
    #: Parcels labelled with at least one of this kind.
    n: int
    precision: float | None
    recall: float | None
    #: Share of scored parcels whose predicted count equals the labelled count.
    exact_count_rate: float
    bar: float
    verdict: str


@dataclass(frozen=True)
class RateResult:
    n: int
    rate: float | None
    bar: float
    verdict: str


@dataclass
class InventoryReport:
    kinds: dict[str, KindResult]
    no_structures: RateResult
    #: County-record agreement: description -> (agreeing, of).
    county: dict[str, tuple[int, int]]
    #: Labelled parcels the run could not answer (model error, no imagery, not reached).
    unscored: list[str] = field(default_factory=list)
    scored: int = 0


def _verdict(n: int, values: list[float | None], bar: float) -> str:
    if n < MIN_LABELS:
        return TOO_FEW
    return "pass" if all(v is not None and v >= bar for v in values) else "fail"


def score_inventory(
    predicted: dict[str, dict[str, int] | None],
    labels: dict[str, dict[str, int]],
    county: dict[str, dict[str, Any]],
) -> InventoryReport:
    """Per-kind presence precision and recall, count accuracy, "no structures" accuracy and
    county agreement. ``predicted[pin]`` is None where the run has no usable answer."""
    scored = [pin for pin in labels if predicted.get(pin) is not None]
    unscored = sorted(pin for pin in labels if predicted.get(pin) is None)

    def found(pin: str, kind: str) -> int:
        return (predicted[pin] or {}).get(kind, 0)

    kinds: dict[str, KindResult] = {}
    for kind in KINDS:
        tp = sum(1 for p in scored if labels[p][kind] > 0 and found(p, kind) > 0)
        fp = sum(1 for p in scored if labels[p][kind] == 0 and found(p, kind) > 0)
        fn = sum(1 for p in scored if labels[p][kind] > 0 and found(p, kind) == 0)
        n = tp + fn
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / n if n else None
        exact = sum(1 for p in scored if labels[p][kind] == found(p, kind))
        bar = BARS.get(kind, 0.0)
        verdict = _verdict(n, [precision, recall], bar) if kind in BARS else "not in the bar"
        kinds[kind] = KindResult(
            kind, n, precision, recall, exact / len(scored) if scored else 0.0, bar, verdict
        )

    empty = [p for p in scored if not any(labels[p].values())]
    right = sum(1 for p in empty if not any((predicted[p] or {}).values()))
    rate = right / len(empty) if empty else None
    no_structures = RateResult(
        len(empty), rate, NO_STRUCTURES_BAR, _verdict(len(empty), [rate], NO_STRUCTURES_BAR)
    )

    def agreement(on_record: Callable[[dict[str, Any]], bool], ok: Callable[[str], bool]):  # noqa: ANN202
        pins = [p for p in scored if on_record(county.get(p, {}))]
        return (sum(1 for p in pins if ok(p)), len(pins))

    county_agreement = {
        "detached garage on record → garage found": agreement(
            lambda a: stratum_of(a) == "detached_garage", lambda p: found(p, "garage") > 0
        ),
        "house on record → house found": agreement(
            lambda a: stratum_of(a) != "vacant", lambda p: found(p, "house") > 0
        ),
        "vacant on record → no house found": agreement(
            lambda a: stratum_of(a) == "vacant", lambda p: found(p, "house") == 0
        ),
    }
    return InventoryReport(kinds, no_structures, county_agreement, unscored, len(scored))


def _cell(raster: ParcelRaster, cell_px: int) -> np.ndarray:
    """The parcel scaled smoothly to fill a square cell, outlined as the model sees it.

    Smooth scaling, unlike the audit chips' whole-pixel steps: this is a labelling surface,
    and a parcel one pixel too big for a whole step would otherwise render at half size.
    """
    rgb = _rgb(raster)
    edge = _boundary(raster.parcel_mask)
    thick = edge.copy()
    thick[1:] |= edge[:-1]
    thick[:-1] |= edge[1:]
    thick[:, 1:] |= edge[:, :-1]
    thick[:, :-1] |= edge[:, 1:]
    rgb[thick] = OUTLINE_RGB
    height, width = rgb.shape[:2]
    scale = cell_px / max(height, width)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    scaled = np.asarray(Image.fromarray(rgb).resize(size, Image.Resampling.LANCZOS))
    out = np.full((cell_px, cell_px, 3), GUTTER_VALUE, dtype=np.uint8)
    top, left = (cell_px - size[1]) // 2, (cell_px - size[0]) // 2
    out[top : top + size[1], left : left + size[0]] = scaled
    return out


def _number_badge(cell: np.ndarray, n: int) -> np.ndarray:
    """Stamp the parcel's sample number in the cell's top-left corner, black on white."""
    image = Image.fromarray(cell)
    draw = ImageDraw.Draw(image)
    text = str(n)
    left, top, right, bottom = draw.textbbox((0, 0), text)
    draw.rectangle((0, 0, right - left + 6, bottom - top + 6), fill=(255, 255, 255))
    draw.text((3 - left, 3 - top), text, fill=(0, 0, 0))
    return np.asarray(image)


def render_sheets(
    sample: list[dict[str, Any]],
    raster_for: Callable[[str], ParcelRaster | None],
    out_dir: Path,
    *,
    per_sheet: int = 20,
    columns: int = 5,
    cell_px: int = 384,
) -> dict[str, Any]:
    """Numbered contact sheets of the sample's parcels, outline only, and their index.

    Blind by construction: only the imagery and the parcel outline are drawn. A parcel
    with no imagery keeps its numbered cell so the numbering never shifts.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cells: list[dict[str, Any]] = []
    for start in range(0, len(sample), per_sheet):
        chunk = sample[start : start + per_sheet]
        name = f"sheet-{start // per_sheet + 1:02}.png"
        rows = (len(chunk) + columns - 1) // columns
        sheet = np.full(
            (rows * (cell_px + GUTTER) + GUTTER, columns * (cell_px + GUTTER) + GUTTER, 3),
            GUTTER_VALUE,
            dtype=np.uint8,
        )
        for index, parcel in enumerate(chunk):
            row, column = divmod(index, columns)
            raster = raster_for(parcel["PIN"])
            cell = (
                _cell(raster, cell_px)
                if raster is not None
                else np.full((cell_px, cell_px, 3), GUTTER_VALUE, dtype=np.uint8)
            )
            cell = _number_badge(cell, parcel["n"])
            top = GUTTER + row * (cell_px + GUTTER)
            left = GUTTER + column * (cell_px + GUTTER)
            sheet[top : top + cell_px, left : left + cell_px] = cell
            cells.append(
                {
                    "n": parcel["n"],
                    "PIN": parcel["PIN"],
                    "sheet": name,
                    "row": row,
                    "column": column,
                    "imagery": raster is not None,
                }
            )
        write_png(out_dir / name, sheet)
    sheet_index = {"columns": columns, "cells": cells}
    (out_dir / "index.json").write_text(json.dumps(sheet_index, indent=2) + "\n")
    return sheet_index
