import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import spi.cli as cli_module
from spi.identity import decode_jwt_claims, projected_user_id, projected_user_ids
from spi.templates import spi_init_values_configmap

ROOT = Path(__file__).parents[1]


def _token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_v1_user_projects_unique_name():
    claims = {
        "iss": "https://sts.windows.net/tenant/",
        "unique_name": "creator@example.com",
        "oid": "user-object-id",
        "appid": "azure-cli-app-id",
    }

    assert projected_user_id(claims) == "creator@example.com"
    assert projected_user_ids(claims) == ["creator@example.com", "user-object-id"]


def test_v1_service_principal_projects_appid():
    claims = {
        "iss": "https://sts.windows.net/tenant/",
        "oid": "service-principal-object-id",
        "appid": "service-principal-client-id",
    }

    assert projected_user_id(claims) == "service-principal-client-id"


def test_v2_user_projects_oid():
    claims = {
        "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "oid": "user-object-id",
        "azp": "calling-client-id",
    }

    assert projected_user_id(claims) == "user-object-id"


def test_decode_jwt_claims_rejects_malformed_token():
    with pytest.raises(ValueError, match="malformed"):
        decode_jwt_claims("not-a-jwt")


def test_cli_resolves_creator_from_azure_token(monkeypatch):
    token = _token(
        {
            "iss": "https://sts.windows.net/tenant/",
            "unique_name": "creator@example.com",
        }
    )
    monkeypatch.setattr(
        cli_module,
        "run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=token),
    )

    assert cli_module._resolve_creator_user_ids(True, "") == ["creator@example.com"]


def test_cli_creator_override_and_opt_out():
    assert cli_module._resolve_creator_user_ids(True, " explicit-id ") == ["explicit-id"]
    assert cli_module._resolve_creator_user_ids(False, "") == []
    with pytest.raises(ValueError, match="cannot be used"):
        cli_module._resolve_creator_user_ids(False, "explicit-id")


def test_init_values_include_creator_identity():
    configmap = yaml.safe_load(
        spi_init_values_configmap(["opendes", "tenant1"], ["creator@example.com", "user-object-id"])
    )
    values = yaml.safe_load(configmap["data"]["values.yaml"])

    assert values["partitions"] == ["opendes", "tenant1"]
    assert values["creatorUserIds"] == ["creator@example.com", "user-object-id"]


def test_init_release_force_replaces_immutable_jobs():
    release = yaml.safe_load(
        (ROOT / "software" / "stacks" / "osdu" / "init" / "release.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert release["spec"]["upgrade"]["force"] is True
