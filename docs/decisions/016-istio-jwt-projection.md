# ADR-016: Istio JWT Projection for Azure-Provider OSDU Services

## Context

The Azure-provider OSDU service images ship an in-process Spring filter chain that reads the caller's application identity from a request header, not from the bearer token; the chain cannot be disabled by configuration. The header is expected to be populated by the Istio sidecar before the request reaches the Java application. With no Istio policy performing that projection, requests carrying a valid bearer fail with 401/403 and `app-id=` empty in the service request log, before any business logic runs. Choosing the Azure provider (ADR-001) therefore implies a runtime contract: something in the request path must extract the JWT payload and surface it as a header the service understands.

## Decision

Three Istio resources satisfy the contract, applied imperatively from the CLI in the same Phase 4 step that writes `osdu-config`; the CLI already holds the tenant id and the OSDU UAMI client id, which keeps the substitution local:

- `RequestAuthentication` accepting the AAD v1 and v2 issuers and audiences `{client_id}` and `https://management.azure.com[/]`, with `outputPayloadToHeader: x-payload` and `forwardOriginalToken: true`.
- `EnvoyFilter` `spi-osdu-identity-filter` in `osdu`, on `SIDECAR_INBOUND`: its Lua reads `jwt_authn` dynamic metadata and writes `x-app-id` / `x-user-id`; the `aud == https://management.azure.com/` branch substitutes the OSDU UAMI client id, matching the audience Workload Identity tokens present.
- `PeerAuthentication` `spi-osdu-mtls` mode `PERMISSIVE` in `osdu`, defensive against managed-mesh defaults that could break the init Jobs.

Sidecar injection is a prerequisite, so the `osdu` namespace `istio.io/rev` label is not pinned in Git; it is sourced from the live cluster revision via the `osdu-flux/spi-cluster-config` ConfigMap and Flux substitution.

The audience list must include every value services use to mint tokens. Bootstrap Jobs use `aud=https://management.azure.com/`, but `core-lib-azure` mints service-to-service calls with scope `${aadClientId}/.default`; if `AAD_CLIENT_ID` is overridden to a separate app registration, that appid must also be in the list, otherwise `jwt_authn` skips validation, the Lua exits early, and downstream services return 403 with `app-id=` empty. `istio_auth_resources()` accepts both `entra_client_id` and `aad_client_id` and emits both, deduped when they match.

Rejected: `RequestAuthentication` plus `AuthorizationPolicy` keyed on JWT claims. Works for images whose Spring chain reads `RequestPrincipal` directly; the Azure provider's does not.

Rejected: per-service default-deny `AuthorizationPolicy` as defense in depth. The Spring chain already enforces identity, and default-deny on services serving traffic carries a wider blast radius; it remains available as a later hardening pass.

Rejected: pre-populating entitlements without going through the service API. Bypasses the auth chain but ties bootstrap to schema internals of the entitlements implementation, the burden ADR-013 and ADR-015 removed.

## Consequences

- The CLI-applied resources are present before any caller is expected to authenticate, so the bootstrap Jobs and ongoing service-to-service traffic both see populated `x-app-id` headers.
- A new dependency on Istio Envoy Lua sits between deployment and authorization. Failures in JWKS reachability, RA configuration drift, or sidecar version skew now manifest as `app-id=` empty rather than as a clear-cut auth error. The runbook should call out checking the EnvoyFilter and RequestAuthentication first when bootstrap Jobs return 401/403.
- Tying the EnvoyFilter to the Workload Identity audience (`https://management.azure.com/`) means future identity changes (different audience, switch to a managed identity with different claims, etc.) require revisiting the Lua. The mapping is small and contained, but it is a coupling that did not previously exist.
- The audience list must include every value services use to mint service-to-service tokens. Bootstrap Jobs use `aud=https://management.azure.com/`, but `core-lib-azure`'s `getWIToken` mints subsequent service-to-service calls with scope `${aadClientId}/.default`. If `AAD_CLIENT_ID` is overridden to a separate OSDU AAD app registration, that appid must also be in the RA audience list; otherwise `jwt_authn` skips validation, the Lua exits early, the Spring filter sees an empty `x-app-id`, and downstream services return 403 with an empty `app-id=` in the request log. `istio_auth_resources()` accepts both `entra_client_id` (UAMI) and `aad_client_id` and emits both, deduped when they match.

Rejected alternatives:

- **Solving with `AuthorizationPolicy` alone.** Works for service images whose Spring chain reads `RequestPrincipal` directly. Our images do not, so the in-process filter still rejects after Istio admits the request.
- **Switching to a different OSDU provider.** Out of scope and inconsistent with the SPI Stack's stated commitment to the Azure provider (ADR-001).
- **Imperative side-channel that pre-populates entitlements without going through the service API.** Bypasses the auth chain entirely but ties bootstrap to schema details internal to the entitlements implementation, and re-creates the maintenance burden ADR-013 and ADR-015 reduced.

## Revision: per-identity projection

The original Lua collapsed every `aud=https://management.azure.com/` app token to the OSDU UAMI client id (`entra_client_id`). That is correct for the bootstrap Jobs it was written for (they run as the UAMI), but it has an unintended side effect: any other identity that mints a management-audience token, notably a CI identity onboarded for the deploy lane, is also silently projected to the UAMI and inherits the bootstrap admin's authorization. Integration tests then pass without the test identity being a real entitlements member, so they do not actually exercise authorization.

The Lua is now a pure identity extractor: the `aud=management.azure.com` special-case (and the `entra_client_id` constant inside the Lua) are removed. It projects the caller's own application id, `appid` for v1 app/MSI tokens or `azp` for v2, as `x-app-id`, and extracts `x-user-id` from the token via the standard issuer-based path (`processAADV1`/`processAADV2`). It makes no authorization decision and is unaware of entitlements. Because the OSDU UAMI's own appid is the bootstrap-admin service account, the bootstrap Jobs are unaffected, while every other identity now flows as itself and must be an explicit entitlements member.

Concretely the responsibilities split three ways: the Lua (deploy / `spi up`) only projects identity; entitlements makes the access decision against the projected `x-user-id`; and initialization/onboarding use the public Entitlements AddMember API for identities that should receive access. `spi up` passes the creator's projected identifier plus its user object-ID alias to `osdu-spi-init`, while `spi onboard` adds each onboarded CI identity. Neither path changes the Lua.

The fresh Stack creator and newly onboarded CI identities use the same four root groups ADME data-seeding uses (`users`, `users.datalake.ops`, `users.datalake.admins`, `users.data.root`). The creator is seeded by the per-partition Entitlements init Job immediately after tenant provisioning; each CI identity is seeded by a short onboarding Job. Both run under the OSDU workload identity, the tenant-provisioning OWNER authorized to call AddMember. This stays consistent with the rejection above: both use the public AddMember API, not a Gremlin side-channel.

The shared `spi-ci-no-data-access` integration-test identity is the explicit
exception: onboarding creates and federates it but never passes it to the
entitlements seed Job. Its valid token must therefore authenticate through the
same Lua path while remaining unauthorized by entitlements.

Note on partition: `partition-azure` is internal. Its `isDomainAdminServiceAccount` check authorizes any AAD-issued service-principal token regardless of group membership, so the partition deploy-lane test passes with or without this projection change or the seed. The per-identity model is only observable on user-facing services (storage, legal, search).
