# ADR-002: AKS Automatic as the Compute Substrate

## Context

The stack requires managed Istio, automatic node provisioning, workload
identity, and admission controls while retaining webhook-based operators for
cert-manager, ECK, and CloudNativePG. AKS Automatic provides that platform
surface, but Kubernetes releases before 1.36 reject the operators'
`MutatingWebhookConfiguration` resources.

## Decision

Deploy only AKS Automatic through `infra/aks.bicep`. The template uses
`sku.name: Automatic`, Kubernetes 1.36, managed Istio `asm-1-30`, hosted
managed-system pools, BYO networking, and automatic node provisioning.
`src/spi/azure_infra.py` rejects an existing non-Automatic cluster; changing
topology requires recreating the environment.

Kubernetes 1.36 narrows the Automatic admission restriction to webhook
configurations that target `nodes`, `persistentvolumes`,
`certificatesigningrequests`, or `tokenreviews`. The stack's operator webhooks
do not target those resources. The Kubernetes minimum and Istio revision move
together because the mesh revision must support the selected Kubernetes minor.

Rejected: AKS Base exposes more cluster controls, but adds a second Bicep entrypoint, CLI mode state, and release matrix.

Rejected: AKS Automatic below 1.36 expands regional availability, but prevents the required middleware operators from reconciling.

## Consequences

- Regions without AKS Automatic 1.36 and `asm-1-30` cannot host the stack.
- One cluster implementation reduces drift, but removes a Base-mode fallback.
- The CLI enables Istio CNI after cluster creation because the resource
  provider rejects `proxyRedirectionMechanism` in the create request.
- Workloads must satisfy AKS Deployment Safeguards and use the platform's
  managed Istio, Workload Identity, and node provisioning contracts.
