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

"""Deployment orchestrator.

Provisions Azure PaaS (via ``azure_infra.provision_azure_infra``), bootstraps
the cluster (namespaces, StorageClasses, Gateway API CRDs, ingress ConfigMap,
Workload Identity SAs, in-cluster seed secrets), activates GitOps via Flux,
and writes the KV runtime secrets that OSDU services read at startup.
"""

import hashlib
import json
import os
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

import typer

from .azure_infra import provision_azure_infra
from .bicep import run_bicep_deployment
from .bootstrap import (
    create_istio_revision_configmap,
    create_storage_classes,
    ensure_namespaces,
    install_gateway_api_crds,
)
from .config import Config, IngressMode, Profile
from .console import console, display_result, display_yaml
from .images import (
    DEFAULT_IMAGE_BRANCH,
    IMAGE_LOCK_CONFIGMAP,
    IMAGE_LOCK_NAMESPACE,
    ImageResolutionError,
    ResolvedImage,
    image_lock_key,
    image_lock_missing_schema_load,
    image_lock_names,
    resolve_image_lock,
    schema_load_lock_patch,
)
from .ingress import (
    create_ingress_config,
    discover_dns_zone,
    get_ingress_ip,
    resolve_post_deploy_inputs,
)
from .paths import INFRA_ROOT
from .pins import PinError, live_pins, render_lock_with_pins
from .secrets import ensure_secrets, get_or_create_seed
from .shell import kubectl_apply_yaml, kubectl_json, prune_kube_context, run_command, run_process
from .templates import (
    istio_auth_resources,
    osdu_config_configmap,
    spi_init_values_configmap,
    workload_identity_sa,
)

GITREPO_NAME = "osdu-spi-stack-system"
SCHEMA_LOAD_KUSTOMIZATION = "spi-osdu-schema-load"
OSDU_CONFIG_ROLLOUT_ANNOTATION = "spi.osdu/config-rollout-hash"

INFRA_FLUX_BICEP = INFRA_ROOT / "flux.bicep"
DEFAULT_SERVICE_AUDIENCE = "https://management.azure.com"


def _resolve_aad_client_id(_identity_client_id: str) -> str:
    """Return the audience services should mint service-to-service tokens for.

    Managed identities cannot themselves be token audiences (AADSTS100040).
    SPI therefore defaults to the ARM resource, which every Workload Identity
    can request and the mesh already accepts. Operators may override this with
    a separate OSDU app registration through AAD_CLIENT_ID.
    """
    return os.environ.get("AAD_CLIENT_ID", "").strip() or DEFAULT_SERVICE_AUDIENCE


def _create_osdu_config(config: Config, infra_outputs: dict) -> None:
    """Create the osdu-config ConfigMap and workload identity SAs."""
    console.print("\n[bold]Creating OSDU configuration...[/bold]")

    partition = config.primary_partition
    identity_client_id = infra_outputs.get("identity_client_id", "")
    aad_client_id = _resolve_aad_client_id(identity_client_id)
    yaml_content = osdu_config_configmap(
        domain="",  # Updated later by `spi info` once external IP is known
        primary_partition=partition,
        tenant_id=infra_outputs.get("tenant_id", ""),
        identity_client_id=identity_client_id,
        aad_client_id=aad_client_id,
        keyvault_uri=infra_outputs.get("keyvault_uri", ""),
        keyvault_name=config.keyvault_name,
        primary_cosmosdb_endpoint=infra_outputs.get(f"{partition}_cosmos_endpoint", ""),
        primary_storage_account_name=infra_outputs.get("common_storage_name", ""),
        primary_servicebus_namespace=infra_outputs.get(f"{partition}_sb_namespace", ""),
        appinsights_key=infra_outputs.get("app_insights_instrumentation_key", ""),
        app_insights_connection_string=infra_outputs.get("app_insights_connection_string", ""),
    )
    display_yaml(yaml_content, "ConfigMap: osdu-config")
    kubectl_apply_yaml(yaml_content, "apply osdu-config ConfigMap")
    display_result("osdu-config ConfigMap created")
    _restart_osdu_deployments_if_config_changed(yaml_content)

    for ns in ["platform", "osdu"]:
        sa_yaml = workload_identity_sa(
            namespace=ns,
            client_id=infra_outputs.get("identity_client_id", ""),
            tenant_id=infra_outputs.get("tenant_id", ""),
        )
        kubectl_apply_yaml(sa_yaml, f"apply workload-identity-sa in {ns}")
    display_result("Workload Identity ServiceAccounts created")


def _restart_osdu_deployments_if_config_changed(
    yaml_content: str,
) -> None:
    """Restart existing services until the current envFrom config is applied."""
    config_hash = hashlib.sha256(yaml_content.encode("utf-8")).hexdigest()
    configmap = run_command(
        ["kubectl", "get", "configmap", "osdu-config", "--namespace", "osdu", "--output", "json"],
        display=False,
        check=False,
    )
    if configmap.returncode != 0:
        raise RuntimeError(
            "Unable to read osdu-config rollout state: "
            f"{configmap.stderr.strip() or configmap.stdout.strip()}"
        )
    try:
        configmap_json = json.loads(configmap.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Unable to parse osdu-config rollout state.") from exc

    applied_hash = (
        configmap_json.get("metadata", {})
        .get("annotations", {})
        .get(OSDU_CONFIG_ROLLOUT_ANNOTATION, "")
    )
    if applied_hash == config_hash:
        return

    deployments = run_command(
        ["kubectl", "get", "deployment", "--namespace", "osdu", "--output", "name"],
        display=False,
        check=False,
    )
    if deployments.returncode != 0:
        raise RuntimeError(
            "Unable to list OSDU deployments before applying the updated "
            f"configuration: {deployments.stderr.strip() or deployments.stdout.strip()}"
        )
    if deployments.stdout.strip():
        run_command(
            ["kubectl", "rollout", "restart", "deployment", "--namespace", "osdu"],
            description="Restart services for updated osdu-config",
        )
        display_result("OSDU service deployments restarted with updated configuration")

    # Record success only after the restart request completes. If listing or
    # restart fails, the old hash remains and the next spi up retries.
    run_command(
        [
            "kubectl",
            "annotate",
            "configmap",
            "osdu-config",
            "--namespace",
            "osdu",
            f"{OSDU_CONFIG_ROLLOUT_ANNOTATION}={config_hash}",
            "--overwrite",
        ],
        description="Record osdu-config rollout state",
    )


def _create_istio_auth(config: Config, infra_outputs: dict) -> None:
    """Apply RequestAuthentication + PeerAuthentication + EnvoyFilter that
    project the JWT payload into x-app-id / x-user-id headers (ADR-016).
    Required because the Azure-provider OSDU service images read identity
    from those headers; without these resources every authenticated call is
    rejected with app-id= empty.
    """
    console.print("\n[bold]Applying OSDU Istio JWT projection...[/bold]")
    identity_client_id = infra_outputs.get("identity_client_id", "")
    yaml_content = istio_auth_resources(
        namespace="osdu",
        tenant_id=infra_outputs.get("tenant_id", ""),
        entra_client_id=identity_client_id,
        aad_client_id=_resolve_aad_client_id(identity_client_id),
    )
    display_yaml(yaml_content, "Istio: RequestAuthentication + PeerAuthentication + EnvoyFilter")
    kubectl_apply_yaml(yaml_content, "apply osdu Istio JWT projection")
    display_result(
        "Istio JWT projection applied (RequestAuthentication, PeerAuthentication, EnvoyFilter)"
    )


def _create_spi_init_values(config: Config) -> None:
    """Apply the spi-init-values ConfigMap that the osdu-spi-init HelmRelease
    consumes via valuesFrom. Must run before Flux reconciles the HelmRelease.
    """
    console.print("\n[bold]Creating SPI init values ConfigMap...[/bold]")
    yaml_content = spi_init_values_configmap(config.data_partitions, config.creator_user_ids)
    display_yaml(yaml_content, "ConfigMap: spi-init-values")
    kubectl_apply_yaml(yaml_content, "apply spi-init-values ConfigMap")
    display_result(
        f"spi-init-values ConfigMap created for partitions: {', '.join(config.data_partitions)}"
        + (
            f"; creator access: {', '.join(config.creator_user_ids)}"
            if config.creator_user_ids
            else "; creator access disabled"
        )
    )


def _resolve_image_lock(config: Config) -> dict[str, ResolvedImage]:
    """Resolve the configured service image baseline."""

    selector = config.image_ref or config.image_tag
    console.print(
        f"\n[bold]Resolving {config.image_source.value} service images at {selector}...[/bold]"
    )
    try:
        resolved = resolve_image_lock(
            source=config.image_source,
            tag=config.image_tag,
            ref=config.image_ref,
            org=config.image_org,
            profile=config.gitops_profile,
        )
    except ImageResolutionError as exc:
        console.print(f"[error]Unable to resolve OSDU service images: {exc}[/error]")
        raise

    for name, image in resolved.items():
        console.print(
            f"  [success]{name}[/success] -> {image.repository.split('/')[-1]}:{image.tag[:12]}"
        )

    return resolved


def _create_image_lock(image_lock_yaml: str) -> None:
    """Apply the generated osdu-image-lock ConfigMap."""

    console.print("\n[bold]Creating OSDU image lock...[/bold]")
    display_yaml(image_lock_yaml, "ConfigMap: osdu-image-lock")
    kubectl_apply_yaml(image_lock_yaml, "apply osdu-image-lock ConfigMap")
    display_result("osdu-image-lock ConfigMap created")


def _validate_existing_image_lock(profile: Profile) -> None:
    """Require a complete live image lock when image refresh is disabled."""
    configmap = kubectl_json(["get", "configmap", IMAGE_LOCK_CONFIGMAP, "-n", IMAGE_LOCK_NAMESPACE])
    data = configmap.get("data") if configmap else None
    if not isinstance(data, dict):
        raise RuntimeError(
            f"--no-refresh-images requires an existing "
            f"{IMAGE_LOCK_NAMESPACE}/{IMAGE_LOCK_CONFIGMAP} ConfigMap"
        )

    if image_lock_missing_schema_load(data):
        console.print("\n[bold]Backfilling schema-load into the existing image lock...[/bold]")
        try:
            patch = schema_load_lock_patch(data, branch=DEFAULT_IMAGE_BRANCH)
        except ImageResolutionError as exc:
            raise RuntimeError(
                "The existing image lock predates schema-load and could not be upgraded: "
                f"{exc}. Re-run without --no-refresh-images."
            ) from exc
        run_command(
            [
                "kubectl",
                "patch",
                "configmap",
                IMAGE_LOCK_CONFIGMAP,
                "-n",
                IMAGE_LOCK_NAMESPACE,
                "--type=merge",
                "-p",
                json.dumps({"data": patch}),
            ],
            description=f"Backfill schema-load entries in {IMAGE_LOCK_CONFIGMAP}",
        )
        data.update(patch)
        display_result(f"{IMAGE_LOCK_CONFIGMAP} ConfigMap updated with schema-load")

    required_keys = {
        f"{image_lock_key(name)}_{suffix}"
        for name in image_lock_names(profile.value)
        for suffix in ("IMAGE_REPOSITORY", "IMAGE_TAG", "IMAGE_DIGEST")
    }
    missing = sorted(key for key in required_keys if not data.get(key))
    if missing:
        raise RuntimeError(
            f"The existing image lock does not cover profile {profile.value!r}; "
            f"missing keys: {', '.join(missing)}. Re-run without --no-refresh-images."
        )


def _prepare_schema_load_upgrade() -> None:
    """Make the live schema-load child safe before the Git source advances.

    An existing Flux configuration can reconcile a new source artifact before
    its parent Kustomization has applied the new child spec. Preinstall the
    substitution and force-replacement fields so that transition cannot render
    the new Job with literal image placeholders or retain its immutable old pod
    template.
    """
    result = run_command(
        [
            "kubectl",
            "get",
            "kustomization",
            SCHEMA_LOAD_KUSTOMIZATION,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "name",
        ],
        description=f"Check Kustomization exists ({SCHEMA_LOAD_KUSTOMIZATION})",
        display=False,
    )
    if not result.stdout.strip():
        return

    patch = {
        "spec": {
            "force": True,
            "postBuild": {
                "substituteFrom": [
                    {
                        "kind": "ConfigMap",
                        "name": IMAGE_LOCK_CONFIGMAP,
                        "optional": False,
                    }
                ]
            },
        }
    }
    run_command(
        [
            "kubectl",
            "patch",
            "kustomization",
            SCHEMA_LOAD_KUSTOMIZATION,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--type=merge",
            "-p",
            json.dumps(patch),
        ],
        description="Prepare schema-load Kustomization for source upgrade",
    )
    display_result("schema-load Kustomization prepared for source upgrade")


def _wait_for_namespace(namespace: str, timeout_seconds: int = 300) -> None:
    """Wait until a namespace exists."""
    deadline = time.time() + timeout_seconds
    last_error = ""
    with console.status(f"[bold]Waiting for namespace {namespace}...[/bold]"):
        while time.time() < deadline:
            result = run_process(
                ["kubectl", "get", "namespace", namespace],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0:
                display_result(f"Namespace {namespace} ready")
                return
            last_error = (result.stderr or result.stdout or "").strip()
            time.sleep(5)
    console.print(f"[error]Namespace {namespace} was not created: {last_error}[/error]")
    raise typer.Exit(code=1)


def _deploy_flux_config(config: Config, activate_gitops: bool) -> None:
    """Install Flux, optionally creating the GitOps configuration."""
    if activate_gitops:
        console.print("\n[bold]Deploying Flux GitOps config via Bicep...[/bold]")
        deployment_name = f"spi-flux-{config.env or 'base'}"
    else:
        console.print("\n[bold]Installing Flux extension via Bicep...[/bold]")
        deployment_name = f"spi-flux-ext-{config.env or 'base'}"

    run_bicep_deployment(
        template_path=str(INFRA_FLUX_BICEP),
        parameters={
            "clusterName": config.cluster_name,
            "repoUrl": config.repo_url,
            "repoBranch": config.repo_branch,
            "profile": config.gitops_profile,
            "ingressMode": config.gitops_ingress_profile,
            "activateGitOps": activate_gitops,
            "gitRepositoryLocalAuthRef": (
                "osdu-spi-stack-system-auth" if config.repo_url.startswith("ssh://") else ""
            ),
        },
        resource_group=config.resource_group,
        deployment_name=deployment_name,
    )
    if not activate_gitops:
        # The extension-only pass installs the Flux controllers into flux-system.
        # Waiting on osdu-flux would prove nothing: ensure_namespaces() already
        # created it earlier in Phase 4, so that check passes instantly whether or
        # not the extension is ready. Wait for the namespace the extension itself
        # creates, so Phase 5 cannot race controller installation.
        _wait_for_namespace("flux-system")


def _write_keyvault_bootstrap_secrets(
    config: Config,
    keyvault_name: str,
    storage_account_name: str,
    aad_client_id: str,
    elastic_password: str,
    redis_password: str,
) -> None:
    """Write the small set of secrets OSDU services read at startup.

    Partition reads tbl-storage-endpoint to locate its metadata table.
    Indexer and workflow read redis-hostname/redis-password via KeyVaultFacade.
    Search and indexer read {partition}-elastic-* via partition service API.

    Elastic credentials are written per-partition because the partition record
    resolves them by partition-prefixed secret name. All partitions share the
    single in-cluster ES cluster and therefore the same elastic user/password.
    """
    console.print("\n[bold]Writing OSDU bootstrap secrets to Key Vault...[/bold]")
    tbl_endpoint = f"https://{storage_account_name}.table.core.windows.net/"
    # The ECK Elasticsearch HTTP cert SANs cover the short service name
    # (elasticsearch-es-http.platform.svc) but NOT the fully-qualified
    # ...svc.cluster.local form. With elastic-ssl-enabled=true the OSDU client
    # verifies the hostname, so the endpoint MUST use a SAN-listed name or
    # every search/indexer call fails with SSLPeerUnverifiedException.
    elastic_endpoint = "https://elasticsearch-es-http.platform.svc:9200"
    redis_hostname = "platform-redis-master.platform.svc.cluster.local"

    secrets_to_write: list[tuple[str, str]] = [
        ("tbl-storage-endpoint", tbl_endpoint),
        ("aad-client-id", aad_client_id),
        ("redis-hostname", redis_hostname),
        ("redis-password", redis_password),
    ]
    for p in config.data_partitions:
        secrets_to_write.extend(
            [
                (f"{p}-elastic-endpoint", elastic_endpoint),
                (f"{p}-elastic-username", "elastic"),
                (f"{p}-elastic-password", elastic_password),
            ]
        )

    # The deployer's Key Vault Secrets Officer assignment is created by
    # rbac.bicep moments earlier; ARM data-plane propagation can lag a few
    # minutes. Retry the first write on ForbiddenByRbac so we don't fail
    # the whole deploy on a benign timing window.
    deadline = time.time() + 300
    first = True
    for name, value in secrets_to_write:
        while True:
            result = run_process(
                [
                    "az",
                    "keyvault",
                    "secret",
                    "set",
                    "--vault-name",
                    keyvault_name,
                    "--name",
                    name,
                    "--value",
                    value,
                    "--output",
                    "none",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            combined = (result.stderr or "") + (result.stdout or "")
            if "ForbiddenByRbac" in combined and first and time.time() < deadline:
                console.print(
                    "  [info]Key Vault role assignment not yet propagated; retrying in 30s...[/info]"
                )
                time.sleep(30)
                continue
            if result.stderr.strip():
                console.print(
                    f"[error]az keyvault secret set failed for {name}: {result.stderr.strip()}[/error]"
                )
            raise typer.Exit(code=1)
        first = False
        console.print(f"  [success]{name}[/success]")

    display_result(f"{len(secrets_to_write)} Key Vault secrets written")


def _pin_gitops_source() -> None:
    """Suspend the GitRepository so future commits don't auto-roll (ADR-014).

    Waits up to 120s for the source-controller to publish its first artifact,
    then patches ``spec.suspend: true``. The wait is non-fatal: on timeout we
    warn and suspend anyway. Downstream Kustomizations/HelmReleases keep
    reconciling from the cached artifact.
    """
    console.print("\n[bold]Pinning environment to deploy commit...[/bold]")

    wait_result = run_process(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            f"gitrepository/{GITREPO_NAME}",
            "-n",
            "osdu-flux",
            "--timeout=120s",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if wait_result.returncode != 0:
        console.print(
            "  [warning]GitRepository did not become Ready within 120s; "
            "suspending anyway. Run 'spi reconcile' if reconciliation stalls.[/warning]"
        )

    run_command(
        [
            "kubectl",
            "patch",
            "gitrepository",
            GITREPO_NAME,
            "-n",
            "osdu-flux",
            "--type=merge",
            "-p",
            '{"spec":{"suspend":true}}',
        ],
        description="Suspend GitRepository (pin to deploy commit)",
        check=False,
    )
    display_result(
        "GitRepository pinned. Run 'spi reconcile' to pull updates, "
        "or 'spi reconcile --resume' to enable auto-reconciliation."
    )


def deploy_azure(
    config: Config,
    dry_run: bool = False,
    refresh_images: bool = True,
    azure_account: Optional[Dict[str, Any]] = None,
    deployer_principal: Optional[Tuple[str, str]] = None,
) -> None:
    """Provision Azure infra, bootstrap Kubernetes, deploy via GitOps.

    In ``dry_run`` mode, only the Azure PaaS Bicep preview runs; AKS, the
    Kubernetes bootstrap phase, and GitOps activation are skipped so the
    caller can inspect what would change without actually provisioning.
    """
    resolved_images: dict[str, ResolvedImage] = {}
    # Core and graduated deploy OSDU services that consume the image lock.
    # Minimal and bare skip registry resolution entirely.
    if (
        refresh_images
        and not dry_run
        and config.profile
        in {
            Profile.CORE,
            Profile.GRADUATED,
        }
    ):
        # Resolve before provisioning so registry/API failures stop quickly and
        # never leave a partially configured cluster with a mixed image set.
        resolved_images = _resolve_image_lock(config)

    # For dns mode we need to resolve the DNS zone BEFORE running main.bicep
    # so the conditional external-dns-identity + DNS Zone Contributor role
    # modules get the right scope + name.
    if not dry_run and config.ingress_mode == IngressMode.DNS and not config.dns_zone:
        zone, rg = discover_dns_zone()
        config.dns_zone = zone
        config.dns_zone_rg = rg

    # Phase 1-3: Azure infrastructure
    infra_outputs = provision_azure_infra(
        config,
        dry_run=dry_run,
        account=azure_account,
        deployer_principal=deployer_principal,
    )

    if dry_run:
        return

    # Phase 4: Kubernetes bootstrap
    istio_revision = ensure_namespaces(infra_outputs.get("istio_revision", ""))
    create_istio_revision_configmap(istio_revision)
    create_storage_classes()
    install_gateway_api_crds()

    # AKS Automatic protects the extension-managed flux-system namespace.
    # Install the controllers there, but keep SPI-owned GitOps inputs in
    # the user-managed osdu-flux namespace before enabling reconciliation.
    _deploy_flux_config(config, activate_gitops=False)

    ensure_secrets()
    if resolved_images:
        try:
            pins = live_pins()
        except PinError as exc:
            console.print(f"[error]{exc}[/error]")
            console.print(
                "[error]Cannot verify service pin state; aborting before the "
                "image lock overwrite could revert an active pin.[/error]"
            )
            raise typer.Exit(code=1)
        _create_image_lock(
            render_lock_with_pins(
                resolved_images,
                pins,
                source=config.image_source,
                tag=config.image_tag,
                ref=config.image_ref,
                org=config.image_org,
                profile=config.gitops_profile,
            )
        )
        if pins:
            console.print(
                "[warning]Active service pins preserved: "
                + ", ".join(f"{name} (MR !{pin.mr})" for name, pin in sorted(pins.items()))
                + "; release with 'spi service reset <service>'.[/warning]"
            )
    elif config.profile in {Profile.CORE, Profile.GRADUATED}:
        _validate_existing_image_lock(config.profile)
    if config.profile in {Profile.CORE, Profile.GRADUATED}:
        _prepare_schema_load_upgrade()
    _create_osdu_config(config, infra_outputs)
    _create_istio_auth(config, infra_outputs)
    _create_spi_init_values(config)

    # Phase 4b: Ingress mode resolution (requires live cluster + Istio LB)
    resolve_post_deploy_inputs(config)
    create_ingress_config(
        config=config,
        external_dns_client_id=infra_outputs.get("external_dns_client_id", ""),
        tenant_id=infra_outputs.get("tenant_id", ""),
        gateway_ip=get_ingress_ip(),
        istio_revision=infra_outputs.get("istio_revision", ""),
    )

    # Phase 5: GitOps activation (Kustomization via Bicep)
    _deploy_flux_config(config, activate_gitops=True)
    display_result(
        f"GitOps activated for profile: {config.profile.value}, "
        f"ingress: {config.ingress_mode.value}"
    )

    # Phase 6: Non-blocking runtime writes.
    # Cross-namespace CA copies and the Redis Istio DestinationRule moved
    # into Flux (software/stacks/osdu/bootstrap/) as Pass 1 of ADR-011.
    # Only the KV seed writes remain here; they run in seconds since all
    # values are known as soon as infra is up and the seed is generated.
    seed = get_or_create_seed()
    _write_keyvault_bootstrap_secrets(
        config=config,
        keyvault_name=config.keyvault_name,
        storage_account_name=infra_outputs.get("common_storage_name", ""),
        aad_client_id=_resolve_aad_client_id(infra_outputs.get("identity_client_id", "")),
        elastic_password=seed["elastic_password"],
        redis_password=seed["redis_password"],
    )

    # Phase 7: Pin the environment to the deploy commit (ADR-014).
    # Future commits to the tracked branch won't auto-reconcile until the
    # user runs 'spi reconcile' or 'spi reconcile --resume'.
    _pin_gitops_source()


def _cluster_api_server(config: Config) -> str:
    """The API server FQDN of the cluster about to be deleted, or "".

    Read while the resource group still exists, because it is what proves a
    kubeconfig context belongs to this cluster rather than a same-named one
    in another subscription. Comes back empty when the cluster is already
    gone or was never created, and the prune then leaves the kubeconfig
    alone. `privateFqdn` comes first because a private cluster populates both
    fields, and `az aks get-credentials` without `--public-fqdn` is what wrote
    the kubeconfig entry this is compared against.
    """
    result = run_command(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.cluster_name,
            "--query",
            "privateFqdn || fqdn",
            "--output",
            "tsv",
        ],
        description=f"Look up API server for {config.cluster_name}",
        display=False,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def cleanup_azure(config: Config) -> None:
    """Delete Azure resource group and all resources.

    The kubeconfig entries `spi up` merged in point at a cluster that is
    about to stop answering, so they are pruned on the way out. The prune
    waits for Azure to report the resource group gone; `--no-wait` returns on
    acceptance, and an accepted delete can still fail afterwards.
    """
    console.print("\n[bold]Cleaning up Azure resources...[/bold]")
    api_server = _cluster_api_server(config)
    result = run_command(
        ["az", "group", "delete", "--name", config.resource_group, "--yes", "--no-wait"],
        description=f"Delete resource group: {config.resource_group}",
        check=False,
    )
    if result.returncode != 0:
        console.print(f"[error]Azure cleanup request failed for {config.resource_group}.[/error]")
        raise typer.Exit(code=1)

    console.print("  [info]Waiting briefly for Azure to acknowledge the deletion...[/info]")
    deadline = time.time() + 60
    while time.time() < deadline:
        exists = run_command(
            ["az", "group", "exists", "--name", config.resource_group],
            description=f"Check resource group status: {config.resource_group}",
            display=False,
            check=False,
        )
        if exists.returncode == 0 and exists.stdout.strip().lower() == "false":
            prune_kube_context(config.cluster_name, server_fqdn=api_server)
            display_result(f"Resource group {config.resource_group} deleted")
            return
        time.sleep(10)

    display_result("Cleanup accepted by Azure; deletion is continuing in the background")
    console.print(
        f"  [warning]Verify later with: az group exists --name {config.resource_group}[/warning]"
    )
    console.print(
        f"  [warning]kubeconfig context {config.cluster_name} is left in place until the "
        f"deletion is confirmed[/warning]"
    )
