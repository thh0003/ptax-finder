"""``ptax-admin``: operator provisioning that talks to Postgres and Cognito directly.

There is no operator web identity; tenants and their first admin are created here.
"""

from contextlib import AbstractContextManager
from pathlib import Path
from typing import NoReturn

import typer
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ptax.auth.cognito import CognitoAdmin
from ptax.config import Settings, get_settings
from ptax.db.models import Tenant, User, UserRole
from ptax.db.session import get_engine

app = typer.Typer(help="ptax-finder operator CLI", no_args_is_help=True)


# Factories are module attributes so tests can route the CLI at a test session / pool.
def _settings() -> Settings:
    return get_settings()


def _session() -> AbstractContextManager[Session]:
    return Session(get_engine(_settings().database_url))


def _cognito() -> CognitoAdmin:
    return CognitoAdmin(_settings())


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=False)
    raise typer.Exit(code=1)


def _upsert_env(path: Path, values: dict[str, str]) -> None:
    """Set keys in a dotenv file, preserving unrelated lines."""
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else None
        if key in remaining:
            updated.append(f"{key}={remaining.pop(key)}")
        else:
            updated.append(line)
    updated.extend(f"{k}={v}" for k, v in remaining.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(updated) + "\n")


LOCAL_POOL_NAME = "ptax-local"
LOCAL_CLIENT_NAME = "ptax-web"

# Demo data for the local stack; the E2E scenarios in the plan reference these by name.
SEED_TENANTS = [
    {"name": "Demo County", "state": "MN", "fips": "27053"},
    {"name": "Other County", "state": "MN", "fips": "27001"},
]
SEED_USERS = [
    {"tenant_fips": "27053", "email": "admin@demo.test", "role": UserRole.admin},
    {"tenant_fips": "27053", "email": "reviewer@demo.test", "role": UserRole.reviewer},
    {"tenant_fips": "27001", "email": "admin@other.test", "role": UserRole.admin},
]
SEED_PASSWORD = "Password1!"


@app.command("bootstrap-local-cognito")
def bootstrap_local_cognito(
    backend_env: Path = typer.Option(Path(".env"), help="backend dotenv to update"),
) -> None:
    """Create (or reuse) the cognito-local user pool + client and write their ids to the env file.

    The SPA reads these from ``GET /api/config`` at runtime, so only the backend env is written.
    """
    settings = _settings()
    idp = _cognito().client

    pools = idp.list_user_pools(MaxResults=50)["UserPools"]
    pool = next((p for p in pools if p["Name"] == LOCAL_POOL_NAME), None)
    if pool is None:
        pool = idp.create_user_pool(
            PoolName=LOCAL_POOL_NAME,
            UsernameAttributes=["email"],
            AutoVerifiedAttributes=["email"],
        )["UserPool"]
    pool_id = pool["Id"]

    clients = idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=50)["UserPoolClients"]
    client = next((c for c in clients if c["ClientName"] == LOCAL_CLIENT_NAME), None)
    if client is None:
        client = idp.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=LOCAL_CLIENT_NAME,
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        )["UserPoolClient"]
    client_id = client["ClientId"]

    endpoint = settings.cognito_endpoint_url or "http://localhost:9229"
    _upsert_env(
        backend_env,
        {
            "COGNITO_USER_POOL_ID": pool_id,
            "COGNITO_CLIENT_ID": client_id,
            "COGNITO_ISSUER": f"{endpoint.rstrip('/')}/{pool_id}",
        },
    )
    typer.echo(f"pool {pool_id}, client {client_id} -> {backend_env}")


@app.command("seed-local")
def seed_local() -> None:
    """Idempotently create the demo tenants and users used for local development."""
    cognito = _cognito()
    with _session() as db:
        for spec in SEED_TENANTS:
            if db.execute(select(Tenant).where(Tenant.fips == spec["fips"])).scalar_one_or_none():
                continue
            db.add(Tenant(**spec))
            typer.echo(f"created tenant {spec['name']}")
        db.commit()

        for spec in SEED_USERS:
            if db.execute(select(User).where(User.email == spec["email"])).scalar_one_or_none():
                continue
            tenant = db.execute(
                select(Tenant).where(Tenant.fips == spec["tenant_fips"])
            ).scalar_one()
            try:
                sub = cognito.admin_create_user(
                    spec["email"], temporary_password=SEED_PASSWORD, suppress_message=True
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "UsernameExistsException":
                    raise
                # The emulator kept the user while the database was reset; re-bind it.
                sub = cognito.get_user(spec["email"])["sub"]
            cognito.admin_set_user_password(spec["email"], SEED_PASSWORD, permanent=True)
            role = UserRole(spec["role"])
            db.add(User(tenant_id=tenant.id, cognito_sub=sub, email=spec["email"], role=role))
            typer.echo(f"created {role.value} {spec['email']}")
        db.commit()
    typer.echo(f"seed complete; all demo users use password {SEED_PASSWORD}")


@app.command("create-tenant")
def create_tenant(
    name: str = typer.Option(..., help="County display name"),
    state: str = typer.Option(..., help="Two-letter state code"),
    fips: str = typer.Option(..., help="Five-digit county FIPS code"),
) -> None:
    """Create a county tenant."""
    with _session() as db:
        db.add(Tenant(name=name, state=state.upper(), fips=fips))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            _fail(f"tenant with fips {fips} already exists")
    typer.echo(f"created tenant {name} ({fips})")


@app.command("create-user")
def create_user(
    tenant_fips: str = typer.Option(..., help="FIPS of an existing tenant"),
    email: str = typer.Option(...),
    role: UserRole = typer.Option(..., help="admin or reviewer"),
    password: str | None = typer.Option(
        None, help="Set a password instead of letting Cognito email a temporary one"
    ),
    permanent: bool = typer.Option(
        False, "--permanent", help="With --password: mark it permanent (no first-login change)"
    ),
) -> None:
    """Create a Cognito user and bind it to a tenant with a role."""
    with _session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.fips == tenant_fips)).scalar_one_or_none()
        if tenant is None:
            _fail(f"no tenant with fips {tenant_fips}")
        cognito = _cognito()
        try:
            sub = cognito.admin_create_user(
                email, temporary_password=password, suppress_message=password is not None
            )
            if password and permanent:
                cognito.admin_set_user_password(email, password, permanent=True)
        except ClientError as exc:
            _fail(f"cognito: {exc.response['Error']['Message']}")
        db.add(User(tenant_id=tenant.id, cognito_sub=sub, email=email, role=role))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            cognito.admin_delete_user(email)
            _fail(f"user {email} already exists")
    typer.echo(f"created {role.value} {email} in tenant {tenant_fips} (sub {sub})")


@app.command("set-password")
def set_password(
    email: str = typer.Argument(...),
    password: str = typer.Argument(...),
    permanent: bool = typer.Option(
        False, "--permanent/--temporary", help="Permanent skips the first-login change"
    ),
) -> None:
    """Set a user's Cognito password (operator reset, or local seeding)."""
    try:
        _cognito().admin_set_user_password(email, password, permanent=permanent)
    except ClientError as exc:
        _fail(f"cognito: {exc.response['Error']['Message']}")
    typer.echo(f"password set for {email} ({'permanent' if permanent else 'temporary'})")
