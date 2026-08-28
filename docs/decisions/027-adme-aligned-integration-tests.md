# ADR-027: ADME-Aligned Integration Tests with Federated Identities

## Context

Service pull requests must exercise the Azure-provider acceptance modules
against a live stack. The stack stores no tester client secret, and custom
smoke tests do not match the service suites used by Azure Data Manager for
Energy.

## Decision

Run each service's ADME-aligned `testing/<service>-test-azure` module with its
test-core dependencies. The service repository owns the Maven goal, profile,
exclusions, token variable names, and timeout settings; `.spi/service.yaml`
declares whether the suite needs a negative-access token.

The deploy lane signs in through GitHub OIDC as the service CI UAMI and exports
an ARM-audience bearer as `INTEGRATION_TESTER_ACCESS_TOKEN` or the
descriptor-selected equivalent. No tester service-principal secret is created.
ADR-031 requires the CI identity to be an Entitlements member, so positive
tests exercise per-identity authorization.

A descriptor can request a second negative-access token. `spi onboard` then
federates the shared `spi-ci-no-data-access` UAMI to that repository but grants
it no Azure data RBAC and no Entitlements membership.

Rejected: a stack-specific smoke harness is easier to control, but provides less coverage and diverges from ADME modules.

Rejected: a tester service principal supports older test code, but adds a long-lived client secret.

## Consequences

- Acceptance tests validate the same Azure-provider module boundary used by
  ADME, but service forks must carry any required alignment until community
  modules adopt it.
- Positive and negative tests use valid federated tokens, so HTTP 403 results
  distinguish missing authorization from failed authentication.
- Services without a negative-access contract receive no second identity or
  token.
