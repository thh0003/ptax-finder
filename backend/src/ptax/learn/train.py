"""Train the building segmenter on the training chips and freeze it with a model card.

Everything that decides the model -- when to stop, which checkpoint to keep, the
probability cutoff -- is chosen on the *training* validation split: whole tiles held out
of the training AOIs (`ptax.eval.training_data`). The evaluation AOI and its visual labels
are never read here; the manifest's AOIs are re-checked against it before any chip loads.

The card is written last, with the weights' sha256 and a ``frozen_at`` time. The detector
refuses weights whose hash differs from the card, so the candidate the decision gate
scores is provably the one this run froze. A retrain after seeing an evaluation score is
a new candidate under a new name, never a silent replacement.
"""

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import segmentation_models_pytorch as smp
import torch
from torch.nn import functional as F

from ptax.eval.training_data import CHIP_PX, assert_disjoint_from_eval
from ptax.learn.model import ENCODER, build_model, normalise, pick_device

MAX_EPOCHS = 30
PATIENCE = 5
BATCH_SIZE = 16
LEARNING_RATE = 3e-4
SEED = 20260923
#: Share of training samples cut down to a parcel-sized crop, and the crop size range.
#: Inference runs on parcel-only rasters with no surrounding context, some under 32 px
#: across, so the model has to have seen buildings with little or no context around them.
CROP_PROBABILITY = 0.5
MIN_CROP_PX = 24
#: Cutoffs tried on the validation split once training has stopped.
CUTOFF_CANDIDATES = tuple(round(0.2 + 0.05 * i, 2) for i in range(13))
#: Side of the parcel-sized validation crops reported beside full-chip IoU.
SMALL_CROP_PX = 48


def iou(prediction: np.ndarray, truth: np.ndarray, valid: np.ndarray) -> float:
    """Building intersection-over-union over ``valid`` pixels; 1.0 when both are empty."""
    p, t = prediction & valid, truth & valid
    union = int((p | t).sum())
    return float((p & t).sum()) / union if union else 1.0


def _pooled_iou(
    probabilities: list[np.ndarray],
    truths: list[np.ndarray],
    valids: list[np.ndarray],
    cutoff: float,
) -> float:
    """IoU over every pixel of every chip at once, so empty chips do not count as perfect."""
    intersection = union = 0
    for probability, truth, valid in zip(probabilities, truths, valids, strict=True):
        p, t = (probability >= cutoff) & valid, truth & valid
        intersection += int((p & t).sum())
        union += int((p | t).sum())
    return intersection / union if union else 1.0


def choose_cutoff(
    probabilities: list[np.ndarray],
    truths: list[np.ndarray],
    valids: list[np.ndarray],
    candidates: tuple[float, ...] = CUTOFF_CANDIDATES,
) -> tuple[float, dict[float, float]]:
    """The candidate cutoff with the best pooled IoU, and every candidate's IoU."""
    scores = {c: _pooled_iou(probabilities, truths, valids, c) for c in candidates}
    best = max(candidates, key=lambda c: (scores[c], -abs(c - 0.5)))
    return best, scores


def random_crop(
    image: np.ndarray,
    label: np.ndarray,
    valid: np.ndarray,
    size: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A ``size`` px square from a random place, set in the top-left of an empty canvas.

    That is how a parcel arrives at inference: its own pixels at the top-left of a
    zero-padded canvas (`ptax.learn.model.Predictor`), nothing around it.
    """
    height, width = label.shape
    top = int(rng.integers(0, height - size + 1))
    left = int(rng.integers(0, width - size + 1))
    out_image = np.zeros_like(image)
    out_label = np.zeros_like(label)
    out_valid = np.zeros_like(valid)
    window = (slice(top, top + size), slice(left, left + size))
    out_valid[:size, :size] = valid[window]
    out_label[:size, :size] = label[window] & valid[window]
    out_image[:, :size, :size] = image[:, window[0], window[1]]
    out_image[:, ~out_valid] = 0
    return out_image, out_label, out_valid


def _augment(
    image: np.ndarray, label: np.ndarray, valid: np.ndarray, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotation, flip, radiometric jitter and, half the time, a parcel-sized crop."""
    turns = int(rng.integers(0, 4))
    image, label, valid = (np.rot90(a, turns, axes=(-2, -1)) for a in (image, label, valid))
    if rng.random() < 0.5:
        image, label, valid = (a[..., ::-1] for a in (image, label, valid))
    # Captures differ in exposure and contrast from year to year; the model must not
    # learn that a roof is one particular brightness.
    contrast = rng.uniform(0.75, 1.25)
    brightness = rng.uniform(-25.0, 25.0)
    jittered = np.clip(image.astype(np.float32) * contrast + brightness, 0, 255)
    image = jittered.astype(np.uint8)
    image[:, ~valid] = 0
    label, valid = np.ascontiguousarray(label), np.ascontiguousarray(valid)
    image = np.ascontiguousarray(image)
    if rng.random() < CROP_PROBABILITY:
        # Log-uniform, so small parcels are sampled as often as mid-sized ones.
        size = int(round(math.exp(rng.uniform(math.log(MIN_CROP_PX), math.log(CHIP_PX)))))
        image, label, valid = random_crop(image, label, valid, size, rng)
    return image, label, valid


@dataclass
class Split:
    images: np.ndarray  # (n, 3, h, w) uint8
    labels: np.ndarray  # (n, h, w) bool
    valid: np.ndarray  # (n, h, w) bool
    years: np.ndarray  # (n,) int


def load_split(manifest_path: Path, split: str) -> Split:
    """Every chip of ``split`` named by the manifest, after re-checking its AOIs."""
    manifest = json.loads(manifest_path.read_text())
    images, labels, valids, years = [], [], [], []
    for record in manifest["aois"].values():
        if "dropped" in record:
            continue
        assert_disjoint_from_eval(tuple(record["bbox"]))
        for year, shard_record in sorted(record["years"].items()):
            if "shard" not in shard_record:
                continue
            with np.load(shard_record["shard"]) as shard:
                keep = shard["split"] == split
                images.append(shard["images"][keep])
                labels.append(shard["labels"][keep])
                valids.append(shard["valid"][keep])
                years.append(np.full(int(keep.sum()), int(year)))
    return Split(
        images=np.concatenate(images),
        labels=np.concatenate(labels),
        valid=np.concatenate(valids),
        years=np.concatenate(years),
    )


def _loss(logits: torch.Tensor, label: torch.Tensor, valid: torch.Tensor) -> torch.Tensor:
    """Binary cross-entropy plus soft Dice, both over valid pixels only."""
    weight = valid.float()
    bce = F.binary_cross_entropy_with_logits(logits, label.float(), weight=weight, reduction="sum")
    bce = bce / weight.sum().clamp(min=1.0)
    probability = torch.sigmoid(logits) * weight
    target = label.float() * weight
    dice = 1.0 - (2.0 * (probability * target).sum() + 1.0) / (
        probability.sum() + target.sum() + 1.0
    )
    return bce + dice


@torch.no_grad()
def _predict(model: torch.nn.Module, images: np.ndarray, device: torch.device) -> list[np.ndarray]:
    model.eval()
    out: list[np.ndarray] = []
    for start in range(0, len(images), BATCH_SIZE):
        batch = torch.from_numpy(np.ascontiguousarray(images[start : start + BATCH_SIZE]))
        logits = model(normalise(batch.to(device)))
        out.extend(torch.sigmoid(logits)[:, 0].cpu().numpy().astype(np.float32))
    return out


def _small_crops(split: Split) -> Split:
    """Each validation chip's top-left ``SMALL_CROP_PX`` square on an empty canvas."""
    images = np.zeros_like(split.images)
    labels = np.zeros_like(split.labels)
    valid = np.zeros_like(split.valid)
    s = SMALL_CROP_PX
    images[:, :, :s, :s] = split.images[:, :, :s, :s]
    labels[:, :s, :s] = split.labels[:, :s, :s]
    valid[:, :s, :s] = split.valid[:, :s, :s]
    return Split(images, labels, valid, split.years)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def train(
    manifest_path: Path,
    models_dir: Path,
    name: str,
    *,
    max_epochs: int = MAX_EPOCHS,
    patience: int = PATIENCE,
    seed: int = SEED,
    device: str | None = None,
    echo: Any = print,
) -> dict[str, Any]:
    """Train, pick the cutoff on validation, and freeze weights plus card under ``name``."""
    card_path = models_dir / f"{name}.json"
    weights_path = models_dir / f"{name}.pt"
    if card_path.exists():
        raise FileExistsError(
            f"{card_path} is a frozen candidate; train a new one under a new name"
        )
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    target = pick_device(device)

    train_split = load_split(manifest_path, "train")
    validation = load_split(manifest_path, "validation")
    echo(f"{len(train_split.images)} training chips, {len(validation.images)} validation chips")

    model = build_model(pretrained=True).to(target)
    optimiser = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE)
    best_iou, best_epoch, best_state = -1.0, 0, None
    history: list[dict[str, float]] = []
    stopped = f"epoch cap ({max_epochs})"
    started = time.monotonic()

    for epoch in range(1, max_epochs + 1):
        model.train()
        order = rng.permutation(len(train_split.images))
        total = 0.0
        for start in range(0, len(order), BATCH_SIZE):
            picked = [
                _augment(train_split.images[i], train_split.labels[i], train_split.valid[i], rng)
                for i in order[start : start + BATCH_SIZE]
            ]
            images = torch.from_numpy(np.stack([p[0] for p in picked])).to(target)
            labels = torch.from_numpy(np.stack([p[1] for p in picked])).to(target)
            valid = torch.from_numpy(np.stack([p[2] for p in picked])).to(target)
            logits = model(normalise(images))[:, 0]
            loss = _loss(logits, labels, valid)
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
            total += float(loss) * len(picked)

        probabilities = _predict(model, validation.images, target)
        epoch_iou = _pooled_iou(probabilities, list(validation.labels), list(validation.valid), 0.5)
        history.append(
            {
                "epoch": epoch,
                "train_loss": round(total / len(order), 4),
                "val_iou": round(epoch_iou, 4),
            }
        )
        echo(f"  epoch {epoch:2d}: loss {total / len(order):.4f}, validation IoU {epoch_iou:.4f}")
        if epoch_iou > best_iou:
            best_iou, best_epoch = epoch_iou, epoch
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        elif epoch - best_epoch >= patience:
            stopped = f"early stop: no validation IoU gain for {patience} epochs"
            break

    assert best_state is not None
    model.load_state_dict(best_state)
    probabilities = _predict(model, validation.images, target)
    truths, valids = list(validation.labels), list(validation.valid)
    cutoff, cutoff_scores = choose_cutoff(probabilities, truths, valids)

    per_year: dict[str, float] = {}
    for year in sorted(set(validation.years.tolist())):
        members = np.flatnonzero(validation.years == year)
        per_year[str(year)] = round(
            _pooled_iou(
                [probabilities[i] for i in members],
                [truths[i] for i in members],
                [valids[i] for i in members],
                cutoff,
            ),
            4,
        )
    small = _small_crops(validation)
    small_iou = _pooled_iou(
        _predict(model, small.images, target), list(small.labels), list(small.valid), cutoff
    )

    models_dir.mkdir(parents=True, exist_ok=True)
    torch.save(best_state, weights_path)
    card: dict[str, Any] = {
        "name": name,
        "architecture": "Unet",
        "encoder": ENCODER,
        "encoder_weights": "imagenet",
        "input": "RGB uint8, ImageNet normalisation, zero-padded top-left to >= 256 px",
        "resolution_m": 1.0,
        "seed": seed,
        "device": str(target),
        "note": "MPS kernels are not bit-deterministic; a rerun reproduces the recipe, "
        "not the exact weights. The weights_sha256 identifies this run's weights.",
        "batch_size": BATCH_SIZE,
        "learning_rate": LEARNING_RATE,
        "loss": "masked BCE + soft Dice",
        "augmentation": {
            "rot90_flip": True,
            "contrast": [0.75, 1.25],
            "brightness": [-25, 25],
            "crop_probability": CROP_PROBABILITY,
            "crop_px": [MIN_CROP_PX, CHIP_PX],
        },
        "max_epochs": max_epochs,
        "patience": patience,
        "epochs_run": len(history),
        "best_epoch": best_epoch,
        "stopped": stopped,
        "history": history,
        "training_seconds": round(time.monotonic() - started),
        "data_manifest": str(manifest_path),
        "data_manifest_sha256": _sha256(manifest_path),
        "train_chips": int(len(train_split.images)),
        "validation_chips": int(len(validation.images)),
        "train_chips_per_year": {
            str(y): int((train_split.years == y).sum()) for y in sorted(set(train_split.years))
        },
        "validation_iou_at_0_5": round(best_iou, 4),
        "cutoff": cutoff,
        "cutoff_iou": {str(c): round(v, 4) for c, v in cutoff_scores.items()},
        "validation_iou": round(cutoff_scores[cutoff], 4),
        "validation_iou_per_year": per_year,
        "validation_iou_small_crops": round(small_iou, 4),
        "small_crop_px": SMALL_CROP_PX,
        "chosen_on": "training validation split only (held-out tiles of the training AOIs)",
        "torch": torch.__version__,
        "segmentation_models_pytorch": smp.__version__,
        "weights": weights_path.name,
        "weights_sha256": _sha256(weights_path),
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    card_path.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n")
    return card
