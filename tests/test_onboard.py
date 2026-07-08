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

from spi.onboard import (
    DEPLOY_DATA_ACTIONS,
    OSDU_BRANCHES,
    OnboardInputs,
    _should_write_secrets,
)

CLUSTER_ID = (
    "/subscriptions/sub-1/resourceGroups/spi-stack-dev3/providers/"
    "Microsoft.ContainerService/managedClusters/spi-stack-dev3"
)


def _inputs(**overrides) -> OnboardInputs:
    base = dict(
        service="partition",
        repo="my-org/partition",
        aks_cluster="spi-stack-dev3",
        aks_rg="spi-stack-dev3",
        identities_rg="spi-stack-dev3",
        namespace="osdu",
        flux_namespace="osdu-flux",
    )
    base.update(overrides)
    inp = OnboardInputs(**base)
    inp.cluster_resource_id = CLUSTER_ID
    return inp


def test_identity_and_role_names_are_service_scoped():
    inp = _inputs()
    assert inp.identity_name == "spi-ci-partition"
    assert inp.deploy_role_name == "spi-ci-partition-deploy"


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


def test_osdu_branch_subjects_cover_the_three_branches():
    assert set(OSDU_BRANCHES) == {"main", "fork_integration", "fork_upstream"}


def test_secret_write_policy_rehome_and_idempotency():
    # First onboard: no secret yet -> write.
    assert _should_write_secrets(secret_present=False, is_rehome=False, force=False) is True
    # Idempotent re-run against the same cluster (same identity already set) -> skip.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=False) is False
    # Re-home onto a new cluster (identity changed) -> rewrite so the secret follows the variable.
    assert _should_write_secrets(secret_present=True, is_rehome=True, force=False) is True
    # Explicit force -> rewrite.
    assert _should_write_secrets(secret_present=True, is_rehome=False, force=True) is True
