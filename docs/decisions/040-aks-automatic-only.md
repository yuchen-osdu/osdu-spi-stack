---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-08-20"
deciders: "Yuchen Wang"
---

# AKS Automatic as the only cluster topology

## Context and Problem Statement

AKS Automatic on Kubernetes 1.36 supports the mutating webhooks required by
the middleware operators (ADR-019). The temporary Base plus Node
Autoprovisioning topology and the later selectable-mode design added a second
cluster implementation, CLI state, and validation matrix without adding a
distinct software capability.

## Decision Outcome

SPI Stack deploys AKS Automatic only. `infra/aks.bicep` is the sole cluster
entrypoint and pins Kubernetes 1.36 with managed Istio `asm-1-30`, BYO private
networking, hosted managed-system pools, and Automatic node provisioning.
Workloads select the shared `spi-pool` labels.

Remove the Base Bicep entrypoint, `--aks-mode`, the `spi-aks-mode` resource
group tag, topology inference, and in-place mode validation. Existing
non-Automatic clusters are rejected; changing topology requires recreating the
environment.

This decision supersedes ADR-033 and its Base fallback, and reaffirms ADR-002
with the Kubernetes 1.36 constraint from ADR-019.

### Consequences

- One cluster implementation and one release validation matrix remain.
- The CLI cannot silently retain or create a Base cluster.
- Historical Base support remains recoverable from Git history, not from the
  product surface.
- Regions used by the stack must support AKS Automatic 1.36 and the required
  system-pool availability zones.
