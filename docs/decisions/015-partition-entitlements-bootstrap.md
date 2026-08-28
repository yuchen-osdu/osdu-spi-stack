# ADR-015: Partition and Entitlements Bootstrap through Flux Jobs

## Context

A fresh partition has no Partition service record or Entitlements root groups.
Services cannot resolve partition configuration without the record, and callers
cannot pass authorization before the groups exist. The initialization must run
after core services become Ready and before schema loading starts.

## Decision

Deploy `software/charts/osdu-spi-init/` through the `spi-osdu-init`
Kustomization at Layer 5a. The chart renders two Jobs per partition:
`partition-init-{partition}` creates the Partition record, then
`entitlements-init-{partition}` calls tenant provisioning and seeds the Stack
creator into `users`, `users.datalake.ops`, `users.datalake.admins`, and
`users.data.root`.

Both Jobs use `workload-identity-sa` and public service APIs.
`spi-init-values` supplies the partition list and creator identifiers.
Partition values name bare Key Vault suffixes because `partition-azure`
prefixes sensitive values with the partition ID. Helm hook ordering and
`before-hook-creation` rerun the Jobs when the chart or values change; accepted
conflict responses make reruns idempotent.

`spi-osdu-schema-load` depends on `spi-osdu-init`. Creator seeding is enabled
by default, disabled by `--no-seed-creator`, and overridden by
`--creator-user-id`. ADR-031 applies the same explicit membership model to
onboarded CI identities.

Rejected: an imperative CLI bootstrap has direct failure reporting, but is absent from the Flux graph and cluster state.

Rejected: direct Entitlements graph writes avoid service startup dependencies, but couple bootstrap to provider internals.

## Consequences

- Schema loading starts only after the partition and root groups exist.
- Multi-partition deployments render two additional Jobs per partition.
- Creator access is automatic unless disabled, while additional identities
  still require explicit onboarding or Entitlements administration.
- Initialization depends on the core service APIs and can hold Layer 5b when
  either service is unhealthy.
