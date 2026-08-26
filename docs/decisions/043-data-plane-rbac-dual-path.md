# ADR-043: Historical Dual-Path Data-Plane Access

## Context

This decision is superseded by
[ADR-023](023-entra-only-data-plane.md), which makes Workload Identity the
only Cosmos and Service Bus data-plane path. It remains recorded because the
dual-path alternative is operationally plausible and must not be
reintroduced accidentally.

Some Azure environments enforce policies that deny or disable local
(key/SAS) authentication on data services: Service Bus namespace creation
is rejected unless local auth is off and TLS >= 1.2, and Cosmos DB accounts
can have `disableLocalAuth` forced on by a `modify` policy at deploy time.
In such environments, key-based access silently stops working even though
the keys still exist in Key Vault. Other environments have no such
policies, and the community OSDU images there may still depend on key/SAS
paths (ADR-005 documents the Service Bus SAS carve-out).

## Decision

Do not restore dual-path access. Keep the Cosmos-native and Azure RBAC
assignments introduced by this work, but disable local authentication and
write only `DISABLED` compatibility placeholders as specified by ADR-023.

Rejected: keep usable keys or SAS as a compatibility path; that makes behavior
tenant-dependent and preserves credentials the current architecture does not
need.

## Consequences

- The supporting RBAC grants, partition blob container, kubelet AcrPull, and
  deployer principal type remain useful and are retained.
- Historical references to a configurable SAS path are non-current.
