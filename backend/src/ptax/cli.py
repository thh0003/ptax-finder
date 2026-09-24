"""``ptax-admin``: operator provisioning that talks to Postgres and Cognito directly.

There is no operator web identity; tenants and their first admin are created here.
"""

import uuid
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
from ptax.db.models import ImageryYear, Tenant, User, UserRole
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


def _tenant_admin(db: Session, tenant_fips: str) -> tuple[Tenant, User]:
    """The tenant with ``tenant_fips`` and its first admin, to attribute operator actions to."""
    tenant = db.execute(select(Tenant).where(Tenant.fips == tenant_fips)).scalar_one_or_none()
    if tenant is None:
        _fail(f"no tenant with fips {tenant_fips}")
    admin = (
        db.execute(
            select(User)
            .where(User.tenant_id == tenant.id, User.role == UserRole.admin)
            .order_by(User.created_at)
        )
        .scalars()
        .first()
    )
    if admin is None:
        _fail(f"tenant {tenant_fips} has no admin to attribute this to")
    return tenant, admin


tenant_config_app = typer.Typer(help="A tenant's parcel improvement pipeline configuration")
app.add_typer(tenant_config_app, name="tenant-config")


def _tenant_by_fips(db: Session, tenant_fips: str) -> Tenant:
    tenant = db.execute(select(Tenant).where(Tenant.fips == tenant_fips)).scalar_one_or_none()
    if tenant is None:
        _fail(f"no tenant with fips {tenant_fips}")
    return tenant


@tenant_config_app.command("set")
def tenant_config_set(
    config_file: Path = typer.Argument(..., help="tenant config JSON, e.g. tenants/*.json"),
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant to configure"),
) -> None:
    """Validate a config file and store it as the tenant's current pipeline configuration."""
    import json

    from pydantic import ValidationError

    from ptax.tenancy.config import TenantConfig
    from ptax.tenancy.store import put_config

    try:
        config = TenantConfig.model_validate(json.loads(config_file.read_text()))
    except ValidationError as exc:
        # One line per bad field; pydantic's message never includes the input value here.
        lines = [f"  {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        _fail(f"{config_file} is not a valid tenant config:\n" + "\n".join(lines))
    with _session() as db:
        tenant = _tenant_by_fips(db, tenant_fips)
        name = tenant.name
        version = put_config(db, tenant.id, config)
        db.commit()
    typer.echo(f"stored config for {name} ({tenant_fips}), version {version}")


@tenant_config_app.command("show")
def tenant_config_show(
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant"),
) -> None:
    """Print the tenant's current pipeline configuration as JSON."""
    from ptax.tenancy.store import get_config

    with _session() as db:
        tenant = _tenant_by_fips(db, tenant_fips)
        config = get_config(db, tenant.id)
    if config is None:
        _fail(f"tenant {tenant_fips} has no pipeline config; set one with `tenant-config set`")
    typer.echo(config.model_dump_json(indent=2))


@tenant_config_app.command("check")
def tenant_config_check(
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant"),
) -> None:
    """Check that the secret, portal token and every configured source are reachable.

    Exits 1 when a required item fails; optional items (footprints, CAMA) are reported only.
    """
    from ptax.storage import get_s3_client
    from ptax.tenancy.check import check_config
    from ptax.tenancy.store import get_config

    with _session() as db:
        tenant = _tenant_by_fips(db, tenant_fips)
        config = get_config(db, tenant.id)
    if config is None:
        _fail(f"tenant {tenant_fips} has no pipeline config; set one with `tenant-config set`")
    results = check_config(config, s3=get_s3_client(_settings()))
    for result in results:
        optional = "" if result.required else " (optional)"
        typer.echo(f"  {result.item:<14} {result.status:<11} {result.reason}{optional}")
    if any(r.required and r.status != "ok" for r in results):
        raise typer.Exit(code=1)


@app.command("county-parcels")
def county_parcels(
    profile_path: Path = typer.Argument(..., help="county profile, e.g. counties/peoria-il.json"),
    area: str = typer.Option(..., help="an area named in the profile, e.g. richwoods"),
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant to load the parcels into"),
) -> None:
    """Load one area's parcels from the county's ArcGIS service as the tenant's parcel layer.

    Only the profile's mapped fields are fetched and stored. The file is uploaded like an
    admin upload and queued for inspection; the worker then ingests it on the profile's id
    field and makes it the tenant's current layer.
    """
    from ptax.county.parcels import CountySourceError, area_parcels_geojson
    from ptax.county.profile import load_profile
    from ptax.db.models import ParcelLayer
    from ptax.jobs.queue import enqueue
    from ptax.storage import get_s3_client

    profile = load_profile(profile_path)
    if area not in profile.areas:
        _fail(f"unknown area {area!r}; {profile_path.name} has {sorted(profile.areas)}")
    with _session() as db:
        tenant, admin = _tenant_admin(db, tenant_fips)
        try:
            data, count = area_parcels_geojson(profile, area)
        except CountySourceError as exc:
            _fail(str(exc))
        if count == 0:
            _fail(f"no parcels found in {area}")
        settings = _settings()
        filename = f"{profile_path.stem}-{area}.geojson"
        key = f"tenants/{tenant.id}/parcel_layer/{uuid.uuid4()}/{filename}"
        get_s3_client(settings).put_object(Bucket=settings.s3_bucket, Key=key, Body=data)
        layer = ParcelLayer(
            tenant_id=tenant.id,
            uploaded_by=admin.id,
            s3_key=key,
            original_filename=filename,
            status="uploaded",
            parcel_id_field=profile.stored_id_field,
        )
        db.add(layer)
        db.flush()
        enqueue(db, "parcel_layer.inspect", tenant.id, {"layer_id": str(layer.id)})
        db.commit()
        typer.echo(f"queued parcel layer {layer.id}: {count} parcels from {profile.name} {area}")


@app.command("county-imagery")
def county_imagery(
    profile_path: Path = typer.Argument(..., help="county profile, e.g. counties/peoria-il.json"),
    year: int = typer.Option(..., help="an imagery year named in the profile, e.g. 2015"),
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant to add the imagery to"),
) -> None:
    """Queue the county's cached orthophoto for ``year`` as an imagery year of the tenant.

    The worker copies the tiles covering the tenant's current parcel layer into stored COGs;
    a failed year is retried by running this again.
    """
    from ptax.county.profile import load_profile
    from ptax.jobs.queue import enqueue

    profile = load_profile(profile_path)
    service = profile.imagery.get(year)
    if service is None:
        _fail(f"{profile_path.name} has no imagery for {year}; it has {sorted(profile.imagery)}")
    with _session() as db:
        tenant, admin = _tenant_admin(db, tenant_fips)
        if tenant.current_parcel_layer_id is None:
            _fail(f"tenant {tenant_fips} has no parcel layer; run county-parcels first")
        existing = db.execute(
            select(ImageryYear).where(
                ImageryYear.tenant_id == tenant.id,
                ImageryYear.year == year,
                ImageryYear.source == "arcgis",
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status != "failed":
            _fail(f"{profile.name} {year} is already {existing.status}")
        imagery_year = existing or ImageryYear(
            tenant_id=tenant.id, year=year, source="arcgis", created_by=admin.id
        )
        imagery_year.provider = service
        imagery_year.status = "queued"
        imagery_year.error = None
        db.add(imagery_year)
        db.flush()
        enqueue(db, "imagery.ingest_arcgis", tenant.id, {"year_id": str(imagery_year.id)})
        db.commit()
        typer.echo(f"queued imagery year {imagery_year.id}: {profile.name} {year} from {service}")


def _ready_year_numbered(db: Session, tenant: Tenant, year: int, source: str | None) -> ImageryYear:
    query = select(ImageryYear).where(
        ImageryYear.tenant_id == tenant.id,
        ImageryYear.year == year,
        ImageryYear.status == "ready",
    )
    if source is not None:
        query = query.where(ImageryYear.source == source)
    found = db.execute(query).scalars().all()
    if not found:
        where = f" from {source}" if source else ""
        _fail(f"no ready imagery year {year}{where} for tenant {tenant.fips}")
    if len(found) > 1:
        _fail(f"several ready imagery years {year}; choose one with --source")
    return found[0]


@app.command("start-run")
def start_run(
    tenant_fips: str = typer.Option(..., help="FIPS of the tenant to run over"),
    base: int = typer.Option(..., help="base imagery year"),
    target: int = typer.Option(..., help="target imagery year"),
    source: str | None = typer.Option(None, help="imagery source, when a year has several"),
) -> None:
    """Queue a classical comparison run without the SPA -- for operators and deployed-stack
    checks.

    Goes through the same `queue_run` as the API, so it accepts and refuses exactly what
    the Runs page would. The run is attributed to the tenant's first admin.
    """
    from ptax.api.runs import RunRefused, queue_run

    with _session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.fips == tenant_fips)).scalar_one_or_none()
        if tenant is None:
            _fail(f"no tenant with fips {tenant_fips}")
        admin = (
            db.execute(
                select(User)
                .where(User.tenant_id == tenant.id, User.role == UserRole.admin)
                .order_by(User.created_at)
            )
            .scalars()
            .first()
        )
        if admin is None:
            _fail(f"tenant {tenant_fips} has no admin to attribute the run to")
        try:
            run = queue_run(
                db,
                tenant=tenant,
                created_by=admin.id,
                base=_ready_year_numbered(db, tenant, base, source),
                target=_ready_year_numbered(db, tenant, target, source),
                detector="classical",
            )
        except RunRefused as exc:
            _fail(str(exc))
        db.commit()
        typer.echo(
            f"queued run {run.id}: {base} -> {target}, {run.detector}, {run.parcels_total} parcels"
        )
