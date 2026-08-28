# ADR-037: Two Azure Data-Plane RBAC Resource Paths

## Context

ADR-023 disables local authentication on Cosmos DB and Service Bus, while the
Storage modules disable shared-key access. The workload UAMI therefore
requires Entra-backed data permissions. Azure exposes those permissions through
two resource models: Cosmos-native role assignments and standard Azure RBAC
assignments. Treating both as
`Microsoft.Authorization/roleAssignments` leaves Cosmos data access
unprovisioned.

## Decision

Provision both RBAC resource paths for `<cluster>-osdu-identity`.

- `infra/modules/cosmos-gremlin.bicep` creates
  `gremlinRoleAssignments` with Gremlin Data Contributor role ID
  `00000000-0000-0000-0000-000000000004`.
- `infra/modules/partition.bicep` creates `sqlRoleAssignments` with Cosmos DB
  Built-in Data Contributor role ID
  `00000000-0000-0000-0000-000000000002`.
- `infra/modules/rbac.bicep` creates standard Azure role assignments for Key
  Vault, Storage, Service Bus Data Sender, Service Bus Data Receiver, and
  AcrPull.

These are two provisioning and diagnostic paths for one Entra-only
authentication model. No usable Cosmos key, Storage key, or Service Bus SAS
fallback is retained.

Rejected: standard Azure RBAC provides one mechanism, but cannot grant Cosmos SQL or Gremlin data permissions.

Rejected: local keys avoid Cosmos-native role resources, but restore stored credentials and conflict with ADR-023.

## Consequences

- `az role assignment list` shows the standard assignments but not the Cosmos
  native grants.
- Cosmos access diagnostics must inspect `sqlRoleAssignments` or
  `gremlinRoleAssignments`; this split adds operational work but matches the
  service APIs.
- Role propagation is asynchronous, so workload startup can precede effective
  access and require a restart after the grant becomes active.
