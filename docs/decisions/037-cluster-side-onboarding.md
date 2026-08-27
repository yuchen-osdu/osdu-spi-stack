# ADR-037: Cluster-Side Onboarding for Service-Fork CI/CD

## Context

The deploy lane (ADR-029, ADR-030) lets a service fork build an image and set it
on a running Deployment. Reaching the cluster couples Azure identity, GitHub
OIDC subjects, Azure and Kubernetes RBAC, OSDU Entitlements membership, live
Stack configuration, and repository Actions settings. A mismatch fails later
inside CI, after the operator has lost the sequence that created it.

The service repository owns build and acceptance behavior in
`.spi/service.yaml`; the environment owns cluster coordinates and deployed
resource names. Onboarding must preserve that boundary. It may read the
descriptor fields needed to select the service and no-data identity, but the
descriptor validator in the service workflow remains authoritative.

A service Deployment must already exist in the Stack. The command grants a
repository access to that workload; it does not add a HelmRelease, Kustomization,
template, or service manifest.

## Decision

`spi onboard` is the cluster-side onboarding command. The descriptor-aware path
is:

```bash
uv run spi onboard --repo yuchen-osdu/partition --env auto2 --verify
```

The command reads `.spi/service.yaml` from the target repository's `main`
branch through the GitHub contents API raw media type. It requires a positive
integer `schemaVersion`, a non-empty `service.name`, and a string
`tests.acceptance.noDataAccessTokenEnv` when that field is present.
`--verify` requires schema version 2. Explicit flags override descriptor or
environment discovery.

`--env auto2` supplies `spi-stack-auto2` as the default AKS cluster, AKS
resource group, and identity resource group. The live Deployment supplies the
Deployment and container names. The `osdu-flux/spi-ingress-config` ConfigMap
supplies `GATEWAY_URL`; `osdu/osdu-config` supplies `KEYVAULT_NAME` and
`STORAGE_ACCOUNT_NAME`; `--partition` supplies `DATA_PARTITION_ID`; the
Entitlements seed Job supplies `ENTITLEMENT_DOMAIN`.

The command creates or reuses the service UAMI, reconciles the repository's
federated credentials, assigns the namespace-scoped deploy role and cluster
reader role, grants Flux read through native Kubernetes RBAC, and seeds the
service identity into the root Entitlements groups. A descriptor that names
`noDataAccessTokenEnv` receives the shared no-data identity without Azure data
RBAC or Entitlements membership. A descriptor that omits the field causes that
repository's no-data federated credentials and variables to be removed.

Only environment-owned Actions settings are written. Azure client, tenant, and
subscription IDs remain Actions secrets; the client ID is also paired with the
non-secret identity variable. Deployment coordinates, gateway, Key Vault,
Storage account, partition, Entitlements domain, and no-data identity facts are
Actions variables. `DEPLOY_VALIDATED` is reset to `false` before verification,
including re-home.

Verification is opt-in and leaves Flux frozen for CI mode. It freezes the Stack
GitRepository, every Kustomization, and every HelmRelease, dispatches Validation
on `main` with `force_full_pipeline=true`, identifies the new run by the prior
run set, commit SHA, event, branch, and dispatch time, then waits with bounded
polling. A successful Validation sets `DEPLOY_VALIDATED=true` before Settings
Apply is dispatched and awaited. A failure in either workflow leaves
`DEPLOY_VALIDATED=false` and reports the workflow URL.

Key Vault access remains conditional on a discovered or explicit vault. The
command neither requires nor populates acceptance-test secret values.
`--dry-run` performs reads and prints the plan without changing Azure,
Kubernetes, GitHub, or the local kubeconfig.

- **Rejected: documented manual steps.** They expose each cross-system value for
  inspection, but do not reconcile drift or prevent a half-rehomed repository.
- **Rejected: a separate bootstrap tool.** It isolates onboarding code, but
  duplicates Stack naming, cluster access, and Flux freeze behavior.

## Consequences

- Onboarding and re-home use the same idempotent command; the cost is an
  operation that spans GitHub, Azure, and Kubernetes failure domains.
- Descriptor behavior stays repository-owned and environment values stay
  Stack-owned; the cost is that verification cannot run until descriptor schema
  version 2 and the service Deployment are both present.
- The first canary can run without a second operator command; the cost is a
  frozen cluster that requires an explicit `spi reconcile --resume` after CI
  mode is no longer needed.
- Key Vault RBAC is established without creating test data; acceptance-test
  secret values remain outside Stack onboarding.
