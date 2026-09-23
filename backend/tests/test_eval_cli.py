"""How `ptax-eval score` attaches visual labels to a scored run.

The rest of the command needs a populated imagery cache, which is too large to commit; the
part worth pinning here is the join between a scored parcel and its by-eye outcome, because
getting it wrong would silently report `BUILD_YR` figures under a visual-label heading.
"""

from ptax.eval.cli import _apply_visual_labels
from ptax.eval.dataset import NEGATIVE_OLD, POSITIVE, read_visual_labels_payload
from ptax.eval.metrics import ScoredParcel


def _labels(*rows: tuple[str, str, bool]):
    return read_visual_labels_payload(
        {
            "labels": [
                {"pid": pid, "stratum": stratum, "improved": improved, "reason": "x"}
                for pid, stratum, improved in rows
            ]
        }
    )


def test_without_labels_the_scored_parcels_are_returned_untouched() -> None:
    results = [ScoredParcel("a", POSITIVE, 0.5, True), ScoredParcel("b", NEGATIVE_OLD, 0.1, False)]

    assert _apply_visual_labels(results, None) == results


def test_the_label_overrides_the_stratum_without_changing_it() -> None:
    """A `negative_old` parcel that visibly gained a barn stays `negative_old` for weighting."""
    results = [ScoredParcel("a", NEGATIVE_OLD, 0.5, True), ScoredParcel("b", POSITIVE, 0.9, True)]

    labelled = _apply_visual_labels(
        results, _labels(("a", NEGATIVE_OLD, True), ("b", POSITIVE, False))
    )

    assert [p.stratum for p in labelled] == [NEGATIVE_OLD, POSITIVE]
    assert [p.improved for p in labelled] == [True, False]


def test_a_parcel_missing_from_the_labels_falls_back_to_its_stratum() -> None:
    """Every parcel in the shipped set is labelled; an unlabelled one must not become a
    silent negative, so it keeps the only outcome evidence it has."""
    results = [ScoredParcel("a", POSITIVE, 0.5, True)]

    labelled = _apply_visual_labels(results, _labels(("other", POSITIVE, True)))

    assert labelled[0].positive is None
    assert labelled[0].improved is True


def test_the_score_and_skip_reason_survive_relabelling() -> None:
    results = [ScoredParcel("a", POSITIVE, None, False, skipped_reason="no_coverage")]

    labelled = _apply_visual_labels(results, _labels(("a", POSITIVE, True)))

    assert labelled[0].skipped_reason == "no_coverage"
    assert labelled[0].scored is False


def test_the_registry_builds_the_classical_detector_by_name() -> None:
    from ptax.detection.detector import ClassicalDetector
    from ptax.detection.registry import get_detector

    assert isinstance(get_detector("classical"), ClassicalDetector)


def test_an_unknown_detector_name_is_refused_with_the_valid_names() -> None:
    import pytest

    from ptax.detection.registry import DETECTORS, get_detector

    with pytest.raises(ValueError) as excinfo:
        get_detector("nope")
    for name in DETECTORS:
        assert name in str(excinfo.value)


def test_importing_the_registry_never_imports_torch() -> None:
    """The production image has no torch; the registry is imported on every path."""
    import subprocess
    import sys

    probe = (
        "import sys, ptax.detection.registry, ptax.eval.cli;"
        "print('torch' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_the_year_bias_report_reads_the_segmenters_building_fractions(capsys) -> None:
    from ptax.eval.cli import _report_year_bias

    _report_year_bias(
        {
            "a": {"base_building_frac": 0.2, "target_building_frac": 0.3},
            "b": {"base_building_frac": 0.4, "target_building_frac": 0.4},
        }
    )

    out = capsys.readouterr().out
    assert "base_building_frac" in out and "(n=2)" in out
    assert "n=0" not in out


def test_the_year_bias_report_is_silent_without_per_year_fractions(capsys) -> None:
    from ptax.eval.cli import _report_year_bias

    _report_year_bias({"a": {"structure_m2": 10.0}})

    assert "agree closely" not in capsys.readouterr().out
