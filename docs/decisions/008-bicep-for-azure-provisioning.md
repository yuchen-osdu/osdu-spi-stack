# ADR-008: Bicep for Azure Provisioning

## Context

The stack provisions an AKS cluster and a resource graph spanning identity,
networking, Key Vault, ACR, Cosmos DB, Service Bus, Storage, Flux, and scoped
role assignments. Declarative ARM dependencies provide idempotency and
parallelism without an external state file; several client-side operations
remain outside the ARM resource model.

## Decision

Declare Azure resources in three Bicep entrypoints:

- `infra/aks.bicep` uses raw `Microsoft.ContainerService/managedClusters`
  Bicep because the required `hostedSystemProfile` surface is absent from the
  pinned AVM module.
- `infra/main.bicep` composes the hand-written modules under `infra/modules/`
  for PaaS, identities, and role assignments.
- `infra/flux.bicep` creates the AKS Flux extension and its
  `fluxConfigurations` resource.

The CLI handles only operations that depend on client state or live queries:
resource-group creation, soft-deleted Key Vault recovery, kubeconfig merge,
Istio CNI enablement, Kubernetes bootstrap, and runtime Key Vault secrets
derived from the generated middleware seed. `spi up --dry-run` runs ARM
what-if against `infra/aks.bicep` and `infra/main.bicep`.

Rejected: Terraform provides a mature plan workflow, but adds a state store and a second lifecycle model.

Rejected: a pure `az` orchestrator keeps logic in Python, but must reimplement ARM ordering, idempotency, and what-if.

Rejected: full AVM adoption standardizes interfaces, but omits the required AKS network surface and adds module versioning.

## Consequences

- ARM deployment operations become the primary diagnostic record, while the
  CLI remains responsible for the explicit imperative seams.
- AKS API versions and raw resource shapes require review when Azure changes
  the Automatic contract.
- Adding a PaaS resource changes Bicep wiring rather than the CLI orchestration
  sequence.
