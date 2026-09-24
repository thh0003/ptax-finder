import uuid

import pytest

from ptax.storage import TenantKeyError, assert_tenant_key, pipeline_key

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_a_pipeline_key_lives_under_the_tenants_prefix() -> None:
    assert pipeline_key(TENANT, "raw", "2015", "x.tif") == f"tenants/{TENANT}/raw/2015/x.tif"
    assert_tenant_key(TENANT, pipeline_key(TENANT, "raw", "2015", "x.tif"))


@pytest.mark.parametrize(
    "key",
    [
        f"tenants/{OTHER}/raw/2015/x.tif",
        f"tenants/{TENANT}/../{OTHER}/x.tif",
        f"tenants/{TENANT}",
        f"tenants/{TENANT}x/raw/x.tif",
        "raw/2015/x.tif",
    ],
)
def test_keys_outside_the_tenants_prefix_are_refused(key: str) -> None:
    with pytest.raises(TenantKeyError):
        assert_tenant_key(TENANT, key)


def test_a_part_cannot_climb_out_of_the_prefix() -> None:
    with pytest.raises(TenantKeyError):
        pipeline_key(TENANT, "..", str(OTHER), "x.tif")
