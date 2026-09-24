"""Publishing and fetching frozen segmenter models through the uploads bucket.

The API records which model a run will use and the worker loads it, from different
images on different machines. The bucket is what they share, so these tests pin what it
must guarantee: a card is only visible once its weights are, a frozen name is never
overwritten with different weights, and nothing reaches a run unless its hash matches.
Runs against the local MinIO bucket, with a unique model name per test.
"""

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from ptax.config import Settings
from ptax.detection.model_store import (
    ModelMismatch,
    fetch_weights,
    publish,
    published_card,
)


def _model(tmp_path: Path, weights: bytes, *, recorded: bytes | None = None) -> Path:
    name = f"segmenter-test-{uuid.uuid4().hex[:10]}"
    (tmp_path / f"{name}.pt").write_bytes(weights)
    card = tmp_path / f"{name}.json"
    digest = hashlib.sha256(recorded if recorded is not None else weights).hexdigest()
    card.write_text(
        json.dumps({"name": name, "weights": f"{name}.pt", "weights_sha256": digest, "cutoff": 0.4})
    )
    return card


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_published_model_can_be_read_back_and_fetched(settings: Settings, tmp_path) -> None:
    card = _model(tmp_path, b"weights-v1")
    name = json.loads(card.read_text())["name"]

    publish(settings, card)

    assert published_card(settings, name)["weights_sha256"] == _sha(b"weights-v1")
    fetched = fetch_weights(settings, name, _sha(b"weights-v1"), tmp_path / "cache")
    assert fetched.read_bytes() == b"weights-v1"


def test_an_unpublished_model_has_no_card(settings: Settings) -> None:
    assert published_card(settings, f"segmenter-absent-{uuid.uuid4().hex[:10]}") is None


def test_weights_that_do_not_match_their_card_are_never_uploaded(
    settings: Settings, tmp_path
) -> None:
    card = _model(tmp_path, b"retrained", recorded=b"frozen")
    name = json.loads(card.read_text())["name"]

    with pytest.raises(ModelMismatch):
        publish(settings, card)

    assert published_card(settings, name) is None


def test_a_frozen_name_is_never_overwritten_with_different_weights(
    settings: Settings, tmp_path
) -> None:
    (tmp_path / "a").mkdir()
    first = _model(tmp_path / "a", b"weights-v1")
    name = json.loads(first.read_text())["name"]
    publish(settings, first)
    publish(settings, first)  # identical bytes: a no-op, not an error

    other = tmp_path / "b"
    other.mkdir()
    (other / f"{name}.pt").write_bytes(b"weights-v2")
    (other / f"{name}.json").write_text(
        json.dumps({"name": name, "weights": f"{name}.pt", "weights_sha256": _sha(b"weights-v2")})
    )
    with pytest.raises(ModelMismatch, match=name):
        publish(settings, other / f"{name}.json")

    assert published_card(settings, name)["weights_sha256"] == _sha(b"weights-v1")


def test_fetching_with_the_wrong_hash_is_refused_and_caches_nothing(
    settings: Settings, tmp_path
) -> None:
    card = _model(tmp_path, b"weights-v1")
    name = json.loads(card.read_text())["name"]
    publish(settings, card)
    cache = tmp_path / "cache"

    with pytest.raises(ModelMismatch):
        fetch_weights(settings, name, _sha(b"something else"), cache)

    assert not list(cache.glob("*")) if cache.exists() else True


def test_a_model_name_cannot_escape_the_models_prefix(settings: Settings) -> None:
    with pytest.raises(ValueError):
        published_card(settings, "../tenants/secret")


def test_the_operator_command_publishes_and_refuses_a_changed_model(
    settings: Settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from ptax import cli

    monkeypatch.setattr(cli, "_settings", lambda: settings)
    card = _model(tmp_path, b"weights-v1")
    name = json.loads(card.read_text())["name"]
    runner = CliRunner()

    result = runner.invoke(cli.app, ["model-publish", str(card)])
    assert result.exit_code == 0, result.output
    assert name in result.output

    (tmp_path / f"{name}.pt").write_bytes(b"weights-v2")
    card.write_text(
        json.dumps({"name": name, "weights": f"{name}.pt", "weights_sha256": _sha(b"weights-v2")})
    )
    result = runner.invoke(cli.app, ["model-publish", str(card)])
    assert result.exit_code == 1
    assert "new name" in result.output
