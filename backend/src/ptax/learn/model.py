"""Build, load and run the building segmenter.

A U-Net with an ImageNet-pretrained ResNet-34 encoder (``segmentation-models-pytorch``),
one output channel: the probability that a pixel is building. It is small enough to run
on CPU, which matters because production workers have no GPU.

Input is RGB only, so imagery from an RGB-only upload and from four-band NAIP go through
the same model. Every input is normalised with fixed ImageNet statistics rather than per
chip: a parcel-only crop can be all roof or all lawn, and stretching each one to its own
range would make the same roof read differently on two parcels. Capture-to-capture
radiometric differences are handled in training instead, by brightness and contrast
jitter across four NAIP years.
"""

from pathlib import Path

import numpy as np
import segmentation_models_pytorch as smp
import torch
from torch import nn

ENCODER = "resnet34"
#: Smallest canvas the model sees. Training chips are 256 px, and training crops smaller
#: than that are zero-padded into a 256 px canvas at the top-left; inference pads the
#: same way so a 30 px parcel arrives exactly as training presented one.
CANVAS_PX = 256
#: The U-Net downsamples five times, so each side must be a multiple of 2**5.
STRIDE = 32

_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
_STD = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)


def pick_device(preferred: str | None = None) -> torch.device:
    """``preferred`` if given, else Apple's ``mps`` when present, else ``cpu``."""
    if preferred is not None:
        return torch.device(preferred)
    return torch.device("mps" if torch.backends.mps.is_available() else "cpu")


def build_model(pretrained: bool = True) -> nn.Module:
    """The U-Net; ``pretrained`` loads ImageNet encoder weights (a one-time download)."""
    model: nn.Module = smp.Unet(
        encoder_name=ENCODER,
        encoder_weights="imagenet" if pretrained else None,
        in_channels=3,
        classes=1,
    )
    return model


def normalise(batch: torch.Tensor) -> torch.Tensor:
    """uint8 RGB ``(n, 3, h, w)`` to the float input the encoder was pretrained on."""
    x = batch.float() / 255.0
    return (x - _MEAN.to(x.device)) / _STD.to(x.device)


def padded_size(side: int) -> int:
    """The canvas side for an input side: at least ``CANVAS_PX``, a multiple of ``STRIDE``."""
    return max(CANVAS_PX, -(-side // STRIDE) * STRIDE)


class Predictor:
    """``predict(rgb) -> probability`` for one parcel raster, on a fixed device."""

    def __init__(self, model: nn.Module, device: torch.device) -> None:
        self.model = model.to(device).eval()
        self.device = device

    @torch.no_grad()
    def __call__(self, rgb: np.ndarray) -> np.ndarray:
        """``(3, h, w)`` uint8 RGB to ``(h, w)`` float32 building probability."""
        _, height, width = rgb.shape
        canvas = np.zeros((1, 3, padded_size(height), padded_size(width)), dtype=np.uint8)
        canvas[0, :, :height, :width] = rgb[:3]
        logits = self.model(normalise(torch.from_numpy(canvas).to(self.device)))
        probability = torch.sigmoid(logits)[0, 0, :height, :width]
        return probability.cpu().numpy().astype(np.float32)


def load(weights: Path, device: str | None = None) -> Predictor:
    """A predictor over trained weights. Does not check them: `ptax.detection.learned` does."""
    model = build_model(pretrained=False)
    state = torch.load(weights, map_location="cpu", weights_only=True)
    model.load_state_dict(state)
    return Predictor(model, pick_device(device))
