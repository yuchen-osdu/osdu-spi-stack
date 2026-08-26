# ADR-041: spi-pool as the Workload Node-Placement Label

## Context

Karpenter NodePools and workload node selectors previously used the
`agentpool` label to steer middleware onto the `platform` pool and OSDU
services onto the `osdu` pool. AKS restricts `agentpool` as a
reserved system label: NodePool manifests that set it in
`spec.template.metadata.labels` are rejected at admission
(`label "agentpool" is restricted`), which blocked the entire nodepool
layer from reconciling.

## Decision

Use `spi-pool`, applied consistently to NodePool templates,
workload `nodeSelector`s, and affinity rules. A stack-owned label can
never collide with platform reservations. The AKS-managed label is set by
the platform, not by Karpenter templates, and taint-only placement loses
the ability to require (rather than merely tolerate) a pool.

Rejected: use `kubernetes.azure.com/agentpool`; Karpenter does not own it.

Rejected: use only taints and tolerations; toleration does not require a pool.

## Consequences

- Node placement is decoupled from AKS reserved-label policy.
- The rename is invisible outside the repository.
- Operators inspecting nodes must know `spi-pool` is the
  placement label (`kubectl get nodes -L spi-pool`).
