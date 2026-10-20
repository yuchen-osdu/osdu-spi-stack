# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""`spi up` writes the deploy identity into spi-init-values for the members Job."""

from unittest.mock import patch

from spi import deploy, identity
from spi.config import Config
from spi.templates import parse_init_values


def _person(user_id: str) -> identity.PersonToken:
    return identity.PersonToken("person-bearer", "1", "aud", user_id, "unique_name")


def _applied(infra_outputs: dict, person=None, live_values: str = "") -> str:
    signed_in = (
        {"side_effect": identity.IdentityError("az is signed in as the application x")}
        if person is None
        else {"return_value": person}
    )
    with (
        patch("spi.deploy.display_yaml"),
        patch("spi.deploy.display_result"),
        patch("spi.deploy.kubectl_apply_yaml") as apply_yaml,
        patch("spi.identity.person_token", **signed_in) as token,
        patch("spi.bootstrap.read_init_values", return_value=live_values),
    ):
        deploy._create_spi_init_values(
            Config.from_env("dev1", data_partitions=["opendes", "second"]), infra_outputs
        )
    if person is not None:
        assert token.call_args.kwargs["resource"]
    return apply_yaml.call_args.args[0]


def _operator(applied: str) -> str:
    values = applied.split("values.yaml: |\n", 1)[1]
    return str(parse_init_values(values).get("entitlementsOperator") or "")


def test_deploy_identity_client_id_is_written_as_the_member():
    applied = _applied({"deploy_identity_client_id": "deployer-client-id"})

    assert "    - opendes\n    - second\n" in applied
    assert "entitlementsMembers:\n    - deployer-client-id\n" in applied


def test_no_member_is_written_without_a_deploy_identity():
    applied = _applied({})

    assert "entitlementsMembers" not in applied
    assert "    - opendes\n" in applied


def test_osdu_identity_client_id_is_written_as_the_tenant_service_account():
    applied = _applied({"identity_client_id": "osdu-client-id"})

    assert "tenantServiceAccount: osdu-client-id\n" in applied
    assert "tenantServiceAccount" not in _applied({})


def test_the_person_running_up_is_written_as_the_operator():
    applied = _applied(
        {"deploy_identity_client_id": "deployer-client-id"}, person=_person("alice@contoso.com")
    )

    assert _operator(applied) == "alice@contoso.com"


def test_an_environment_identity_is_never_the_operator():
    """az signed in as the deploy identity (fork CI) is already seeded as a member."""
    applied = _applied(
        {"deploy_identity_client_id": "deployer-client-id"},
        person=_person("Deployer-Client-Id"),
        live_values="    entitlementsOperator: bob@contoso.com\n",
    )

    assert _operator(applied) == "bob@contoso.com"


def test_an_unnamed_runner_keeps_the_recorded_operator():
    """An application sign-in names no person; dropping the operator would
    rename the members Job and run it again for nobody."""
    applied = _applied(
        {"deploy_identity_client_id": "deployer-client-id"},
        live_values='    partitions:\n    - opendes\n    entitlementsOperator: "bob@contoso.com"\n',
    )

    assert _operator(applied) == "bob@contoso.com"
    assert "entitlementsOperator" not in _applied({"deploy_identity_client_id": "d"})
