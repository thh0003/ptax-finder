"""The building segmenter's inference and training helpers, on random input.

Accuracy is measured by `ptax-eval train` on held-out tiles and by `ptax-eval score`
against the visual labels, not here. These tests pin the contracts the detector relies on:
any parcel-sized input comes back as a same-sized probability map, and the training
helpers score and crop the way the model card says they do.

Skipped without the optional ``ml`` dependency group, so the default suite runs without
torch exactly as the production image does.
"""

from pathlib import Path

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from ptax.learn.model import build_model, load  # noqa: E402
from ptax.learn.train import choose_cutoff, iou, random_crop  # noqa: E402


@pytest.fixture(scope="module")
def weights(tmp_path_factory: pytest.TempPathFactory) -> Path:
    torch.manual_seed(0)
    path = tmp_path_factory.mktemp("model") / "weights.pt"
    torch.save(build_model(pretrained=False).state_dict(), path)
    return path


@pytest.mark.parametrize("shape", [(37, 53), (24, 24), (300, 290)])
def test_predict_returns_a_probability_per_input_pixel(weights: Path, shape) -> None:
    predict = load(weights, device="cpu")
    rgb = np.random.default_rng(1).integers(0, 256, (3, *shape), dtype=np.uint8)

    probability = predict(rgb)

    assert probability.shape == shape
    assert probability.dtype == np.float32
    assert 0.0 <= probability.min() and probability.max() <= 1.0


def test_iou_counts_only_valid_pixels() -> None:
    truth = np.zeros((4, 4), bool)
    truth[:2, :2] = True
    guess = np.zeros((4, 4), bool)
    guess[:2, :3] = True
    valid = np.ones((4, 4), bool)

    assert iou(guess, truth, valid) == pytest.approx(4 / 6)
    valid[:, 2] = False  # the one wrong column is outside the imagery
    assert iou(guess, truth, valid) == pytest.approx(1.0)


def test_the_cutoff_is_the_one_with_the_best_iou() -> None:
    truth = np.array([[True, True, False, False]])
    probability = np.array([[0.9, 0.6, 0.4, 0.1]], dtype=np.float32)
    valid = np.ones_like(truth)

    cutoff, scores = choose_cutoff([probability], [truth], [valid], (0.3, 0.5, 0.7))

    assert cutoff == 0.5
    assert scores[0.5] == pytest.approx(1.0)


def test_a_random_crop_sits_in_the_corner_and_the_rest_is_invalid() -> None:
    """Inference sees parcel-only crops, zero-padded at the bottom and right."""
    rng = np.random.default_rng(3)
    image = np.full((3, 256, 256), 200, np.uint8)
    label = np.ones((256, 256), bool)
    valid = np.ones((256, 256), bool)

    out_image, out_label, out_valid = random_crop(image, label, valid, 40, rng)

    assert out_image.shape == (3, 256, 256)
    assert out_valid[:40, :40].all() and out_valid.sum() == 40 * 40
    assert (out_image[:, ~out_valid] == 0).all()
    assert not out_label[~out_valid].any()
