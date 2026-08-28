# ADR-033: Subscription-Resolved System-Pool Availability Zones

## Context

Availability zones for `Standard_D4lds_v5` vary by subscription as well as
region. Passing a restricted zone fails AKS provisioning, while AKS Automatic
rejects a reduced set when the region publishes more zones for the SKU. A
region-wide static value cannot represent both constraints.

## Decision

Resolve the system-pool zones before the AKS Bicep deployment.
`src/spi/azure_infra.py::_resolve_system_pool_zones()` runs
`az vm list-skus` for `Standard_D4lds_v5`, collects the published zones, removes
subscription restrictions, and passes the result to
`infra/aks.bicep::availabilityZones`.

The preflight fails when the SKU has no published zone, no usable zone, or any
restricted published zone. The last case directs the deployment to another
region because Automatic requires the full published set. The CLI exposes no
manual zone override.

Rejected: fixed Bicep zones avoid SKU discovery, but fail when the subscription restricts a listed zone.

Rejected: a regional table avoids a live query, but cannot encode subscription restrictions or capacity changes.

## Consequences

- AKS deployment adds one subscription-scoped SKU query before resource
  creation.
- Zone restrictions fail with the SKU and region named before ARM starts the
  cluster deployment.
- A subscription with a partial zone restriction must use another region; the
  CLI does not force a reduced Automatic topology.
