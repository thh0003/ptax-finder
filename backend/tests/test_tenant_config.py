import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ptax import cli
from ptax.config import Settings
from ptax.db.models import Tenant
from ptax.tenancy.config import DEFAULT_CLASSES, TenantConfig
from ptax.tenancy.store import get_config, put_config

EXAMPLE = Path(__file__).parents[1] / "tenants" / "peoria-il.example.json"
runner = CliRunner()


def _example() -> dict[str, Any]:
    return json.loads(EXAMPLE.read_text())


def test_the_example_config_validates_with_the_default_classes_and_thresholds() -> None:
    config = TenantConfig.model_validate(_example())
    assert sorted(config.imagery) == [2015, 2019]
    assert config.classes == list(DEFAULT_CLASSES)
    assert config.thresholds.min_new_area_sqft == 100
    assert config.footprints is not None and config.cama is None
    assert config.publish.publish_all is False


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda c: c["imagery"].pop("2019"), "imagery"),
        (lambda c: c.update(crs_epsg=4326), "crs_epsg"),
        (lambda c: c.update(portal_url="http://www.arcgis.com"), "portal_url"),
        (lambda c: c["imagery"]["2015"].update(year=2014), "imagery"),
        (lambda c: c["imagery"]["2015"].update(url_or_s3_uri="s3://bucket/x"), "imagery"),
        (lambda c: c.update(portal_type="geoserver"), "portal_type"),
        (lambda c: c.update(thresholds={"iou_match": 1.5}), "thresholds"),
        (lambda c: c.update(secret_arn="client-secret-in-the-clear"), "secret_arn"),
    ],
)
def test_an_invalid_config_is_refused_naming_the_field(change, field: str) -> None:
    raw = _example()
    change(raw)
    with pytest.raises(ValidationError) as caught:
        TenantConfig.model_validate(raw)
    assert field in {str(e["loc"][0]) for e in caught.value.errors()}


def _tenant(db: Session, fips: str) -> Tenant:
    tenant = Tenant(name=f"County {fips}", state="IL", fips=fips)
    db.add(tenant)
    db.flush()
    return tenant


def test_a_config_is_stored_per_tenant_and_versioned(db: Session) -> None:
    a, b = _tenant(db, "91001"), _tenant(db, "91002")
    config = TenantConfig.model_validate(_example())

    assert put_config(db, a.id, config) == 1
    assert put_config(db, a.id, config) == 2
    assert get_config(db, a.id) == config
    # Tenant B's scope cannot see tenant A's row.
    assert get_config(db, b.id) is None


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, db: Session, settings: Settings) -> Session:
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    @contextmanager
    def session() -> Iterator[Session]:
        # Like a real session: a commit expires what was loaded and the exit detaches it,
        # so a command reading an ORM attribute after its `with` block fails here too.
        yield db
        db.expire_all()
        db.expunge_all()

    monkeypatch.setattr(cli, "_session", session)
    return db


def test_the_operator_sets_and_shows_a_config(cli_db: Session, tmp_path: Path) -> None:
    fips = _tenant(cli_db, "91003").fips
    result = runner.invoke(cli.app, ["tenant-config", "set", str(EXAMPLE), "--tenant-fips", fips])
    assert result.exit_code == 0, result.output
    assert "version 1" in result.output

    shown = runner.invoke(cli.app, ["tenant-config", "show", "--tenant-fips", fips])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["county_name"] == "Peoria County"

    bad = tmp_path / "bad.json"
    raw = _example()
    raw["crs_epsg"] = 4326
    raw["portal_url"] = "http://www.arcgis.com"
    bad.write_text(json.dumps(raw))
    refused = runner.invoke(cli.app, ["tenant-config", "set", str(bad), "--tenant-fips", fips])
    assert refused.exit_code == 1
    assert "crs_epsg" in refused.output and "portal_url" in refused.output


def test_show_reports_a_tenant_with_no_config(cli_db: Session) -> None:
    tenant = _tenant(cli_db, "91004")
    shown = runner.invoke(cli.app, ["tenant-config", "show", "--tenant-fips", tenant.fips])
    assert shown.exit_code == 1
    assert "no pipeline config" in shown.output
