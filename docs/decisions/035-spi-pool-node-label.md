# ADR-035: `spi-pool` as the Workload Placement Label

## Context

AKS reserves the `agentpool` node label. Karpenter rejects NodePool templates
that set it, so using that key for stack-authored labels blocks the NodePool
layer before workloads can schedule.

## Decision

Use the stack-owned `spi-pool` label on Karpenter NodePool templates, workload
`nodeSelector` fields, and affinity rules. Values distinguish the `platform`
and `osdu` pools. Taints and tolerations remain in place, but the selector
requires the intended pool rather than permitting any tolerated node.

Rejected: `agentpool` matches AKS terminology, but AKS reserves it and rejects stack-authored values.

Rejected: `kubernetes.azure.com/agentpool` identifies AKS pools, but Karpenter NodePool templates do not own it.

Rejected: taints and tolerations prevent accidental admission, but do not require the intended pool.

## Consequences

- NodePool admission no longer depends on an AKS-reserved label.
- Workload manifests and NodePool templates must change together when a pool
  name changes.
- Operators inspect placement with `kubectl get nodes -L spi-pool`, not the
  AKS `agentpool` label.
