---
status: "accepted"
contact: "Yuchen Wang"
date: "2026-08-19"
deciders: "Yuchen Wang"
---

# ADR-026: Graduated profile starts with the Wellbore service pair

## Context

The core profile is the stable Azure SPI baseline. Domain services add
runtime dependencies, image requirements and security boundaries that should
not silently expand every core deployment. The first Python SPI pilot is the
Wellbore bulk worker, but the worker is not a public product boundary and does
not enforce Wellbore record ACLs.

## Decision

Add a cumulative `graduated` profile containing:

- the public `wellbore-domain-services` API;
- the internal `wellbore-domain-services-worker`;
- a public route for the main service only;
- NetworkPolicy and Istio AuthorizationPolicy around the worker;
- profile-aware atomic image resolution for the core 13 images plus the two
  Wellbore images.

The worker uses the same Workload Identity as the core services, resolves its
partition storage through Partition and Key Vault, and writes to the existing
`wdms-osdu` Blob container. Key Vault stores `aad-client-id`, which the fixed
Azure Python library converts to an OAuth `/.default` scope.

`core` remains the default. There is no automatic fallback from missing SPI
images to community images.

## Consequences

- Core deployments do not require or expose Wellbore.
- Graduated deployment fails before Azure provisioning if either Wellbore
  image is missing.
- Main DDMS remains the ACL and public API boundary.
- The worker can be tested in-cluster without becoming externally routable.
- Additional graduated DDMS families can be added later without changing the
  core contract.
