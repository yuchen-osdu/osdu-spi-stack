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

"""Unit tests for the pure logic in `spi onboard`.

These cover the security-relevant string construction (namespace-scoped role
assignment scopes, identity/role names) without touching az/kubectl/gh.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

import spi.onboard as onboard_module
from spi.onboard import (
    DEPLOY_DATA_ACTIONS,
    EXPECTED_VALIDATION_JOBS,
    FEDERATED_CREDENTIAL_LIMIT,
    NO_DATA_ACCESS_IDENTITY_NAME,
    OSDU_BRANCHES,
    SETTINGS_APPLY_WORKFLOW,
    VALIDATION_WORKFLOW,
    OnboardInputs,
    ServiceDescriptor,
    WorkflowJob,
    WorkflowRun,
    _derive_stack_coordinates,
    _desired_repository_variables,
    _determine_material_environment_change,
    _discover_environment_facts,
    _dispatch_workflow,
    _ensure_custom_deploy_role,
    _ensure_flux_read_rbac,
    _ensure_rbac,
    _extract_entitlement_domain,
    _fetch_service_descriptor,
    _gh_delete_variable,
    _github_repo_slug,
    _load_canonical_descriptor_validator,
    _no_data_access_federated_credentials,
    _parse_service_descriptor,
    _remove_no_data_access_federated_credentials,
    _remove_unused_keyvault_role,
    _require_validation_jobs,
    _resolve_descriptor_and_coordinates,
    _resolve_no_data_access_profile,
    _select_dispatched_run,
    _service_federated_credentials,
    _should_write_secrets,
    _verify_onboarding,
    _wait_for_workflow_run,
    _write_handoff,
)

CLUSTER_ID = (
    "/subscriptions/sub-1/resourceGroups/spi-stack-dev3/providers/"
    "Microsoft.ContainerService/managedClusters/spi-stack-dev3"
)


def _inputs() -> OnboardInputs:
    inp = OnboardInputs(
        service="partition",
        repo="my-org/partition",
        aks_cluster="spi-stack-dev3",
        aks_rg="spi-stack-dev3",
        identities_rg="spi-stack-dev3",
        namespace="osdu",
        flux_namespace="osdu-flux",
    )
    inp.cluster_resource_id = CLUSTER_ID
    inp.github_oidc_subject_prefix = "repo:my-org@123/partition@456"
    return inp


def test_identity_and_role_names_are_service_scoped():
    inp = _inputs()
    assert inp.identity_name == "spi-ci-partition"
    assert inp.deploy_role_name == "spi-ci-partition-deploy"
    assert inp.no_data_access_identity_name == NO_DATA_ACCESS_IDENTITY_NAME


def test_no_data_access_profile_is_opt_in_by_service():
    storage = _inputs()
    storage.descriptor_no_data_access_token_env = "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN"
    _resolve_no_data_access_profile(storage)
    assert storage.uses_no_data_access_identity is True
    assert storage.resolved_no_data_access_token_env == "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN"

    schema = _inputs()
    schema.descriptor_no_data_access_token_env = ""
    _resolve_no_data_access_profile(schema)
    assert schema.uses_no_data_access_identity is False
    assert schema.resolved_no_data_access_token_env == ""


def test_no_data_access_profile_allows_explicit_override():
    inp = _inputs()
    inp.no_data_access_token_env = ""
    assert inp.uses_no_data_access_identity is False

    inp.service = "schema"
    inp.no_data_access_token_env = "CUSTOM_NO_ACCESS_TOKEN"
    assert inp.uses_no_data_access_identity is True
    assert inp.resolved_no_data_access_token_env == "CUSTOM_NO_ACCESS_TOKEN"


def test_no_data_access_profile_uses_descriptor():
    inp = _inputs()
    inp.descriptor_no_data_access_token_env = "DESCRIPTOR_NO_ACCESS_TOKEN"

    _resolve_no_data_access_profile(inp)

    assert inp.uses_no_data_access_identity is True
    assert inp.resolved_no_data_access_token_env == "DESCRIPTOR_NO_ACCESS_TOKEN"


def test_namespace_scope_targets_the_service_namespace():
    inp = _inputs()
    assert inp.namespace_scope == f"{CLUSTER_ID}/namespaces/osdu"
    assert inp.flux_namespace_scope == f"{CLUSTER_ID}/namespaces/osdu-flux"


def test_namespace_scope_is_not_cluster_wide():
    # Security: the deploy role must never be assignable at the bare cluster scope.
    inp = _inputs()
    assert inp.namespace_scope != CLUSTER_ID
    assert inp.namespace_scope.endswith("/namespaces/osdu")


def test_deploy_data_actions_are_least_privilege():
    # No wildcard / delete / secrets-read actions in the deploy role.
    blob = " ".join(DEPLOY_DATA_ACTIONS).lower()
    assert "*" not in blob
    assert "secrets" not in blob
    assert "/delete" not in blob
    # Deployments get write (set image); pods/events are read-only.
    assert "apps/deployments/write" in blob
    assert "pods/read" in blob
    assert "events/read" in blob
    # pods/log/read and the Flux CRD read are not registered AKS dataActions, so they are
    # NOT in the Azure role (they would make `az role definition create` fail); Flux read is
    # granted via native k8s RBAC instead.
    assert "pods/log" not in blob
    assert "kustomize" not in blob


def test_custom_role_rehome_preserves_existing_cluster_scope(monkeypatch):
    inp = _inputs()
    old_cluster = (
        "/subscriptions/sub-1/resourceGroups/spi-stack-old/providers/"
        "Microsoft.ContainerService/managedClusters/spi-stack-old"
    )
    existing_role = {
        "id": ("/subscriptions/sub-1/providers/Microsoft.Authorization/roleDefinitions/role-guid"),
        "name": "role-guid",
        "roleName": "spi-ci-partition-deploy",
        "roleType": "CustomRole",
        "assignableScopes": [old_cluster],
    }
    captured = {}

    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: [existing_role],
    )

    def capture_role(command, **_kwargs):
        definition = command[command.index("--role-definition") + 1]
        captured["command"] = command
        captured["role"] = json.loads(Path(definition.removeprefix("@")).read_text())

    monkeypatch.setattr(onboard_module, "_run", capture_role)

    _ensure_custom_deploy_role(inp)

    assert "update" in captured["command"]
    assert captured["role"]["id"].endswith("/roleDefinitions/role-guid")
    assert captured["role"]["roleName"] == "spi-ci-partition-deploy"
    assert captured["role"]["assignableScopes"] == [old_cluster, CLUSTER_ID]
    assert captured["role"]["permissions"][0]["dataActions"] == DEPLOY_DATA_ACTIONS


def test_flux_reader_covers_kustomizations_and_helmreleases(monkeypatch):
    inp = _inputs()
    inp.identity_principal_id = "service-principal-object-id"
    manifests = []

    def capture_manifest(command, **_kwargs):
        manifests.append(Path(command[-1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(onboard_module, "_run", capture_manifest)

    _ensure_flux_read_rbac(inp)

    assert len(manifests) == 1
    assert 'apiGroups: ["kustomize.toolkit.fluxcd.io"]' in manifests[0]
    assert 'resources: ["kustomizations"]' in manifests[0]
    assert 'apiGroups: ["helm.toolkit.fluxcd.io"]' in manifests[0]
    assert 'resources: ["helmreleases"]' in manifests[0]


def test_osdu_branch_subjects_cover_the_three_branches():
    assert set(OSDU_BRANCHES) == {"main", "fork_integration", "fork_upstream"}


def test_service_federated_subjects_use_resolved_github_prefix():
    subjects = _service_federated_credentials(_inputs())
    assert subjects["spi-ci-partition-pull-request"] == (
        "repo:my-org@123/partition@456:pull_request"
    )
    assert subjects["spi-ci-partition-branch-main"] == (
        "repo:my-org@123/partition@456:ref:refs/heads/main"
    )


def test_shared_no_data_identity_uses_existing_repo_subjects():
    subjects = _no_data_access_federated_credentials(_inputs())
    assert subjects["spi-no-data-partition-pull-request"] == (
        "repo:my-org@123/partition@456:pull_request"
    )
    assert subjects["spi-no-data-partition-branch-main"] == (
        "repo:my-org@123/partition@456:ref:refs/heads/main"
    )
    assert len(subjects) == 4
    assert FEDERATED_CREDENTIAL_LIMIT // len(subjects) == 5


def test_federated_credential_subject_change_updates_in_place(monkeypatch):
    calls = []
    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: [
            {
                "name": "spi-no-data-partition-pull-request",
                "subject": "repo:old-org/partition:pull_request",
            }
        ],
    )
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    onboard_module._reconcile_federated_credentials(
        _inputs(),
        NO_DATA_ACCESS_IDENTITY_NAME,
        {"spi-no-data-partition-pull-request": ("repo:my-org@123/partition@456:pull_request")},
    )

    assert len(calls) == 1
    assert "update" in calls[0]
    assert "create" not in calls[0]
    assert "repo:my-org@123/partition@456:pull_request" in calls[0]


def test_federated_credential_limit_fails_before_create(monkeypatch):
    existing = [
        {"name": f"credential-{index}", "subject": f"subject-{index}"}
        for index in range(FEDERATED_CREDENTIAL_LIMIT)
    ]
    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda *_args, **_kwargs: existing,
    )
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("must not mutate at the credential limit"),
    )

    with pytest.raises(typer.Exit):
        onboard_module._reconcile_federated_credentials(
            _inputs(),
            NO_DATA_ACCESS_IDENTITY_NAME,
            _no_data_access_federated_credentials(_inputs()),
        )


def test_disabling_profile_removes_repo_federated_credentials(monkeypatch):
    calls = []

    def fake_az_json(args, **_kwargs):
        if args[:2] == ["identity", "show"]:
            return {"clientId": "shared-client", "principalId": "shared-principal"}
        return [
            {
                "name": "spi-no-data-partition-pull-request",
                "subject": "repo:my-org@123/partition@456:pull_request",
            }
        ]

    monkeypatch.setattr(onboard_module, "_az_json", fake_az_json)
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda command, **_kwargs: calls.append(command),
    )

    _remove_no_data_access_federated_credentials(_inputs())

    assert len(calls) == 1
    assert "delete" in calls[0]
    assert "spi-no-data-partition-pull-request" in calls[0]


def test_repo_variable_delete_attempts_empty_values_and_ignores_404(monkeypatch):
    calls = []
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="gh: variable not found (HTTP 404)",
            ),
        ]
    )
    monkeypatch.setattr(
        onboard_module,
        "run_process",
        lambda *args, **_kwargs: calls.append(args[0]) or next(responses),
    )

    _gh_delete_variable(_inputs(), "EMPTY_BUT_PRESENT")
    _gh_delete_variable(_inputs(), "MISSING")

    assert len(calls) == 2
    assert all("delete" in command for command in calls)


def test_secret_write_policy_rehome_and_idempotency():
    # First onboard: no secret yet -> write.
    assert _should_write_secrets(secret_present=False, is_rehome=False, force=False) is True
    # Idempotent re-run against the same cluster (same identity already set) -> skip.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=False) is False
    # Re-home onto a new cluster (identity changed) -> rewrite so the secret follows the variable.
    assert _should_write_secrets(secret_present=True, is_rehome=True, force=False) is True
    # Explicit force -> rewrite.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=True) is True


def test_env_derivation_and_explicit_overrides():
    assert _derive_stack_coordinates("auto2", "", "", "") == (
        "spi-stack-auto2",
        "spi-stack-auto2",
        "spi-stack-auto2",
    )
    assert _derive_stack_coordinates(
        "auto2",
        "cluster-override",
        "",
        "identity-override",
    ) == ("cluster-override", "spi-stack-auto2", "identity-override")

    with pytest.raises(ValueError, match="--aks-cluster"):
        _derive_stack_coordinates("", "", "cluster-rg", "identity-rg")


class _CanonicalValidator:
    @staticmethod
    def parse(raw):
        return json.loads(raw)

    @staticmethod
    def validate(_document, _schema):
        return []


def test_raw_descriptor_read_uses_exact_sha_and_github_raw_media_type(monkeypatch):
    commands = []
    raw = json.dumps(
        {
            "schemaVersion": 2,
            "service": {"name": "partition"},
            "tests": {
                "acceptance": {
                    "noDataAccessTokenEnv": "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN",
                    "keyVaultBindings": {"CLIENT_SECRET": "acceptance-secret"},
                }
            },
        }
    )

    def fake_run_process(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(onboard_module, "run_process", fake_run_process)

    descriptor = _fetch_service_descriptor(
        "yuchen-osdu/partition",
        "abc123",
        validator=_CanonicalValidator,
        schema={"supportedSchemaVersions": [2]},
    )

    assert descriptor == ServiceDescriptor(
        schema_version=2,
        service_name="partition",
        no_data_access_token_env="NO_DATA_ACCESS_TESTER_ACCESS_TOKEN",
        has_keyvault_bindings=True,
    )
    assert commands == [
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw+json",
            "repos/yuchen-osdu/partition/contents/.spi/service.yaml?ref=abc123",
        ]
    ]


def test_descriptor_v2_extracts_acceptance_no_data_and_empty_keyvault():
    descriptor = _parse_service_descriptor(
        json.dumps(
            {
                "schemaVersion": 2,
                "service": {"name": "storage"},
                "tests": {
                    "acceptance": {
                        "type": "maven",
                        "noDataAccessTokenEnv": "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN",
                        "keyVaultBindings": {},
                    }
                },
            }
        ),
        validator=_CanonicalValidator,
        schema={"supportedSchemaVersions": [2]},
    )

    assert descriptor.schema_version == 2
    assert descriptor.service_name == "storage"
    assert descriptor.no_data_access_token_env == "NO_DATA_ACCESS_TESTER_ACCESS_TOKEN"
    assert descriptor.has_keyvault_bindings is False


def test_descriptor_aware_onboarding_requires_v2():
    with pytest.raises(ValueError, match="schemaVersion: 2"):
        _parse_service_descriptor(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "service": {"name": "partition"},
                }
            ),
            validator=_CanonicalValidator,
            schema={"supportedSchemaVersions": [1, 2]},
        )


def test_canonical_validation_errors_are_reported_without_partial_fallback():
    class Error:
        def render(self):
            return "service.name: invalid-value"

    class RejectingValidator(_CanonicalValidator):
        @staticmethod
        def validate(_document, _schema):
            return [Error()]

    with pytest.raises(ValueError, match="service.name: invalid-value"):
        _parse_service_descriptor(
            json.dumps({"schemaVersion": 2, "service": {"name": "INVALID"}}),
            validator=RejectingValidator,
            schema={"supportedSchemaVersions": [2]},
        )


def test_canonical_validator_and_schema_load_from_same_template_sha(monkeypatch):
    source = """
import json
def parse(raw):
    return json.loads(raw)
def validate(document, schema):
    return [] if document["schemaVersion"] in schema["supportedSchemaVersions"] else ["bad"]
"""
    fetched = []

    def fake_fetch(repo, path, ref):
        fetched.append((repo, path, ref))
        if path.endswith("descriptor.py"):
            return source
        return '{"supportedSchemaVersions":[2]}'

    monkeypatch.setattr(onboard_module, "_fetch_github_raw", fake_fetch)

    validator, schema = _load_canonical_descriptor_validator("org/template", "template-sha")

    assert validator.parse('{"schemaVersion":2}') == {"schemaVersion": 2}
    assert validator.validate({"schemaVersion": 2}, schema) == []
    assert fetched == [
        (
            "org/template",
            ".github/scripts/service-config/descriptor.py",
            "template-sha",
        ),
        (
            "org/template",
            ".github/scripts/service-config/schema.json",
            "template-sha",
        ),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/yuchen-osdu/osdu-spi.git", "yuchen-osdu/osdu-spi"),
        ("git@github.com:yuchen-osdu/osdu-spi.git", "yuchen-osdu/osdu-spi"),
        ("yuchen-osdu/osdu-spi", "yuchen-osdu/osdu-spi"),
    ],
)
def test_template_repo_url_normalization(value, expected):
    assert _github_repo_slug(value) == expected


def test_descriptor_resolution_pins_repo_and_template_main_shas(monkeypatch):
    inp = OnboardInputs(repo="my-org/partition", env="auto2")
    calls = []
    monkeypatch.setenv("TEMPLATE_REPO_URL", "https://github.com/template-org/spi.git")
    monkeypatch.setattr(
        onboard_module,
        "_repo_head_sha",
        lambda repo: {
            "my-org/partition": "repo-sha",
            "template-org/spi": "template-sha",
        }[repo],
    )
    monkeypatch.setattr(
        onboard_module,
        "_load_canonical_descriptor_validator",
        lambda repo, sha: (
            calls.append(("validator", repo, sha))
            or (_CanonicalValidator, {"supportedSchemaVersions": [2]})
        ),
    )
    monkeypatch.setattr(
        onboard_module,
        "_fetch_service_descriptor",
        lambda repo, ref, **_kwargs: (
            calls.append(("descriptor", repo, ref)) or ServiceDescriptor(2, "partition", "", False)
        ),
    )

    _resolve_descriptor_and_coordinates(inp)

    assert inp.repo_main_sha == "repo-sha"
    assert inp.template_main_sha == "template-sha"
    assert inp.service == "partition"
    assert calls == [
        ("validator", "template-org/spi", "template-sha"),
        ("descriptor", "my-org/partition", "repo-sha"),
    ]


def test_canonical_descriptor_failure_stops_before_preconditions_or_mutations(monkeypatch):
    inp = OnboardInputs(repo="my-org/partition", env="auto2")

    def fail_validation(_inp):
        raise typer.Exit(code=1)

    monkeypatch.setattr(
        onboard_module,
        "_resolve_descriptor_and_coordinates",
        fail_validation,
    )
    monkeypatch.setattr(
        onboard_module,
        "_check_preconditions",
        lambda _inp: pytest.fail("canonical validation must run first"),
    )

    with pytest.raises(typer.Exit):
        onboard_module.onboard(inp)


def test_configmap_discovery_populates_environment_facts(monkeypatch):
    inp = _inputs()
    inp.partition = "tenant1"
    inp.descriptor_has_keyvault_bindings = True

    def fake_configmap(name, namespace):
        if (namespace, name) == ("osdu-flux", "spi-ingress-config"):
            return {"INGRESS_FQDN": "stack.example.test"}
        assert (namespace, name) == ("osdu", "osdu-config")
        return {
            "KEYVAULT_NAME": "stack-kv",
            "PRIMARY_STORAGE_ACCOUNT_NAME": "stackstorage",
            "DOMAIN": "contoso.osdu.example",
        }

    monkeypatch.setattr(onboard_module, "_read_configmap_data", fake_configmap)

    _discover_environment_facts(inp)

    assert inp.gateway_url == "https://stack.example.test"
    assert inp.keyvault == "stack-kv"
    assert inp.storage_account_name == "stackstorage"
    assert inp.data_partition_id == "tenant1"
    assert inp.expected_entitlement_domain == "contoso.osdu.example"


def test_configmap_discovery_preserves_explicit_overrides(monkeypatch):
    inp = _inputs()
    inp.descriptor_has_keyvault_bindings = True
    inp.gateway_url = "https://override.example.test/"
    inp.keyvault = "override-kv"
    monkeypatch.setattr(
        onboard_module,
        "_read_configmap_data",
        lambda name, _namespace: (
            {"INGRESS_FQDN": "ignored.example.test"}
            if name == "spi-ingress-config"
            else {
                "KEYVAULT_NAME": "ignored-kv",
                "STORAGE_ACCOUNT_NAME": "stackstorage",
            }
        ),
    )

    _discover_environment_facts(inp)

    assert inp.gateway_url == "https://override.example.test"
    assert inp.keyvault == "override-kv"
    assert inp.storage_account_name == "stackstorage"


def test_no_secret_contract_skips_keyvault_discovery(monkeypatch):
    inp = _inputs()
    inp.keyvault = "ignored-explicit-vault"
    monkeypatch.setattr(
        onboard_module,
        "_read_configmap_data",
        lambda name, _namespace: (
            {"INGRESS_FQDN": "stack.example.test"}
            if name == "spi-ingress-config"
            else {"PRIMARY_STORAGE_ACCOUNT_NAME": "stackstorage"}
        ),
    )

    _discover_environment_facts(inp)

    assert inp.keyvault is None


def test_entitlement_domain_is_captured_from_seed_output():
    logs = """
Seeding entitlements member 'client' into partition 'opendes'
  domain = contoso.osdu.example (groups visible: 42)
RESULT rc=0
"""
    assert _extract_entitlement_domain(logs) == "contoso.osdu.example"


def _ready_inputs(*, verify=False, dry_run=False):
    inp = _inputs()
    inp.verify = verify
    inp.dry_run = dry_run
    inp.repo_main_sha = "abc123"
    inp.template_repo = "yuchen-osdu/osdu-spi"
    inp.template_main_sha = "template123"
    inp.descriptor_has_keyvault_bindings = True
    inp.subscription_id = "subscription-id"
    inp.tenant_id = "tenant-id"
    inp.identity_client_id = "service-client-id"
    inp.identity_principal_id = "service-principal-id"
    inp.deployment_name = "osdu-partition"
    inp.container_name = "osdu-partition"
    inp.gateway_url = "https://stack.example.test"
    inp.keyvault = "stack-kv"
    inp.storage_account_name = "stackstorage"
    inp.data_partition_id = "opendes"
    inp.entitlement_domain = "contoso.osdu.example"
    return inp


def test_handoff_writes_environment_owned_variables(monkeypatch):
    inp = _ready_inputs()
    secrets = {}
    variables = {}
    deleted = []
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_secret",
        lambda _inp, name, value: secrets.__setitem__(name, value),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: variables.__setitem__(name, value),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_delete_variable",
        lambda _inp, name: deleted.append(name),
    )

    _write_handoff(inp)

    assert secrets == {
        "AZURE_CLIENT_ID": "service-client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_SUBSCRIPTION_ID": "subscription-id",
    }
    assert variables["AZURE_CLIENT_ID"] == "service-client-id"
    assert variables["DATA_PARTITION_ID"] == "opendes"
    assert variables["ENTITLEMENT_DOMAIN"] == "contoso.osdu.example"
    assert variables["STORAGE_ACCOUNT_NAME"] == "stackstorage"
    assert variables["KEYVAULT_NAME"] == "stack-kv"
    assert variables["GATEWAY_URL"] == "https://stack.example.test"
    assert variables["DEPLOY_VALIDATED"] == "false"
    assert deleted == []


def test_noop_no_verify_preserves_validated_true_without_repo_mutations(monkeypatch):
    inp = _ready_inputs()
    inp.existing_variables = {
        **_desired_repository_variables(inp),
        "DEPLOY_VALIDATED": "true",
    }
    inp.existing_secret_names = {
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    }
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_secret",
        lambda *_args: pytest.fail("no-op must not write secrets"),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda *_args: pytest.fail("no-op must not write variables"),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_delete_variable",
        lambda *_args: pytest.fail("no-op must not delete variables"),
    )

    _write_handoff(inp)

    assert _determine_material_environment_change(inp) is False
    assert inp.existing_variables["DEPLOY_VALIDATED"] == "true"


def test_material_change_resets_validated_before_first_repo_mutation(monkeypatch):
    inp = _ready_inputs()
    inp.existing_variables = {
        **_desired_repository_variables(inp),
        "GATEWAY_URL": "https://old.example.test",
        "DEPLOY_VALIDATED": "true",
    }
    inp.existing_secret_names = {
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    }
    events = []
    monkeypatch.setattr(onboard_module, "_gh_set_secret", lambda *_args: None)
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: events.append(("set", name, value)),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_delete_variable",
        lambda _inp, name: events.append(("delete", name)),
    )

    _write_handoff(inp)

    assert events[0] == ("set", "DEPLOY_VALIDATED", "false")
    assert ("set", "GATEWAY_URL", "https://stack.example.test") in events


def test_no_secret_contract_removes_variable_and_exact_keyvault_role(monkeypatch):
    inp = _ready_inputs()
    inp.descriptor_has_keyvault_bindings = False
    inp.keyvault = None
    inp.existing_variables = {
        **_desired_repository_variables(inp),
        "KEYVAULT_NAME": "old-vault",
        "DEPLOY_VALIDATED": "true",
    }
    inp.existing_secret_names = {
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    }
    repo_events = []
    role_commands = []

    def fake_az_json(args, **_kwargs):
        if args[:2] == ["keyvault", "show"]:
            return {
                "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/old"
            }
        if args[:3] == ["role", "assignment", "list"]:
            return [
                {"id": "/subscriptions/sub/providers/Microsoft.Authorization/roleAssignments/1"}
            ]
        pytest.fail(f"unexpected Azure lookup: {args}")

    monkeypatch.setattr(onboard_module, "_az_json", fake_az_json)
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda command, **_kwargs: role_commands.append(command),
    )
    monkeypatch.setattr(onboard_module, "_gh_set_secret", lambda *_args: None)
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: repo_events.append(("set", name, value)),
    )
    monkeypatch.setattr(
        onboard_module,
        "_gh_delete_variable",
        lambda _inp, name: repo_events.append(("delete", name)),
    )

    _remove_unused_keyvault_role(inp)
    _write_handoff(inp)

    assert role_commands == [
        [
            "az",
            "role",
            "assignment",
            "delete",
            "--ids",
            "/subscriptions/sub/providers/Microsoft.Authorization/roleAssignments/1",
        ]
    ]
    assert ("delete", "KEYVAULT_NAME") in repo_events
    assert not any(event[:2] == ("set", "KEYVAULT_NAME") for event in repo_events)


def test_no_secret_contract_never_grants_keyvault_role(monkeypatch):
    inp = _ready_inputs()
    inp.descriptor_has_keyvault_bindings = False
    inp.keyvault = None
    assignments = []
    monkeypatch.setattr(
        onboard_module,
        "_assign_role",
        lambda _inp, role, *_args: assignments.append(role),
    )
    monkeypatch.setattr(onboard_module, "_ensure_custom_deploy_role", lambda _inp: None)
    monkeypatch.setattr(onboard_module, "_ensure_flux_read_rbac", lambda _inp: None)
    monkeypatch.setattr(
        onboard_module,
        "_az_json",
        lambda args, **_kwargs: pytest.fail(f"unexpected Key Vault lookup: {args}"),
    )

    _ensure_rbac(inp)

    assert "Key Vault Secrets User" not in assignments


def _workflow_run(workflow, *, status="queued", conclusion="", jobs=None):
    run_id = 101 if workflow == VALIDATION_WORKFLOW else 202
    if jobs is None and workflow == VALIDATION_WORKFLOW and status == "completed":
        jobs = tuple(WorkflowJob(name, "completed", "success") for name in EXPECTED_VALIDATION_JOBS)
    return WorkflowRun(
        database_id=run_id,
        url=f"https://github.test/runs/{run_id}",
        status=status,
        conclusion=conclusion,
        created_at="2026-08-27T18:00:00Z",
        head_sha="abc123",
        jobs=jobs or (),
    )


def test_verify_lifecycle_and_settings_apply_ordering(monkeypatch):
    inp = _ready_inputs(verify=True)
    events = []
    monkeypatch.setattr(onboard_module, "_assert_repo_main_unchanged", lambda *_args: None)
    inp.existing_secret_names = {
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
    }
    monkeypatch.setattr(onboard_module, "_gh_set_secret", lambda *_args: None)
    monkeypatch.setattr(onboard_module, "_gh_delete_variable", lambda *_args: None)
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: (
            events.append(f"set:{value}") if name == "DEPLOY_VALIDATED" else None
        ),
    )
    monkeypatch.setattr(
        onboard_module,
        "_freeze_flux_for_verification",
        lambda _inp: events.append("freeze"),
    )

    def dispatch(_repo, workflow, **_kwargs):
        events.append(f"dispatch:{workflow}")
        return _workflow_run(workflow)

    def wait(_repo, run):
        workflow = VALIDATION_WORKFLOW if run.database_id == 101 else SETTINGS_APPLY_WORKFLOW
        events.append(f"wait:{workflow}")
        return _workflow_run(workflow, status="completed", conclusion="success")

    monkeypatch.setattr(onboard_module, "_dispatch_workflow", dispatch)
    monkeypatch.setattr(onboard_module, "_wait_for_workflow_run", wait)

    _write_handoff(inp)
    _verify_onboarding(inp)

    assert events == [
        "set:false",
        "freeze",
        f"dispatch:{VALIDATION_WORKFLOW}",
        f"wait:{VALIDATION_WORKFLOW}",
        f"dispatch:{SETTINGS_APPLY_WORKFLOW}",
        f"wait:{SETTINGS_APPLY_WORKFLOW}",
        "set:true",
        f"dispatch:{SETTINGS_APPLY_WORKFLOW}",
        f"wait:{SETTINGS_APPLY_WORKFLOW}",
    ]
    assert inp.validation_result == "success"
    assert inp.settings_apply_results == ["success", "success"]


def test_verify_validation_failure_leaves_validated_false(monkeypatch):
    inp = _ready_inputs(verify=True)
    flags = []
    dispatched = []
    monkeypatch.setattr(onboard_module, "_assert_repo_main_unchanged", lambda *_args: None)
    monkeypatch.setattr(onboard_module, "_freeze_flux_for_verification", lambda _inp: None)
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: flags.append(value) if name == "DEPLOY_VALIDATED" else None,
    )
    monkeypatch.setattr(
        onboard_module,
        "_dispatch_workflow",
        lambda _repo, workflow, **_kwargs: dispatched.append(workflow) or _workflow_run(workflow),
    )
    monkeypatch.setattr(
        onboard_module,
        "_wait_for_workflow_run",
        lambda _repo, _run: _workflow_run(
            VALIDATION_WORKFLOW,
            status="completed",
            conclusion="failure",
        ),
    )

    with pytest.raises(typer.Exit):
        _verify_onboarding(inp)

    assert flags == ["false"]
    assert dispatched == [VALIDATION_WORKFLOW]


def test_validation_requires_named_deploy_lane_jobs():
    incomplete = _workflow_run(
        VALIDATION_WORKFLOW,
        status="completed",
        conclusion="success",
        jobs=(
            WorkflowJob("🚀 Deploy to spi-stack", "completed", "success"),
            WorkflowJob("🧪 Integration Tests", "completed", "failure"),
        ),
    )

    with pytest.raises(RuntimeError, match="Deploy, Test & Restore.*missing"):
        _require_validation_jobs(incomplete)


def test_verify_settings_failure_rolls_validated_back(monkeypatch):
    inp = _ready_inputs(verify=True)
    flags = []
    monkeypatch.setattr(onboard_module, "_assert_repo_main_unchanged", lambda *_args: None)
    monkeypatch.setattr(onboard_module, "_freeze_flux_for_verification", lambda _inp: None)
    monkeypatch.setattr(
        onboard_module,
        "_gh_set_variable",
        lambda _inp, name, value: flags.append(value) if name == "DEPLOY_VALIDATED" else None,
    )
    monkeypatch.setattr(
        onboard_module,
        "_dispatch_workflow",
        lambda _repo, workflow, **_kwargs: _workflow_run(workflow),
    )

    calls = 0

    def wait(_repo, run):
        nonlocal calls
        workflow = VALIDATION_WORKFLOW if run.database_id == 101 else SETTINGS_APPLY_WORKFLOW
        calls += 1
        conclusion = "failure" if calls == 3 else "success"
        return _workflow_run(workflow, status="completed", conclusion=conclusion)

    monkeypatch.setattr(onboard_module, "_wait_for_workflow_run", wait)

    with pytest.raises(typer.Exit):
        _verify_onboarding(inp)

    assert flags == ["true", "false"]
    assert inp.validation_result == "success"
    assert inp.settings_apply_results == ["success", "failure"]


def test_dispatched_run_selection_excludes_old_and_wrong_sha_runs():
    dispatched_after = datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc)
    runs = [
        WorkflowRun(1, "old", "completed", "success", "2026-08-27T18:01:00Z", "abc"),
        WorkflowRun(2, "wrong", "queued", "", "2026-08-27T18:01:00Z", "wrong"),
        WorkflowRun(3, "early", "queued", "", "2026-08-27T17:59:59Z", "abc"),
        WorkflowRun(4, "selected", "queued", "", "2026-08-27T18:00:01Z", "abc"),
    ]

    selected = _select_dispatched_run(
        runs,
        previous_run_ids={1},
        head_sha="abc",
        dispatched_after=dispatched_after,
    )

    assert selected is not None
    assert selected.database_id == 4


def test_dispatched_run_selection_rejects_ambiguous_candidates():
    runs = [
        WorkflowRun(4, "one", "queued", "", "2026-08-27T18:00:01Z", "abc"),
        WorkflowRun(5, "two", "queued", "", "2026-08-27T18:00:02Z", "abc"),
    ]

    with pytest.raises(RuntimeError, match="Ambiguous workflow dispatch"):
        _select_dispatched_run(
            runs,
            previous_run_ids=set(),
            head_sha="abc",
            dispatched_after=datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc),
        )


def test_workflow_dispatch_uses_main_inputs_and_returns_new_run(monkeypatch):
    created = WorkflowRun(
        database_id=101,
        url="https://github.test/runs/101",
        status="queued",
        conclusion="",
        created_at="2099-08-27T18:00:00Z",
        head_sha="abc123",
    )
    listings = iter([[], [created]])
    commands = []
    monkeypatch.setattr(
        onboard_module,
        "_list_workflow_runs",
        lambda *_args: next(listings),
    )
    monkeypatch.setattr(onboard_module, "_repo_head_sha", lambda _repo: "abc123")
    monkeypatch.setattr(
        onboard_module,
        "run_process",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    selected = _dispatch_workflow(
        "my-org/partition",
        VALIDATION_WORKFLOW,
        expected_sha="abc123",
        inputs={"force_full_pipeline": "true"},
        discovery_timeout=5,
        poll_interval=0,
    )

    assert selected == created
    assert commands == [
        [
            "gh",
            "workflow",
            "run",
            VALIDATION_WORKFLOW,
            "-R",
            "my-org/partition",
            "--ref",
            "main",
            "-f",
            "force_full_pipeline=true",
        ]
    ]


def test_workflow_dispatch_fails_if_main_moved(monkeypatch):
    monkeypatch.setattr(onboard_module, "_list_workflow_runs", lambda *_args: [])
    monkeypatch.setattr(onboard_module, "_repo_head_sha", lambda _repo: "new-sha")
    monkeypatch.setattr(
        onboard_module,
        "run_process",
        lambda *_args, **_kwargs: pytest.fail("dispatch must not run after main moves"),
    )

    with pytest.raises(RuntimeError, match="moved from expected-sha to new-sha"):
        _dispatch_workflow(
            "my-org/partition",
            VALIDATION_WORKFLOW,
            expected_sha="expected-sha",
        )


def test_workflow_polling_waits_until_completion(monkeypatch):
    queued = _workflow_run(VALIDATION_WORKFLOW)
    completed = _workflow_run(
        VALIDATION_WORKFLOW,
        status="completed",
        conclusion="success",
    )
    monkeypatch.setattr(onboard_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(onboard_module, "_read_workflow_run", lambda *_args: completed)

    assert (
        _wait_for_workflow_run(
            "my-org/partition",
            queued,
            completion_timeout=5,
            poll_interval=0,
        )
        == completed
    )


def test_workflow_timeout_cancels_and_waits_for_terminal_state(monkeypatch):
    queued = _workflow_run(VALIDATION_WORKFLOW)
    cancelled = _workflow_run(
        VALIDATION_WORKFLOW,
        status="completed",
        conclusion="cancelled",
    )
    commands = []
    monotonic = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(onboard_module.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(onboard_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(onboard_module, "_read_workflow_run", lambda *_args: cancelled)
    monkeypatch.setattr(
        onboard_module,
        "run_process",
        lambda command, **_kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    with pytest.raises(RuntimeError, match="cancellation was requested"):
        _wait_for_workflow_run(
            "my-org/partition",
            queued,
            completion_timeout=0,
            cancel_timeout=5,
            poll_interval=0,
        )

    assert commands == [["gh", "run", "cancel", "101", "-R", "my-org/partition"]]


def test_dry_run_performs_reads_but_no_mutations(monkeypatch):
    inp = OnboardInputs(
        repo="my-org/partition",
        env="auto2",
        verify=True,
        dry_run=True,
    )
    process_commands = []

    def result(returncode=0, stdout="", stderr=""):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    def resolve_inputs(resolved):
        resolved.aks_cluster = "spi-stack-auto2"
        resolved.aks_rg = "spi-stack-auto2"
        resolved.identities_rg = "spi-stack-auto2"
        resolved.service = "partition"
        resolved.descriptor_service_name = "partition"
        resolved.descriptor_schema_version = 2
        resolved.repo_main_sha = "abc123"
        resolved.template_repo = "yuchen-osdu/osdu-spi"
        resolved.template_main_sha = "template123"

    def fake_run_process(command, **_kwargs):
        process_commands.append(command)
        joined = " ".join(command)
        if command[:3] == ["az", "account", "show"]:
            return result(
                stdout=json.dumps({"id": "sub", "tenantId": "tenant", "name": "subscription"})
            )
        if command[:3] == ["az", "aks", "show"]:
            return result(
                stdout=json.dumps(
                    {
                        "id": CLUSTER_ID,
                        "aadProfile": {"enableAzureRbac": True},
                    }
                )
            )
        if command[:3] == ["gh", "auth", "status"]:
            return result()
        if command[:3] == ["gh", "repo", "view"]:
            return result(stdout='{"viewerPermission":"WRITE"}')
        if "actions/oidc/customization/sub" in joined:
            return result(stdout='{"use_default":true,"sub_claim_prefix":"repo:my-org/partition"}')
        if command[:3] == ["gh", "variable", "list"]:
            return result(stdout="[]")
        if command[:3] == ["gh", "secret", "list"]:
            return result(stdout="[]")
        if command[:3] == ["gh", "api", "repos/my-org/partition/commits/main"]:
            return result(stdout="abc123\n")
        if command[:3] == ["az", "identity", "show"]:
            return result(returncode=1, stderr="not found")
        if command[:3] == ["az", "identity", "federated-credential"]:
            return result(stdout="[]")
        if command[:3] == ["az", "role", "assignment"]:
            return result(stdout="[]")
        if command[:3] == ["az", "role", "definition"]:
            return result(stdout="[]")
        pytest.fail(f"unexpected process command: {command}")

    monkeypatch.setattr(onboard_module, "_resolve_descriptor_and_coordinates", resolve_inputs)
    monkeypatch.setattr(onboard_module, "run_process", fake_run_process)
    monkeypatch.setattr(
        onboard_module,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not invoke visible mutation commands"),
    )

    onboard_module.onboard(inp)

    assert not any(command and command[0] == "kubectl" for command in process_commands)
    mutations = (
        ("az", "aks", "get-credentials"),
        ("az", "identity", "create"),
        ("az", "role", "assignment", "create"),
        ("kubectl", "apply"),
        ("kubectl", "patch"),
        ("kubectl", "delete"),
        ("gh", "secret", "set"),
        ("gh", "variable", "set"),
        ("gh", "workflow", "run"),
    )
    assert not any(
        tuple(command[: len(prefix)]) == prefix
        for command in process_commands
        for prefix in mutations
    )
