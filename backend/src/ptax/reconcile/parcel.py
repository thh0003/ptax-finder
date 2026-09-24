"""One parcel's verdict: combine classified detections with the optional change model."""

from collections.abc import Iterable, Sequence

import shapely
from shapely.geometry.base import BaseGeometry

from ptax.reconcile.geometry import area_sqft
from ptax.reconcile.matching import classify
from ptax.reconcile.types import (
    CamaRecord,
    ClassifiedDetection,
    Detection,
    ParcelResult,
    ParcelStatus,
)
from ptax.tenancy.config import Thresholds


def _flagged(c: ClassifiedDetection) -> bool:
    """A change we report: new or expanded, reliable, and not already assessed."""
    return c.change_type in ("new", "expanded") and not c.already_assessed


def _added_region(c: ClassifiedDetection, a_by_id: dict[str, Detection]) -> BaseGeometry:
    """The ground a flagged detection says changed: its outline if new, the addition if
    expanded."""
    if c.matched_a_id is None:
        return c.detection.geom
    return c.detection.geom.difference(a_by_id[c.matched_a_id].geom)


def reconcile_parcel(
    detections_a: Sequence[Detection],
    detections_b: Sequence[Detection],
    *,
    pin: str,
    parcel_geom: BaseGeometry,
    year_a: int,
    crs_epsg: int,
    thresholds: Thresholds,
    change_polygons: Sequence[BaseGeometry] | None = None,
    cama: Iterable[CamaRecord] = (),
) -> ParcelResult:
    """The parcel's status, estimate and detections. Geometries are in ``crs_epsg``.

    ``high_confidence`` needs both signals: every flagged detection scores at least
    ``high_confidence_score`` and the change model covers enough of what it added.
    Without a change model a flagged parcel is at best ``needs_review``.
    """
    t = thresholds
    classified = classify(
        detections_a,
        detections_b,
        pin=pin,
        year_a=year_a,
        crs_epsg=crs_epsg,
        thresholds=t,
        cama=cama,
    )
    flagged = [c for c in classified if _flagged(c)]

    agrees: bool | None = None
    change_on_parcel = 0.0
    if change_polygons is not None:
        change = shapely.union_all(list(change_polygons))
        change_on_parcel = area_sqft(change.intersection(parcel_geom), crs_epsg)
        if flagged:
            a_by_id = {d.id: d for d in detections_a}
            regions = [_added_region(c, a_by_id) for c in flagged]
            agrees = all(
                r.area > 0 and r.intersection(change).area / r.area >= t.change_agreement_frac
                for r in regions
            )
        else:
            # Neither signal flags a change only if the change model saw too little to report.
            agrees = change_on_parcel < t.min_new_area_sqft

    uncertain_change = any(
        c.change_type == "uncertain" and c.would_be in ("new", "expanded") for c in classified
    )
    status: ParcelStatus
    if flagged and agrees and all(c.detection.score >= t.high_confidence_score for c in flagged):
        status = "high_confidence"
    elif flagged or change_on_parcel >= t.min_new_area_sqft or uncertain_change:
        status = "needs_review"
    else:
        status = "no_change"

    return ParcelResult(
        pin=pin,
        status=status,
        new_sqft_est=sum(c.delta_sqft for c in flagged),
        classes_added=tuple(sorted({c.detection.cls for c in flagged})),
        change_model_agrees=agrees,
        detections=tuple(classified),
    )
