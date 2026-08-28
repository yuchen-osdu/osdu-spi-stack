# ADR-028: Descriptor-Aware Cluster-Side Onboarding

## Context

A service repository needs GitHub federation, Azure and Kubernetes RBAC,
Entitlements membership, live Stack coordinates, and Actions settings before
its deploy lane can reach an environment. The repository owns build and test
behavior in `.spi/service.yaml`; the Stack owns environment values. A service
Deployment and a schema version 2 descriptor must exist before access can be
granted.

## Decision

Use `spi onboard` to reconcile the environment-owned side of that boundary.
The command resolves the target repository's exact `main` SHA, reads
`.spi/service.yaml`, then downloads the canonical `descriptor.py` and
`schema.json` from the exact `main` SHA of `TEMPLATE_REPO_URL`. Canonical parse
and schema validation complete before mutation.

The command discovers the live Deployment, gateway, Storage account, and
Entitlements domain, while `--partition` supplies the target partition. It then
creates or reuses the service UAMI, reconciles GitHub OIDC subjects, assigns
the namespace deploy and read roles, seeds Entitlements membership, and writes
environment-owned repository settings.
Key Vault settings and RBAC exist only when the descriptor declares
`keyVaultBindings`; the negative-access identity exists only when the
descriptor declares `noDataAccessTokenEnv`.

`--verify` freezes Flux, dispatches Validation for the resolved SHA, requires
`🔒 Deploy, Test & Restore`, `🚀 Deploy to spi-stack`, and
`🧪 Integration Tests` to succeed, and runs Settings Apply before and after
setting `DEPLOY_VALIDATED=true`. A failure restores the variable to `false`.
`--dry-run` prints unresolved live values without reading the active Kubernetes
context or mutating GitHub, Azure, or Kubernetes.

Rejected: manual steps expose each value, but do not reconcile drift or prevent a partial re-home.

Rejected: a separate tool isolates GitHub administration, but duplicates Stack discovery and Flux suspension.

## Consequences

- Re-running the command reconciles access and can re-home a repository, but
  the operation spans GitHub, Azure, Kubernetes, and OSDU failure domains.
- Onboarding does not create the service Deployment, modify Stack manifests, or
  populate acceptance-test secret values.
- Verification is optional and leaves the cluster suspended after success.
- Descriptor changes control optional access, so removing a Key Vault or
  negative-token contract also removes the corresponding environment settings.
