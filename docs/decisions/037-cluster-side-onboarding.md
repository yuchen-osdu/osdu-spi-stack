# ADR-037: Cluster-Side Onboarding for Service-Fork CI/CD

## Context

The deploy lane (ADR-029, ADR-030) lets a service fork build an image and set it
on a running Deployment. Reaching the cluster couples Azure identity, GitHub
OIDC subjects, Azure and Kubernetes RBAC, OSDU Entitlements membership, live
Stack configuration, and repository Actions settings. A mismatch fails later
inside CI, after the operator has lost the sequence that created it.

The service repository owns build and acceptance behavior in
`.spi/service.yaml`; the environment owns cluster coordinates and deployed
resource names. Onboarding must preserve that boundary and must reject a
descriptor before changing either environment. The template's descriptor
parser and closed schema remain the single validation implementation.

A service Deployment must already exist in the Stack. The command grants a
repository access to that workload; it does not add a HelmRelease, Kustomization,
template, or service manifest.

## Decision

`spi onboard` is the cluster-side onboarding command. The descriptor-aware path
is:

```bash
uv run spi onboard --repo yuchen-osdu/partition --env auto2 --verify
```

The command resolves the target repository's exact `main` SHA and reads
`.spi/service.yaml` at that revision through the GitHub contents API raw media
type. It resolves the exact `main` SHA of `TEMPLATE_REPO_URL`, downloads
`.github/scripts/service-config/descriptor.py` and `schema.json` at that
revision, imports the parser dynamically, and runs canonical parse and
validation before mutation. Descriptor-aware onboarding requires schema version
2. It extracts `service.name`,
`tests.acceptance.noDataAccessTokenEnv`, and whether
`tests.acceptance.keyVaultBindings` is non-empty. Explicit flags override the
extracted service or environment discovery, not canonical validation.

`--env auto2` supplies `spi-stack-auto2` as the default AKS cluster, AKS
resource group, and identity resource group. The live Deployment supplies the
Deployment and container names. The `osdu-flux/spi-ingress-config` ConfigMap
supplies `GATEWAY_URL`; `osdu/osdu-config` supplies `STORAGE_ACCOUNT_NAME` and,
only when the descriptor has Key Vault bindings, `KEYVAULT_NAME`; `--partition`
supplies `DATA_PARTITION_ID`; the Entitlements seed Job supplies
`ENTITLEMENT_DOMAIN`.

The command creates or reuses the service UAMI, reconciles the repository's
federated credentials, assigns the namespace-scoped deploy role and cluster
reader role, grants Flux read through native Kubernetes RBAC, and seeds the
service identity into the root Entitlements groups. A descriptor that names
`noDataAccessTokenEnv` receives the shared no-data identity without Azure data
RBAC or Entitlements membership. A descriptor that omits the field causes that
repository's no-data federated credentials and variables to be removed.

Only environment-owned Actions settings are written. Azure client, tenant, and
subscription IDs remain Actions secrets; the client ID is also paired with the
non-secret identity variable. Deployment coordinates, gateway, Storage account,
partition, Entitlements domain, and no-data identity facts are Actions
variables. `KEYVAULT_NAME` and the Key Vault Secrets User role exist only for a
non-empty `keyVaultBindings` contract. Removing that contract removes the
variable and removes the exact prior service-identity role assignment when the
vault and assignment remain identifiable.

Verification is opt-in and leaves Flux frozen for CI mode. It freezes the Stack
GitRepository, every Kustomization, and every HelmRelease, dispatches Validation
on `main` with `force_full_pipeline=true`, and accepts only a run for the
resolved SHA. The selector uses a captured run URL when the CLI returns one;
otherwise it uses the prior run set, commit SHA, event, branch, and dispatch
time, and rejects multiple matches. Validation succeeds only when the workflow
and `🔒 Deploy, Test & Restore`, `🚀 Deploy to spi-stack`, and
`🧪 Integration Tests` complete successfully.

`DEPLOY_VALIDATED` remains false through Validation and the first Settings Apply
pass. The command sets it true only after both succeed, then dispatches Settings
Apply a second time so the required checks become active. A moved `main`
branch, failed job, failed Settings Apply pass, or timeout restores false.
Timed-out runs are cancelled and awaited to a terminal state. A material
environment change, re-home, or requested verification resets false before the
first repository mutation; a no-op `--no-verify` rerun preserves true.

The command neither requires nor populates acceptance-test secret values.
`--dry-run` does not inspect the active kubectl context. It reports the live
Deployment and ConfigMap values as unresolved placeholders and prints the plan
without changing Azure, Kubernetes, GitHub, or the local kubeconfig.

- **Rejected: documented manual steps.** They expose each cross-system value for
  inspection, but do not reconcile drift or prevent a half-rehomed repository.
- **Rejected: a separate bootstrap tool.** It isolates onboarding code, but
  duplicates Stack naming, cluster access, and Flux freeze behavior.

## Consequences

- Onboarding and re-home use the same idempotent command; the cost is an
  operation that spans GitHub, Azure, and Kubernetes failure domains.
- Descriptor behavior stays repository-owned and environment values stay
  Stack-owned; the cost is that onboarding cannot run until descriptor schema
  version 2 and the service Deployment are both present.
- The first canary can run without a second operator command; the cost is a
  frozen cluster that requires an explicit `spi reconcile --resume` after CI
  mode is no longer needed.
- Key Vault RBAC is established without creating test data; acceptance-test
  secret values remain outside Stack onboarding.
