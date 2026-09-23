"""Detectors by name, so a caller can choose one without importing all of them.

The learned detector depends on torch, which only the optional ``ml`` dependency group
installs and the production image never does. Each entry is therefore a factory that
imports its module when called: importing this registry, or choosing ``classical``, never
touches torch.
"""

from collections.abc import Callable

from ptax.detection.detector import ClassicalDetector, Detector


def _classical() -> Detector:
    return ClassicalDetector()


def _segmentation() -> Detector:
    # The module is torch-free; the model it loads is not, so it is imported on demand.
    from ptax.detection import learned

    return learned.from_model_card()


#: Every selectable detector. ``classical`` is the default everywhere a name is optional.
DETECTORS: dict[str, Callable[[], Detector]] = {
    "classical": _classical,
    "segmentation": _segmentation,
}
DEFAULT_DETECTOR = "classical"


def get_detector(name: str) -> Detector:
    """Build the detector registered under ``name``."""
    factory = DETECTORS.get(name)
    if factory is None:
        raise ValueError(f"unknown detector {name!r}; known: {', '.join(DETECTORS)}")
    return factory()
