"""Pair one parcel's Year A and Year B detections and classify what changed."""

from collections.abc import Iterable, Sequence

from shapely.geometry.base import BaseGeometry

from ptax.reconcile.geometry import area_sqft, feet_per_unit
from ptax.reconcile.types import CamaRecord, ChangeType, ClassifiedDetection, Detection
from ptax.tenancy.config import Thresholds


def _iou(a: BaseGeometry, b: BaseGeometry) -> float:
    union = a.union(b).area
    return float(a.intersection(b).area / union) if union else 0.0


def match(
    year_a: Sequence[Detection],
    year_b: Sequence[Detection],
    *,
    tolerance_units: float,
    iou_min: float,
) -> dict[str, tuple[str, float]]:
    """Year B id -> (Year A id, IoU): greedy one-to-one in descending IoU, class-blind.

    Both outlines are buffered by ``tolerance_units`` first, so misregistration and
    building lean between the two years do not break a match.
    """
    buffered_a = [(d.id, d.geom.buffer(tolerance_units)) for d in year_a]
    pairs = []
    for b in year_b:
        grown = b.geom.buffer(tolerance_units)
        for a_id, a_geom in buffered_a:
            if grown.intersects(a_geom):
                iou = _iou(grown, a_geom)
                if iou >= iou_min:
                    pairs.append((iou, b.id, a_id))
    matches: dict[str, tuple[str, float]] = {}
    taken: set[str] = set()
    for iou, b_id, a_id in sorted(pairs, key=lambda p: -p[0]):
        if b_id not in matches and a_id not in taken:
            matches[b_id] = (a_id, iou)
            taken.add(a_id)
    return matches


def _reliable(d: Detection, t: Thresholds) -> bool:
    return d.score >= t.min_score and not d.occluded and not d.tile_flagged


def _cama_suppresses(
    records: list[CamaRecord],
    used: set[int],
    *,
    cls: str,
    added_sqft: float,
    year_a: int,
    tolerance_pct: float,
) -> bool:
    """Claim the unused record closest in area that accounts for ``added_sqft``, if any."""
    candidates = [
        (abs(r.area_sqft - added_sqft), i)
        for i, r in enumerate(records)
        if i not in used
        and r.cls == cls
        and r.year >= year_a
        and abs(r.area_sqft - added_sqft) <= tolerance_pct * added_sqft
    ]
    if not candidates:
        return False
    used.add(min(candidates)[1])
    return True


def classify(
    detections_a: Sequence[Detection],
    detections_b: Sequence[Detection],
    *,
    pin: str,
    year_a: int,
    crs_epsg: int,
    thresholds: Thresholds,
    cama: Iterable[CamaRecord] = (),
) -> list[ClassifiedDetection]:
    """Every Year B detection with its change type, then every unmatched Year A one as
    ``removed``. Geometries are in ``crs_epsg``; areas are square feet."""
    t = thresholds
    matches = match(
        detections_a,
        detections_b,
        tolerance_units=t.match_tolerance_ft / feet_per_unit(crs_epsg),
        iou_min=t.iou_match,
    )
    a_by_id = {d.id: d for d in detections_a}
    records = [r for r in cama if r.pin == pin]
    used: set[int] = set()

    results = []
    for b in detections_b:
        area = area_sqft(b.geom, crs_epsg)
        a_id, iou = matches.get(b.id, (None, None))
        would_be: ChangeType
        if a_id is None:
            delta = area
            would_be = "new" if area >= t.min_new_area_sqft else "unchanged"
        else:
            area_a = area_sqft(a_by_id[a_id].geom, crs_epsg)
            delta = area - area_a
            grew = (
                delta >= t.expansion_min_sqft
                and area_a > 0
                and delta / area_a >= t.expansion_min_pct
            )
            would_be = "expanded" if grew else "unchanged"
        change_type = would_be if _reliable(b, t) else "uncertain"
        assessed = change_type in ("new", "expanded") and _cama_suppresses(
            records,
            used,
            cls=b.cls,
            added_sqft=delta,
            year_a=year_a,
            tolerance_pct=t.cama_area_tolerance_pct,
        )
        results.append(
            ClassifiedDetection(
                detection=b,
                change_type=change_type,
                area_sqft=area,
                delta_sqft=delta,
                matched_a_id=a_id,
                iou=iou,
                already_assessed=assessed,
                would_be=would_be,
            )
        )

    matched_a = {a_id for a_id, _ in matches.values()}
    for a in detections_a:
        if a.id not in matched_a:
            area = area_sqft(a.geom, crs_epsg)
            results.append(
                ClassifiedDetection(
                    detection=a,
                    change_type="removed",
                    area_sqft=area,
                    delta_sqft=-area,
                    would_be="removed",
                )
            )
    return results
