from contextlib import nullcontext
from pathlib import Path

import boto3
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ptax import cli
from ptax.auth.cognito import CognitoAdmin
from ptax.config import Settings
from ptax.db.models import Tenant, User

runner = CliRunner()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, db: Session, cognito_settings: Settings) -> Settings:
    """Route the CLI at the test session and the moto pool."""
    monkeypatch.setattr(cli, "_settings", lambda: cognito_settings)
    monkeypatch.setattr(cli, "_session", lambda: nullcontext(db))
    return cognito_settings


def test_create_tenant_and_user(cli_env: Settings, db: Session) -> None:
    result = runner.invoke(
        cli.app, ["create-tenant", "--name", "Demo County", "--state", "MN", "--fips", "27053"]
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        cli.app,
        [
            "create-user",
            "--tenant-fips",
            "27053",
            "--email",
            "admin@demo.test",
            "--role",
            "admin",
            "--password",
            "Password1!",
            "--permanent",
        ],
    )
    assert result.exit_code == 0, result.output

    tenant = db.execute(select(Tenant).where(Tenant.fips == "27053")).scalar_one()
    user = db.execute(select(User).where(User.email == "admin@demo.test")).scalar_one()
    assert user.tenant_id == tenant.id
    assert user.role == "admin"

    cognito_user = CognitoAdmin(cli_env).get_user("admin@demo.test")
    assert cognito_user["sub"] == user.cognito_sub


def test_duplicate_fips_fails_clearly(cli_env: Settings) -> None:
    args = ["create-tenant", "--name", "A", "--state", "MN", "--fips", "27099"]
    assert runner.invoke(cli.app, args).exit_code == 0
    result = runner.invoke(cli.app, args)
    assert result.exit_code != 0
    assert "27099" in result.output and "already exists" in result.output


def test_create_user_for_unknown_tenant_fails(cli_env: Settings) -> None:
    result = runner.invoke(
        cli.app, ["create-user", "--tenant-fips", "00000", "--email", "x@x.test", "--role", "admin"]
    )
    assert result.exit_code != 0
    assert "00000" in result.output


def _read_env(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def test_bootstrap_local_cognito_is_idempotent(cli_env: Settings, tmp_path: Path) -> None:
    backend_env = tmp_path / ".env"
    backend_env.write_text("DATABASE_URL=keep-me\n")
    args = ["bootstrap-local-cognito", "--backend-env", str(backend_env)]

    assert runner.invoke(cli.app, args).exit_code == 0
    first = _read_env(backend_env)
    assert runner.invoke(cli.app, args).exit_code == 0
    second = _read_env(backend_env)

    assert first == second
    assert first["DATABASE_URL"] == "keep-me"
    assert first["COGNITO_ISSUER"].endswith("/" + first["COGNITO_USER_POOL_ID"])
    assert first["COGNITO_CLIENT_ID"]

    idp = boto3.client("cognito-idp", region_name="us-east-1")
    pools = [
        p for p in idp.list_user_pools(MaxResults=50)["UserPools"] if p["Name"] == "ptax-local"
    ]
    assert len(pools) == 1
    clients = idp.list_user_pool_clients(UserPoolId=pools[0]["Id"], MaxResults=50)[
        "UserPoolClients"
    ]
    assert [c["ClientName"] for c in clients] == ["ptax-web"]


def test_seed_local_is_idempotent(cli_env: Settings, db: Session) -> None:
    assert runner.invoke(cli.app, ["seed-local"]).exit_code == 0
    assert runner.invoke(cli.app, ["seed-local"]).exit_code == 0

    tenants = db.execute(select(Tenant).where(Tenant.fips.in_(["27053", "27001"]))).scalars().all()
    assert {t.name for t in tenants} == {"Demo County", "Other County"}
    users = db.execute(select(User)).scalars().all()
    assert {u.email for u in users} == {
        "admin@demo.test",
        "reviewer@demo.test",
        "admin@other.test",
    }
    assert CognitoAdmin(cli_env).get_user("reviewer@demo.test")["status"] == "CONFIRMED"


def test_set_password(cli_env: Settings) -> None:
    runner.invoke(cli.app, ["create-tenant", "--name", "A", "--state", "MN", "--fips", "27077"])
    runner.invoke(
        cli.app,
        ["create-user", "--tenant-fips", "27077", "--email", "r@demo.test", "--role", "reviewer"],
    )
    # Same shape as TS-004 step 4: flag between the two positionals.
    result = runner.invoke(cli.app, ["set-password", "r@demo.test", "--permanent", "Password1!"])
    assert result.exit_code == 0, result.output
    assert CognitoAdmin(cli_env).get_user("r@demo.test")["status"] == "CONFIRMED"
