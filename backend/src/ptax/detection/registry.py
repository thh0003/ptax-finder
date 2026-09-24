"""Detectors by name, so a caller (the evaluation harness) can choose one by its name."""

from collections.abc import Callable

from ptax.detection.detector import ClassicalDetector, Detector


def _classical() -> Detector:
    return ClassicalDetector()


#: Every selectable detector. ``classical`` is the default everywhere a name is optional.
DETECTORS: dict[str, Callable[[], Detector]] = {
    "classical": _classical,
}
DEFAULT_DETECTOR = "classical"


def get_detector(name: str) -> Detector:
    """Build the detector registered under ``name``."""
    factory = DETECTORS.get(name)
    if factory is None:
        raise ValueError(f"unknown detector {name!r}; known: {', '.join(DETECTORS)}")
    return factory()
