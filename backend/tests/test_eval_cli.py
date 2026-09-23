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
