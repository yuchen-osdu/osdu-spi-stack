# ADR-016: Istio JWT Projection for Azure-Provider OSDU Services

## Context

Azure-provider OSDU services read `x-app-id` and `x-user-id` headers in their
Spring authorization chain. A valid bearer token without those headers reaches
the application but fails before business logic. The gateway must validate the
token and project identity without making the Entitlements decision itself.

## Decision

`src/spi/templates.py::istio_auth_resources()` renders three resources that the
CLI applies during Kubernetes bootstrap:

- `RequestAuthentication/spi-osdu-jwt-authn` accepts Entra v1 and v2 issuers,
  validates the configured audiences, and exposes the JWT payload to Envoy.
- `EnvoyFilter/spi-osdu-identity-filter` runs on sidecar inbound traffic,
  removes caller-supplied identity headers, and projects the token's `appid` or
  `azp` as `x-app-id`. Issuer-specific claim precedence supplies `x-user-id`.
- `PeerAuthentication/spi-osdu-mtls` keeps namespace mTLS permissive for
  bootstrap Jobs that call the services directly.

The filter projects each caller as itself. It does not collapse
management-audience tokens to the OSDU UAMI and does not authorize group
membership. ADR-031 assigns that decision to Entitlements and explicit
membership seeding.

The `osdu` namespace receives its `istio.io/rev` value from
`osdu-flux/spi-cluster-config`; ADR-030 defines that live revision contract.

Rejected: `AuthorizationPolicy` alone enforces mesh claims, but does not populate the Azure provider's identity headers.

Rejected: mapping management-audience tokens to the workload UAMI preserves bootstrap, but grants callers the owner's identity.

## Consequences

- Bootstrap, service-to-service, CI, and external callers use one projection
  path, but every accepted audience must remain in `RequestAuthentication`.
- Entitlements can distinguish callers and negative-authorization tests can
  use a valid token without membership.
- Missing sidecars, JWKS failures, or audience drift surface as empty
  `x-app-id` or `x-user-id` values rather than a provider-specific diagnostic.
