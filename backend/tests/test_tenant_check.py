import json
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from moto import mock_aws
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ptax import cli
from ptax.config import Settings
from ptax.db.models import Tenant
from ptax.tenancy import check as check_module
from ptax.tenancy.check import check_config
from ptax.tenancy.config import TenantConfig
from ptax.tenancy.secrets import SecretError, arcgis_credentials
from ptax.tenancy.store import put_config

EXAMPLE = Path(__file__).parents[1] / "tenants" / "peoria-il.example.json"
CLIENT_SECRET = "s3cr3t-value-that-must-never-print"
TOKEN = "tok-that-must-never-print"
runner = CliRunner()


class FakeArcgis:
    """Portal token endpoint plus every service in the example config."""

    def __init__(self) -> None:
        self.status: dict[str, int] = {}
        self.token_error = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://www.arcgis.com/sharing/rest/oauth2/token"):
            if self.token_error:
                return httpx.Response(
                    200, json={"error": {"code": 400, "message": "Invalid client_id"}}
                )
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 7200})
        for fragment, status in self.status.items():
            if fragment in url:
                return httpx.Response(status, text="down")
        return httpx.Response(200, json={"currentVersion": 11.3})


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("secretsmanager", region_name="us-east-1")
        arn = client.create_secret(
            Name="ptax/tenants/t1/arcgis",
            SecretString=json.dumps({"client_id": "app-id", "client_secret": CLIENT_SECRET}),
        )["ARN"]
        yield arn


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeArcgis:
    fake = FakeArcgis()
    monkeypatch.setattr(
        check_module, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(fake))
    )
    return fake


def _config(arn: str, **changes: Any) -> TenantConfig:
    raw = json.loads(EXAMPLE.read_text())
    raw["secret_arn"] = arn
    raw.update(changes)
    return TenantConfig.model_validate(raw)


def _by_item(results) -> dict[str, str]:  # noqa: ANN001
    return {r.item: r.status for r in results}


def test_credentials_come_from_secrets_manager_and_stay_hidden(aws: str) -> None:
    creds = arcgis_credentials(_config(aws))
    assert creds.client_id == "app-id"
    assert creds.client_secret.get_secret_value() == CLIENT_SECRET
    assert CLIENT_SECRET not in repr(creds) and CLIENT_SECRET not in str(creds)

    missing = _config(aws.replace("ptax/tenants/t1/arcgis", "ptax/tenants/nope/arcgis"))
    with pytest.raises(SecretError) as caught:
        arcgis_credentials(missing)
    assert "nope" in str(caught.value)


def test_a_secret_without_the_expected_fields_is_refused_without_echoing_it(aws: str) -> None:
    boto3.client("secretsmanager", region_name="us-east-1").put_secret_value(
        SecretId=aws, SecretString=json.dumps({"password": CLIENT_SECRET})
    )
    with pytest.raises(SecretError) as caught:
        arcgis_credentials(_config(aws))
    assert CLIENT_SECRET not in str(caught.value)
    # Raised `from None`: a traceback cannot surface the parse error that quoted the input.
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_every_endpoint_answering_is_all_ok(aws: str, fake: FakeArcgis) -> None:
    results = check_config(_config(aws))
    assert _by_item(results) == {
        "secret": "ok",
        "portal token": "ok",
        "parcels": "ok",
        "imagery 2015": "ok",
        "imagery 2019": "ok",
        "footprints": "ok",
    }


def test_a_failing_required_endpoint_is_reported(aws: str, fake: FakeArcgis) -> None:
    fake.status["Tax_Parcels"] = 500
    fake.status["Building_Outlines"] = 404
    results = {r.item: r for r in check_config(_config(aws))}
    assert results["parcels"].status == "unreachable" and results["parcels"].required
    # Footprints are optional: reported, but never required.
    assert results["footprints"].status == "unreachable" and not results["footprints"].required


def test_a_rejected_token_is_denied_without_leaking_the_secret(aws: str, fake: FakeArcgis) -> None:
    fake.token_error = True
    results = {r.item: r for r in check_config(_config(aws))}
    assert results["portal token"].status == "denied"
    assert all(CLIENT_SECRET not in r.reason and TOKEN not in r.reason for r in results.values())


def test_a_files_source_is_checked_in_s3(aws: str, fake: FakeArcgis, settings: Settings) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="drops")
    s3.put_object(Bucket="drops", Key="tenants/t1/raw/2019/tile.tif", Body=b"x")
    raw = json.loads(EXAMPLE.read_text())
    raw["imagery"]["2019"] = {
        **raw["imagery"]["2019"],
        "source": "files",
        "url_or_s3_uri": "s3://drops/tenants/t1/raw/2019/",
    }
    raw["imagery"]["2015"] = {
        **raw["imagery"]["2015"],
        "source": "files",
        "url_or_s3_uri": "s3://drops/tenants/t1/raw/2015/",
    }
    results = _by_item(check_config(_config(aws, imagery=raw["imagery"]), s3=s3))
    assert results["imagery 2019"] == "ok"
    assert results["imagery 2015"] == "unreachable"


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, db: Session, settings: Settings) -> Session:
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(cli, "_session", lambda: nullcontext(db))
    return db


def test_the_check_command_prints_one_line_per_item_and_fails_on_required(
    cli_db: Session, aws: str, fake: FakeArcgis
) -> None:
    tenant = Tenant(name="Check County", state="IL", fips="92001")
    cli_db.add(tenant)
    cli_db.flush()
    put_config(cli_db, tenant.id, _config(aws))

    ok = runner.invoke(cli.app, ["tenant-config", "check", "--tenant-fips", "92001"])
    assert ok.exit_code == 0, ok.output
    assert ok.output.count("ok") >= 6

    fake.status["Tax_Parcels"] = 500
    failed = runner.invoke(cli.app, ["tenant-config", "check", "--tenant-fips", "92001"])
    assert failed.exit_code == 1
    assert "parcels" in failed.output and "unreachable" in failed.output
    for output in (ok.output, failed.output):
        assert CLIENT_SECRET not in output and TOKEN not in output
