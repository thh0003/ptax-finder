"""Frozen segmenter models in the uploads bucket, shared by the API and the worker.

The API decides which model a run will use and records its weights' sha256; the worker,
in a different image, loads that model and must prove it is the one recorded. The bucket
is what they share, so this module keeps three promises about it:

- **A visible card means its weights are there.** Weights are uploaded before the card, so
  the API can never record a model the worker cannot fetch.
- **A frozen name is never overwritten.** Re-publishing identical bytes is a no-op;
  different weights under a published name are refused. A retrained model is a new name.
- **Nothing reaches a run unless its hash matches.** Fetched weights are hashed before
  they leave a temporary file.

Torch-free: the API imports this, and the API image has no torch.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from ptax.config import Settings
from ptax.storage import get_s3_client

MODEL_PREFIX = "models/"
#: Names become S3 keys, so they are restricted to what cannot leave the prefix.
_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


class ModelMismatch(Exception):
    """Weights whose sha256 is not the one a card or a run recorded."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(name: str, suffix: str) -> str:
    if not _NAME.match(name) or ".." in name:
        raise ValueError(f"invalid model name {name!r}")
    return f"{MODEL_PREFIX}{name}{suffix}"


def published_card(settings: Settings, name: str) -> dict[str, Any] | None:
    """The card published under ``name``, or None when there is none."""
    key = _key(name, ".json")
    try:
        body = get_s3_client(settings).get_object(Bucket=settings.s3_bucket, Key=key)["Body"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    card: dict[str, Any] = json.loads(body.read())
    return card


def publish(settings: Settings, card_path: Path) -> dict[str, Any]:
    """Upload a frozen card and the weights beside it; return the published card.

    Refuses weights that do not match the card, and a name already published with
    different weights.
    """
    card: dict[str, Any] = json.loads(card_path.read_text())
    name = str(card["name"])
    weights = card_path.parent / card["weights"]
    actual = sha256_file(weights)
    if actual != card["weights_sha256"]:
        raise ModelMismatch(
            f"{weights} has sha256 {actual}, but {card_path} froze {card['weights_sha256']}"
        )
    existing = published_card(settings, name)
    if existing is not None:
        if existing.get("weights_sha256") == actual:
            return existing
        raise ModelMismatch(
            f"{name} is already published with weights {existing.get('weights_sha256')};"
            " a retrained model needs a new name"
        )
    client = get_s3_client(settings)
    client.upload_file(str(weights), settings.s3_bucket, _key(name, ".pt"))
    # The card last: once it is visible, its weights are too.
    client.put_object(
        Bucket=settings.s3_bucket,
        Key=_key(name, ".json"),
        Body=json.dumps(card, indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )
    return card


def fetch_weights(settings: Settings, name: str, expected_sha256: str, cache_dir: Path) -> Path:
    """Local weights for ``name`` whose sha256 is ``expected_sha256``, downloading once.

    The cache file is keyed by hash as well as name, so a worker that served an older
    model never mistakes its cached file for a new one.
    """
    target = cache_dir / f"{name}-{expected_sha256[:12]}.pt"
    if target.exists() and sha256_file(target) == expected_sha256:
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    get_s3_client(settings).download_file(settings.s3_bucket, _key(name, ".pt"), str(partial))
    actual = sha256_file(partial)
    if actual != expected_sha256:
        partial.unlink()
        raise ModelMismatch(
            f"published weights for {name} have sha256 {actual}, but the run recorded"
            f" {expected_sha256}"
        )
    partial.replace(target)
    return target
