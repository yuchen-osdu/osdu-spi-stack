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

"""Cluster-side onboarding: grant a GitHub service-fork repo permission to deploy
into this spi-stack cluster (CI/CD deploy lane, design SS9.4.A).

This is the cluster-side half of a two-step onboarding flow. It creates the Azure
identity + RBAC a fork's GitHub Actions need to `kubectl set image` its own Deployment,
then writes the handoff secrets/variables onto the repo. The fork-side half
(init.yml / settings-apply.yml in osdu-spi) configures the repo itself.

Authorization model: this cluster runs **Azure RBAC for Kubernetes**
(`aadProfile.enableAzureRBAC = true`, `disableLocalAccounts = true`). A plain
Kubernetes RoleBinding is therefore NOT the authorization path (Phase 0 gate 0b);
deploy permission is granted via **Azure role assignments**. To honor the design's
least-privilege intent (SS6.1 step 3 -- patch only the named Deployment + read
pods/replicasets/events/logs, NOT broad write), we define a **custom Azure role**
with the equivalent dataActions and assign it at the namespace scope, rather than
the broad built-in "Azure Kubernetes Service RBAC Writer".

Every step is idempotent: re-running against the same --repo makes no duplicate
identities or role assignments and does not overwrite secrets unless
--force-rewrite-secrets is given.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import typer

from .console import console, display_result
from .shell import run_command

# Federated-credential subjects to register on the target repo. The corp Entra tenant
# commonly disables wildcard subjects, so we enumerate the OSDU branch set explicitly
# plus the pull_request subject. (Design SS6.1 step 2: "enumerate explicit refs if the
# wildcard feature is unavailable".)
OSDU_BRANCHES = ("main", "fork_integration", "fork_upstream")
GH_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
GH_OIDC_AUDIENCE = "api://AzureADTokenExchange"

# Least-privilege dataActions for the custom deploy role (Azure RBAC for Kubernetes).
# Mirrors the Kubernetes Role in design SS6.1 step 3: patch the deployment + read the
# pod/replicaset/event/log chain that `kubectl rollout status` and diagnostics walk.
DEPLOY_DATA_ACTIONS = [
    "Microsoft.ContainerService/managedClusters/apps/deployments/read",
    "Microsoft.ContainerService/managedClusters/apps/deployments/write",
    "Microsoft.ContainerService/managedClusters/apps/replicasets/read",
    "Microsoft.ContainerService/managedClusters/pods/read",
    "Microsoft.ContainerService/managedClusters/pods/log/read",
    "Microsoft.ContainerService/managedClusters/events/read",
]
# Read-only dataActions for the Flux CI-mode pre-check (list Kustomizations).
FLUX_READ_DATA_ACTIONS = [
    "Microsoft.ContainerService/managedClusters/kustomize.toolkit.fluxcd.io/kustomizations/read",
]
AKS_CLUSTER_USER_ROLE = "Azure Kubernetes Service Cluster User Role"
KEY_VAULT_SECRETS_USER_ROLE = "Key Vault Secrets User"


def _resolve(cmd_list: List[str]) -> List[str]:
    """Resolve argv[0] to an absolute path so subprocess (shell=False) finds it.

    Windows exposes az/gh/kubectl as ``.cmd`` shims that PATHEXT resolves for a shell but
    not for ``subprocess.run`` with a bare name. Resolve up front; fall back to the name.
    """
    if not cmd_list:
        return cmd_list
    found = shutil.which(cmd_list[0])
    return [found, *cmd_list[1:]] if found else cmd_list


@dataclass
class OnboardInputs:
    service: str
    repo: str  # org/repo
    aks_cluster: str
    aks_rg: str
    identities_rg: str
    namespace: str = "osdu"
    flux_namespace: str = "flux-system"
    keyvault: Optional[str] = None
    dry_run: bool = False
    force_rewrite_secrets: bool = False
    # Captured/derived during the run.
    subscription_id: str = ""
    tenant_id: str = ""
    cluster_resource_id: str = ""
    identity_client_id: str = ""
    identity_principal_id: str = ""
    deployment_name: str = ""
    container_name: str = ""
    kv_secret_names: List[str] = field(default_factory=list)

    @property
    def identity_name(self) -> str:
        return f"spi-ci-{self.service}"

    @property
    def deploy_role_name(self) -> str:
        return f"spi-ci-{self.service}-deploy"

    @property
    def namespace_scope(self) -> str:
        return f"{self.cluster_resource_id}/namespaces/{self.namespace}"

    @property
    def flux_namespace_scope(self) -> str:
        return f"{self.cluster_resource_id}/namespaces/{self.flux_namespace}"


def _az_json(args: List[str], check: bool = True) -> Any:
    """Run an `az ... -o json` command silently and return parsed JSON (or None).

    Used for existence probes and lookups where the transparent command panel from
    ``run_command`` would be noise. ``check=False`` lets callers treat a non-zero
    exit (e.g. "not found") as ``None`` for idempotency checks.
    """
    cmd = _resolve(["az", *args, "--output", "json"])
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        if check:
            console.print(
                f"  [error]az {' '.join(args[:3])} failed: {(result.stderr or '').strip()}[/error]"
            )
            raise typer.Exit(code=1)
        return None
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _plan(message: str) -> None:
    console.print(f"  [warning][dry-run][/warning] {message}")


# --------------------------------------------------------------------------------------
# Step 1 - operator precondition checks
# --------------------------------------------------------------------------------------
def _check_preconditions(inp: OnboardInputs) -> None:
    """Verify az/kubectl/gh are authenticated with the access onboarding needs.

    Fails fast with a remediation message rather than half-applying a grant.
    """
    console.print("\n[bold]Verifying operator preconditions...[/bold]")

    account = _az_json(["account", "show"], check=False)
    if not account:
        console.print(
            "  [error]Azure CLI is not logged in. Run: az login --tenant <tenant> "
            "and az account set --subscription <sub>[/error]"
        )
        raise typer.Exit(code=1)
    inp.subscription_id = account.get("id", "")
    inp.tenant_id = account.get("tenantId", "")
    console.print(
        f"  [info]Subscription: {account.get('name', '?')} ({inp.subscription_id})[/info]"
    )

    # Resolve + cache the cluster resource id (also proves AKS read access).
    cluster = _az_json(
        ["aks", "show", "--name", inp.aks_cluster, "--resource-group", inp.aks_rg], check=False
    )
    if not cluster:
        console.print(
            f"  [error]Cannot read AKS cluster {inp.aks_cluster} in {inp.aks_rg}. "
            "Check --aks-cluster/--aks-rg and your Azure RBAC.[/error]"
        )
        raise typer.Exit(code=1)
    inp.cluster_resource_id = cluster.get("id", "")
    if not cluster.get("aadProfile", {}).get("enableAzureRbac"):
        console.print(
            "  [warning]Cluster does not report Azure RBAC for Kubernetes. This command "
            "provisions Azure role assignments; on a non-Azure-RBAC cluster a Kubernetes "
            "RoleBinding would be required instead (design SS6.1 step 3).[/warning]"
        )

    # gh auth + admin on the target repo (needed to write secrets/variables).
    gh_status = subprocess.run(_resolve(["gh", "auth", "status"]), capture_output=True, text=True)
    if gh_status.returncode != 0:
        console.print("  [error]GitHub CLI is not authenticated. Run: gh auth login[/error]")
        raise typer.Exit(code=1)
    repo_view = subprocess.run(
        _resolve(["gh", "repo", "view", inp.repo, "--json", "viewerPermission"]),
        capture_output=True,
        text=True,
    )
    if repo_view.returncode != 0:
        console.print(
            f"  [error]Cannot access repo {inp.repo} via gh. Check the name and SSO.[/error]"
        )
        raise typer.Exit(code=1)
    perm = (json.loads(repo_view.stdout or "{}").get("viewerPermission") or "").upper()
    if perm not in ("ADMIN", "MAINTAIN", "WRITE"):
        console.print(
            f"  [error]Insufficient permission on {inp.repo} (have {perm or 'NONE'}); need "
            "WRITE+ to set secrets/variables.[/error]"
        )
        raise typer.Exit(code=1)

    display_result("Preconditions OK (az + AKS read + gh repo write)")


# --------------------------------------------------------------------------------------
# Step 2 - verify the Deployment exists, capture names
# --------------------------------------------------------------------------------------
def _verify_deployment(inp: OnboardInputs) -> None:
    """Confirm Deployment/<name> exists in the namespace; capture deployment + container.

    The convention is Deployment name == ``osdu-<service>``; we resolve it from the live
    cluster rather than assuming, and read the first container's name (D13).
    """
    console.print("\n[bold]Verifying target Deployment...[/bold]")
    # Ensure we have a kube context for this cluster (idempotent). Skip in dry-run -- fetching
    # credentials mutates the local kubeconfig, which a plan-only run must not do.
    if not inp.dry_run:
        run_command(
            [
                "az",
                "aks",
                "get-credentials",
                "--resource-group",
                inp.aks_rg,
                "--name",
                inp.aks_cluster,
                "--overwrite-existing",
                "--only-show-errors",
            ],
            description="Get AKS credentials",
            check=False,
        )

    candidate = f"osdu-{inp.service}"
    deployment = subprocess.run(
        _resolve(["kubectl", "get", "deployment", candidate, "-n", inp.namespace, "-o", "json"]),
        capture_output=True,
        text=True,
    )
    if deployment.returncode != 0:
        msg = (
            f"Deployment/{candidate} not found in namespace '{inp.namespace}'. "
            "The service must be deployed (its HelmRelease reconciled) before onboarding."
        )
        if inp.dry_run:
            # A plan-only run may not have a kube context yet; warn and assume the convention.
            console.print(
                f"  [warning][dry-run] could not read the live Deployment ({msg}). Assuming '{candidate}'.[/warning]"
            )
            inp.deployment_name = candidate
            inp.container_name = candidate
            display_result(f"(dry-run) target Deployment assumed '{candidate}'")
            return
        console.print(f"  [error]{msg}[/error]")
        raise typer.Exit(code=1)
    obj = json.loads(deployment.stdout)
    inp.deployment_name = obj["metadata"]["name"]
    containers = obj["spec"]["template"]["spec"]["containers"]
    inp.container_name = containers[0]["name"]
    display_result(
        f"Deployment '{inp.deployment_name}' (container '{inp.container_name}') in '{inp.namespace}'"
    )


# --------------------------------------------------------------------------------------
# Step 3 - User-Assigned Managed Identity
# --------------------------------------------------------------------------------------
def _ensure_identity(inp: OnboardInputs) -> None:
    console.print("\n[bold]Ensuring managed identity...[/bold]")
    existing = _az_json(
        ["identity", "show", "--name", inp.identity_name, "--resource-group", inp.identities_rg],
        check=False,
    )
    if existing:
        display_result(f"Identity '{inp.identity_name}' already exists")
    elif inp.dry_run:
        _plan(f"az identity create --name {inp.identity_name} --resource-group {inp.identities_rg}")
        return
    else:
        run_command(
            [
                "az",
                "identity",
                "create",
                "--name",
                inp.identity_name,
                "--resource-group",
                inp.identities_rg,
                "--output",
                "none",
            ],
            description=f"Create UAMI {inp.identity_name}",
        )
        existing = _az_json(
            ["identity", "show", "--name", inp.identity_name, "--resource-group", inp.identities_rg]
        )
    if existing:
        inp.identity_client_id = existing.get("clientId", "")
        inp.identity_principal_id = existing.get("principalId", "")


# --------------------------------------------------------------------------------------
# Step 4 - federated credentials
# --------------------------------------------------------------------------------------
def _ensure_federated_credentials(inp: OnboardInputs) -> None:
    console.print("\n[bold]Ensuring federated credentials...[/bold]")
    subjects: Dict[str, str] = {
        f"spi-ci-{inp.service}-pull-request": f"repo:{inp.repo}:pull_request",
    }
    for branch in OSDU_BRANCHES:
        subjects[f"spi-ci-{inp.service}-branch-{branch}"] = (
            f"repo:{inp.repo}:ref:refs/heads/{branch}"
        )

    existing = (
        _az_json(
            [
                "identity",
                "federated-credential",
                "list",
                "--identity-name",
                inp.identity_name,
                "--resource-group",
                inp.identities_rg,
            ],
            check=False,
        )
        or []
    )
    existing_subjects = {fc.get("subject") for fc in existing}

    for name, subject in subjects.items():
        if subject in existing_subjects:
            console.print(f"  [info]federated credential for '{subject}' already present[/info]")
            continue
        if inp.dry_run:
            _plan(f"az identity federated-credential create --name {name} --subject {subject}")
            continue
        run_command(
            [
                "az",
                "identity",
                "federated-credential",
                "create",
                "--name",
                name,
                "--identity-name",
                inp.identity_name,
                "--resource-group",
                inp.identities_rg,
                "--issuer",
                GH_OIDC_ISSUER,
                "--subject",
                subject,
                "--audiences",
                GH_OIDC_AUDIENCE,
                "--output",
                "none",
            ],
            description=f"Federated credential {subject}",
        )
    display_result(f"{len(subjects)} federated-credential subject(s) reconciled")


# --------------------------------------------------------------------------------------
# Steps 5-7 - Azure RBAC role assignments
# --------------------------------------------------------------------------------------
def _assign_role(inp: OnboardInputs, role: str, scope: str, description: str) -> None:
    """Idempotently create an Azure role assignment for the identity at a scope."""
    if not inp.identity_principal_id:
        if inp.dry_run:
            _plan(f"(after identity exists) assign '{role}' at {scope}")
            return
        console.print("  [error]Identity principalId unknown; cannot assign role.[/error]")
        raise typer.Exit(code=1)
    existing = _az_json(
        [
            "role",
            "assignment",
            "list",
            "--assignee",
            inp.identity_principal_id,
            "--role",
            role,
            "--scope",
            scope,
        ],
        check=False,
    )
    if existing:
        console.print(f"  [info]{description}: already assigned[/info]")
        return
    if inp.dry_run:
        _plan(f"az role assignment create --role '{role}' --scope {scope}")
        return
    run_command(
        [
            "az",
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            inp.identity_principal_id,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            role,
            "--scope",
            scope,
            "--output",
            "none",
        ],
        description=description,
        check=False,
    )


def _ensure_custom_deploy_role(inp: OnboardInputs) -> None:
    """Create/update the least-privilege custom Azure role for namespace deploy.

    Assignable at the cluster scope; the assignment (separate step) is namespace-scoped.
    """
    role_def = {
        "Name": inp.deploy_role_name,
        "Description": (
            f"SPI CI deploy for {inp.service}: patch Deployment/{inp.deployment_name} and read "
            "pods/replicasets/events/logs in its namespace. Least-privilege (design SS6.1)."
        ),
        "Actions": [],
        "NotActions": [],
        "DataActions": DEPLOY_DATA_ACTIONS + FLUX_READ_DATA_ACTIONS,
        "NotDataActions": [],
        "AssignableScopes": [inp.cluster_resource_id],
    }
    existing = _az_json(["role", "definition", "list", "--name", inp.deploy_role_name], check=False)
    if inp.dry_run:
        verb = "update" if existing else "create"
        _plan(
            f"az role definition {verb} '{inp.deploy_role_name}' (custom least-privilege deploy role)"
        )
        return
    # az role definition create is idempotent-friendly via update when it exists.
    action = "update" if existing else "create"
    run_command(
        [
            "az",
            "role",
            "definition",
            action,
            "--role-definition",
            json.dumps(role_def),
            "--output",
            "none",
        ],
        description=f"{action.capitalize()} custom role {inp.deploy_role_name}",
        check=False,
    )


def _ensure_rbac(inp: OnboardInputs) -> None:
    console.print("\n[bold]Ensuring Azure RBAC...[/bold]")
    # 5. Cluster User (get kubeconfig).
    _assign_role(
        inp, AKS_CLUSTER_USER_ROLE, inp.cluster_resource_id, "AKS Cluster User (cluster scope)"
    )
    # 6. Namespace-scoped least-privilege deploy (custom role).
    _ensure_custom_deploy_role(inp)
    _assign_role(
        inp,
        inp.deploy_role_name,
        inp.namespace_scope,
        f"Custom deploy role on namespace '{inp.namespace}'",
    )
    # Flux read lives in the same custom role; assign it at the flux namespace scope too so
    # the deploy action's suspend pre-check can list Kustomizations there.
    if inp.flux_namespace != inp.namespace:
        _assign_role(
            inp,
            inp.deploy_role_name,
            inp.flux_namespace_scope,
            f"Custom deploy role (flux read) on namespace '{inp.flux_namespace}'",
        )
    # 7. Key Vault Secrets User (acceptance-test secrets).
    if inp.keyvault:
        kv = _az_json(["keyvault", "show", "--name", inp.keyvault], check=False)
        if kv and kv.get("id"):
            inp.kv_secret_names = _list_kv_secret_names(inp.keyvault)
            _assign_role(
                inp,
                KEY_VAULT_SECRETS_USER_ROLE,
                kv["id"],
                f"Key Vault Secrets User on '{inp.keyvault}'",
            )
        else:
            console.print(
                f"  [warning]Key Vault '{inp.keyvault}' not found; skipping KV grant.[/warning]"
            )
    else:
        console.print(
            "  [info]No --keyvault given; skipping Key Vault grant (set it for integration tests).[/info]"
        )


def _list_kv_secret_names(vault: str) -> List[str]:
    data = _az_json(
        ["keyvault", "secret", "list", "--vault-name", vault, "--query", "[].name"], check=False
    )
    return data or []


# --------------------------------------------------------------------------------------
# Step 8 - write handoff secrets + variables to the repo
# --------------------------------------------------------------------------------------
def _gh_set_secret(inp: OnboardInputs, name: str, value: str) -> None:
    if inp.dry_run:
        _plan(f"gh secret set {name} -R {inp.repo} (value hidden)")
        return
    proc = subprocess.run(
        _resolve(["gh", "secret", "set", name, "-R", inp.repo, "--body", value]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        console.print(
            f"  [error]gh secret set {name} failed: {(proc.stderr or '').strip()}[/error]"
        )
        raise typer.Exit(code=1)
    console.print(f"  [success]secret {name} set[/success]")


def _gh_set_variable(inp: OnboardInputs, name: str, value: str) -> None:
    if inp.dry_run:
        _plan(f"gh variable set {name}={value} -R {inp.repo}")
        return
    # `gh variable set` updates if present, so this is idempotent.
    proc = subprocess.run(
        _resolve(["gh", "variable", "set", name, "-R", inp.repo, "--body", value]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        console.print(
            f"  [error]gh variable set {name} failed: {(proc.stderr or '').strip()}[/error]"
        )
        raise typer.Exit(code=1)
    console.print(f"  [success]variable {name}={value} set[/success]")


def _write_handoff(inp: OnboardInputs) -> None:
    console.print("\n[bold]Writing handoff secrets + variables to the repo...[/bold]")
    secret_exists = _secret_present(inp, "AZURE_CLIENT_ID")
    if secret_exists and not inp.force_rewrite_secrets:
        console.print(
            "  [info]AZURE_* secrets already present; leaving as-is "
            "(use --force-rewrite-secrets to overwrite).[/info]"
        )
    else:
        _gh_set_secret(inp, "AZURE_CLIENT_ID", inp.identity_client_id)
        _gh_set_secret(inp, "AZURE_TENANT_ID", inp.tenant_id)
        _gh_set_secret(inp, "AZURE_SUBSCRIPTION_ID", inp.subscription_id)

    # Variables (always reconcile; non-sensitive).
    _gh_set_variable(inp, "K8S_DEPLOYMENT_NAME", inp.deployment_name)
    _gh_set_variable(inp, "K8S_CONTAINER_NAME", inp.container_name)
    # AZURE_CLIENT_ID is ALSO written as a variable (not just a secret) so the deploy lane's
    # validate.yml `if:` can gate on `vars.AZURE_CLIENT_ID != ''` -- un-onboarded forks skip
    # the deploy/integration-test jobs cleanly instead of failing azure/login (design SS6.1
    # step 5 lists AZURE_CLIENT_ID as a variable for use in if: expressions).
    _gh_set_variable(inp, "AZURE_CLIENT_ID", inp.identity_client_id)


def _secret_present(inp: OnboardInputs, name: str) -> bool:
    proc = subprocess.run(
        _resolve(["gh", "secret", "list", "-R", inp.repo]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False
    return any(line.split("\t")[0].strip() == name for line in proc.stdout.splitlines())


# --------------------------------------------------------------------------------------
# Step 9 - JSON summary
# --------------------------------------------------------------------------------------
def _emit_summary(inp: OnboardInputs) -> None:
    summary = {
        "service": inp.service,
        "repo": inp.repo,
        "dry_run": inp.dry_run,
        "identity": {
            "name": inp.identity_name,
            "client_id": inp.identity_client_id,
            "principal_id": inp.identity_principal_id,
        },
        "cluster": {
            "name": inp.aks_cluster,
            "resource_group": inp.aks_rg,
            "namespace": inp.namespace,
            "flux_namespace": inp.flux_namespace,
        },
        "secrets_written": ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_SUBSCRIPTION_ID"],
        "variables_written": {
            "K8S_DEPLOYMENT_NAME": inp.deployment_name,
            "K8S_CONTAINER_NAME": inp.container_name,
            "AZURE_CLIENT_ID": inp.identity_client_id,
        },
        "kv_secret_names_to_populate": inp.kv_secret_names,
        "next_steps": [
            "Set the remaining org variables on the repo/org: AKS_RESOURCE_GROUP, "
            "AKS_CLUSTER_NAME, K8S_NAMESPACE, FLUX_NAMESPACE, GATEWAY_URL, KEYVAULT_NAME.",
            "Set the per-service test variables: ACCEPTANCE_TEST_DIR, "
            "ACCEPTANCE_TEST_SECRET_MAP, ACCEPTANCE_TEST_DEPENDENCIES.",
            f"Run settings-apply.yml on {inp.repo} (or wait for its schedule) to reconcile "
            "rulesets, required-check filtering, and GHCR visibility.",
            "Populate the Key Vault secret VALUES out of band (this command grants read access "
            "but does not set values).",
        ],
    }
    console.print("\n[bold]Onboarding summary[/bold]")
    console.print_json(json.dumps(summary))


def onboard(inp: OnboardInputs) -> None:
    """Run the full cluster-side onboarding flow (design SS9.4.A)."""
    title = f"[bold]spi onboard[/bold] - grant {inp.repo} deploy access to {inp.aks_cluster}"
    if inp.dry_run:
        title += "\n[warning]DRY RUN: planning only, no changes[/warning]"
    from rich.panel import Panel

    console.print(Panel(title, border_style="cyan"))

    _check_preconditions(inp)
    _verify_deployment(inp)
    _ensure_identity(inp)
    _ensure_federated_credentials(inp)
    _ensure_rbac(inp)
    _write_handoff(inp)
    _emit_summary(inp)

    if inp.dry_run:
        console.print("\n[warning]Dry run complete - no changes were made.[/warning]")
    else:
        display_result(f"Onboarding complete for {inp.repo}")
