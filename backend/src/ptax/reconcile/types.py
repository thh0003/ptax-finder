"""Values passed through reconciliation. Geometries are in the tenant's projected CRS."""

from dataclasses import dataclass
from typing import Literal

from shapely.geometry.base import BaseGeometry

Year = Literal["A", "B"]
ChangeType = Literal["new", "expanded", "unchanged", "uncertain", "removed"]


@dataclass(frozen=True)
class Detection:
    """One structure a segmentation model found in one year's imagery."""

    id: str
    year: Year
    cls: str
    score: float
    geom: BaseGeometry
    #: Partly hidden (tree canopy, shadow); its outline and area are unreliable.
    occluded: bool = False
    #: In a tile whose co-registration residual exceeded the tolerance.
    tile_flagged: bool = False


@dataclass(frozen=True)
class CamaRecord:
    """One improvement the county's assessment records hold, already mapped to our classes."""

    pin: str
    cls: str
    area_sqft: float
    #: The year the improvement was assessed (or built, when that is all CAMA has).
    year: int


@dataclass(frozen=True)
class ClassifiedDetection:
    """A detection and what changed: a Year B detection, or an unmatched (removed) Year A one."""

    detection: Detection
    change_type: ChangeType
    area_sqft: float
    #: Added area: the full area when unmatched, Year B minus Year A when matched, and
    #: minus the Year A area when removed.
    delta_sqft: float
    matched_a_id: str | None = None
    iou: float | None = None
    #: A county assessment after Year A already accounts for this new or expanded area.
    already_assessed: bool = False
    #: What the detection would be were it reliable; differs from ``change_type`` only
    #: for ``uncertain`` detections.
    would_be: ChangeType | None = None


ParcelStatus = Literal["high_confidence", "needs_review", "no_change"]


@dataclass(frozen=True)
class ParcelResult:
    """One parcel's verdict for a run, with the classified detections behind it."""

    pin: str
    status: ParcelStatus
    #: Flagged ``new`` areas plus flagged ``expanded`` deltas, in square feet.
    new_sqft_est: float
    #: The sorted classes of the flagged detections.
    classes_added: tuple[str, ...]
    #: None without a change model; otherwise whether it agrees with segmentation.
    change_model_agrees: bool | None
    detections: tuple[ClassifiedDetection, ...]
