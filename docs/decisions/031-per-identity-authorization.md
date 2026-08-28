# ADR-031: Per-Identity Authorization with Explicit Membership

## Context

ADR-016 projects Entra token claims into the headers consumed by the Azure
provider. Mapping every management-audience token to the OSDU workload UAMI
would make CI and human callers inherit the bootstrap owner's authorization,
so positive tests could pass without caller membership and negative tests
could not prove denial.

## Decision

Project each caller's own identity. The Istio Lua filter uses `appid` for Entra
v1 application tokens and `azp` for v2 application tokens as `x-app-id`, then
uses issuer-specific claim precedence for `x-user-id`. The filter performs no
membership decision.

Grant access through the public Entitlements AddMember API. `spi up` supplies
the creator identifiers to the ADR-015 initialization Jobs unless
`--no-seed-creator` is set. `spi onboard` runs a short Job under the OSDU
workload identity to add each CI identity to `users`,
`users.datalake.ops`, `users.datalake.admins`, and `users.data.root`.
The shared no-data identity receives no membership.

Rejected: collapsing callers to the workload UAMI keeps bootstrap implicit, but erases the caller boundary.

Rejected: direct Entitlements graph writes avoid the service API, but couple onboarding to provider storage internals.

## Consequences

- Authorization outcomes identify the calling principal, but each permitted
  principal requires an explicit membership operation.
- Bootstrap and onboarding use the same public API boundary and accept
  existing-member conflicts as idempotent success.
- Human token versions can project different identifiers, so creator seeding
  may store more than one claim-derived alias.
