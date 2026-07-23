# ADR-015: Partition + Entitlements Bootstrap via a Flux Helm Chart

**Status**: Accepted

## Context

OSDU's Azure provider needs two pieces of state in place before any record, schema, or entitlements operation can succeed against a fresh cluster:

1. **A partition record.** Every service resolves partition-specific backend config (Cosmos endpoint, Service Bus namespace, storage account, elastic credentials) by calling `partition-service`. Without a record, those lookups return nothing.
2. **Entitlements root groups.** Authorization calls look up the caller's appid in `users.data.root@{partition}...` and siblings. With an empty entitlements DB every call returns 401, regardless of which identity the caller holds.

Both pieces are mechanical, one-shot operations that have to happen exactly once per partition per environment. The same structural problem ADR-013 solved for schema-load applies here, scaled to an additional admin concern: per-partition identity provisioning.

## Decision

Bootstrap partitions and entitlements with a Flux-managed Helm chart that renders two one-shot Jobs per partition:

- `partition-init-{partition}` POSTs the partition record to `partition-service`.
- `entitlements-init-{partition}` POSTs `tenant-provisioning` so entitlements creates root groups with the caller's appid as OWNER, then adds the Stack creator's projected identity to the four root groups.

Shape:

- Chart `software/charts/osdu-spi-init/` with `partition-record.yaml`, `scripts.yaml`, `partition-init.yaml`, `entitlements-init.yaml` templates. The two Job templates both loop over `.Values.partitions` so a multi-partition deploy renders one Job per partition with no extra manifests.
- Both Jobs share the `workload-identity-sa` ServiceAccount (ADR-005) and the Token.py MSAL pattern already proven in `schema-load/script.yaml`. The caller's appid is the OSDU UAMI client id, which is what `partition-azure` stores as `TenantInfo.service-account` and what entitlements-azure authorizes as bootstrap admin on an empty DB.
- Partition list and creator identifiers are injected via a `spi-init-values` ConfigMap the CLI writes. The CLI resolves the creator from the current Azure access token using the same v1/v2 claim precedence as the Istio JWT filter and includes the user object ID as an alias when token versions project different values. The HelmRelease consumes the values with `valuesFrom`, so adding a partition or seeding its creator is initialization state, not a Git edit.
- Partition record values use **bare** Key Vault secret suffixes (`cosmos-endpoint`, `sb-namespace`, etc.), not partition-prefixed names. `partition-azure` auto-prefixes the partition id onto every `sensitive: true` value at write time, so embedding the prefix in the chart double-prefixes the stored value and breaks downstream service lookups.
- New Kustomization `spi-osdu-init` at `software/stacks/osdu/init/`, wired into the core profile as Layer 5a (after `spi-osdu-services`, before `spi-osdu-schema-load`) per ADR-007. `schema-load` depends on `spi-osdu-init` so the schema POSTs see a tenant that is already provisioned.
- Idempotence: 201 and 409 from partition-service count as success; 200 and 409 from entitlements-tenant-provisioning count as success. No `ttlSecondsAfterFinished` (same rationale as ADR-013 — Flux would re-create an auto-deleted Job and turn one-shot into periodic).
- Creator access is enabled by default. `--no-seed-creator` disables it for workload-only automation, and `--creator-user-id` supplies an explicit projected identity when Azure CLI token discovery is unsuitable.
- The Git-backed Helm chart reconciles by source revision. Both init Jobs are ordered Helm post-install/post-upgrade hooks with `before-hook-creation`. Changing chart code or creator values therefore deletes the prior completed Jobs and reruns partition before Entitlements without patching immutable Job pod templates.

Rejected:

- **Imperative CLI step.** Re-opens the problems ADR-011 and ADR-013 closed: hidden CLI dependency, no re-run from the cluster, invisible to `flux get kustomizations`.
- **Per-partition Flux Kustomization stamping.** One Kustomization per partition would duplicate every wiring decision in this ADR. A single Kustomization that contains a chart with a per-partition loop is the same outcome with less YAML.
- **Partition-prefixed values in the chart's `partition.json`.** Double-prefixes every stored value and surfaces as "Invalid data partition id" the first time a service dereferences the record. The bare-key pattern matches what `partition-azure` expects.

## Consequences

- Fresh deploy reaches a schema-loaded cluster whose creator can call user-facing OSDU APIs without a manual Entitlements post-step.
- Multi-partition enables via `spi up --env dev1 --partition p1 --partition p2`; the chart renders one partition-init + one entitlements-init Job per partition with no manifest changes.
- Four per-partition Key Vault secrets are declared in `partition.bicep` (`{p}-storage-account-blob-endpoint`, and `"DISABLED"` placeholders for `{p}-cosmos-connection`, `{p}-sb-connection`, `{p}-storage-account-key`) so the partition record resolves under Workload Identity without exposing real connection strings. Service Bus local auth is disabled; indexer-queue must use a Workload Identity-aware Service Bus client path.
- Manual re-run is `kubectl delete job -n osdu partition-init-{p} entitlements-init-{p}` followed by `flux reconcile kustomization spi-osdu-init`. 409 responses on re-run are treated as success.
- Additional human users remain an explicit administration task; only the identity that runs `spi up` is seeded automatically.
