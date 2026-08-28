# ADR-005: Workload Identity for Azure PaaS Access

## Context

OSDU services access Cosmos DB, Service Bus, Storage, and Key Vault. Stored
connection strings and service-principal secrets add rotation work and conflict
with the local-auth-disabled resources defined by ADR-023. AKS exposes an OIDC
issuer that can federate Kubernetes ServiceAccounts with Entra identities.

## Decision

Use one UAMI named `<cluster>-osdu-identity` for the OSDU workload fleet.
`infra/modules/identity.bicep` creates federated credentials for
`workload-identity-sa` across the fixed OSDU namespace set. Service pods carry
the `azure.workload.identity/use: "true"` label and receive the client ID,
tenant ID, and federated token file through the AKS workload identity webhook.

`infra/modules/rbac.bicep` grants the UAMI Key Vault Secrets User, Storage Blob
Data Contributor on common and partition storage, Storage Table Data
Contributor on common storage, and Service Bus Data Sender and Data Receiver
on each partition namespace. Cosmos SQL and Gremlin grants use their native
data-plane role-assignment resources as described by ADR-037. The module also
grants AcrPull to both the UAMI and the AKS kubelet identity; pod image pulls
use the kubelet identity.

The `dns` ingress profile creates a separate `<cluster>-external-dns` UAMI with
DNS Zone Contributor on the selected zone.

Rejected: per-service UAMIs reduce the Azure-side blast radius, but multiply federation and RBAC for shared PaaS resources.

## Consequences

- PaaS access uses short-lived Entra tokens and stores no usable account keys
  or Service Bus SAS strings.
- The shared identity keeps provisioning bounded, but all OSDU services receive
  the same Azure data-access envelope.
- Service Bus clients must enable both MSI and Workload Identity code paths;
  `${partition}-sb-connection` remains a schema-compatible `DISABLED` value.
- The GHCR baseline includes a Workload-Identity-capable indexer-queue image;
  the community image source works only with images that support the same token
  path.
