# ADR-030: Runtime-Resolved Managed Istio Revision

## Context

Sidecar injection requires the `osdu` namespace label `istio.io/rev` to match
the managed Istio revision installed on the cluster. A literal in a
Flux-managed Namespace can overwrite the correct bootstrap value after a mesh
revision change and leave new pods without sidecars.

## Decision

Publish the cluster's managed Istio revision through
`osdu-flux/spi-cluster-config`. `infra/aks.bicep` outputs its pinned revision;
`src/spi/bootstrap.py` writes it as `ISTIO_REVISION`; and
`software/components/namespaces/namespaces.yaml` references
`${ISTIO_REVISION}` through Flux substitution.

For an existing cluster, the CLI resolves the revision from the live
`istiod-<revision>` deployment and fails if it cannot identify one.
`spi reconcile` refreshes the ConfigMap before triggering any Kustomization, so
older deployments and mesh upgrades converge on the live value.

Rejected: a literal namespace label is self-contained, but Flux can reapply a revision the cluster no longer runs.

Rejected: default sidecar injection removes the variable, but does not bind pods to the AKS managed-mesh revision.

## Consequences

- The Namespace manifest requires `spi-cluster-config`; a missing value blocks
  `spi-namespaces` and every dependent layer.
- Mesh revision changes require no manifest edit, but admitted pods retain
  their existing sidecar until the workload restarts.
- Revision discovery becomes a deployment precondition instead of a silent
  loss of injection.
