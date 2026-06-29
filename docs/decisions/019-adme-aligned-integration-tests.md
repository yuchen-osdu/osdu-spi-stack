# ADR-019: ADME-Aligned, Secret-Less Integration Tests on the Deploy Lane

**Status**: Accepted

## Context

The deploy lane (`validate.yml` → build → deploy → integration-test) needs a
real integration suite per service so a PR proves the service works against the
live SPI Stack, not just that it compiles. Two questions had to be answered: (1)
*which* tests run, and (2) *how* the suite authenticates, given the SPI Stack is
managed-identity-only and carries no test service-principal secret.

Microsoft's ADME (Azure Data Manager for Energy) runs the authoritative
Azure-provider OSDU suites. `oep-deployment-resources/IntegrationTests/OneBranch/ServiceITsProd.yml`
shows that for each service ADME builds the `testing/<svc>-test-core` reactor
module and runs `testing/<svc>-test-azure`, passing a single bearer token under
the canonical name `INTEGRATION_TESTER_ACCESS_TOKEN`. That token is minted
**without a secret** (`GenerateTokenWithoutSecret.yml` → `az account
get-access-token --resource <clientId>`), i.e. federated, exactly the mechanism
the SPI deploy lane already uses for its MSI.

The community/open-source `<svc>-test-azure` modules our forks sync are *older*
than ADME's internal forks and lack the federated-token support, so they fail on
a managed-identity-only stack.

## Decision

Run the same modules ADME runs (`testing/<svc>-test-azure`, with
`<svc>-test-core` built via the testing reactor) and authenticate every service
suite with the single federated `INTEGRATION_TESTER_ACCESS_TOKEN`, minted as the
service's CI managed identity (`azure/login` OIDC → `az account
get-access-token --resource $AAD_CLIENT_ID`). No test service-principal secret
is introduced; the stack stays secret-less and the suite exercises the
per-identity entitlements model (ADR-016) because the CI MSI is a real
entitlements member seeded by `spi onboard`.

Service-specific alignment, each verified byte-identical against the ADME fork
(`OpenEnergyPlatform/OSDU-Legal`, `OSDU-Storage`, …):

- **storage** — `storage-test-azure`'s `AzureTestUtils` already consumes
  `INTEGRATION_TESTER_ACCESS_TOKEN` if present, so no source change. The
  `TestRecordAccessAuthorization` and `should_returnRecordsAfterCrsConversion*`
  cases are excluded: the former needs a second *no-data-access* identity (out of
  scope for now); the latter need the `crs-conversion` service, which is not
  deployed in the scoped stack.
- **legal** — `legal-test-azure`'s `AzureLegalTagUtils.accessToken()` is
  forward-ported from ADME so it prefers `INTEGRATION_TESTER_ACCESS_TOKEN` and
  only falls back to the SP client-credentials flow when no token is supplied.
  ADME also comments out the `uploadTenantTestingConfigFile()` calls in
  `CreateLegalTagApiAcceptanceTests` ("the older config is not provided hence
  commenting"); the deployed legal service already carries the COO config, so the
  test needs no blob credential. Adopting both deltas removes the only dependency
  on a tester SP secret. The three tests ADME excludes (subscription-message
  delete, client-consent first-party create, invalid-expiration validate) are
  excluded identically.
- **maven invocation** — the lane runs a single reactor pass
  (`mvn -pl <svc>-test-azure -am verify`). Because `-am` also runs the upstream
  `<svc>-test-core` module's `test` phase, and a `-Dtest=!…` exclusion puts
  surefire into "specified tests" mode, the core module (abstract base classes,
  no concrete tests) fails with *"No tests matching pattern were executed"*. The
  goal therefore carries `-Dsurefire.failIfNoSpecifiedTests=false`
  (and `-Dmaven.surefire.useFile=false`, matching ADME's console output). ADME
  side-steps this by building core (`install`) and azure (`verify`) in separate
  invocations; the single-reactor pass is equivalent with the flag.

The token env var name, maven goal, maven profile, and per-attempt timeout are
all driven by repo variables (`ROOT_TOKEN_ENV`, `MAVEN_GOAL`, `MAVEN_PROFILE`,
`IT_TIMEOUT_MINUTES`/`IT_MAX_ATTEMPTS`) so a service is configured without
editing the workflow.

## Consequences

- Both legal and storage run their full ADME suites green on the SPI Stack with
  no test secret: storage 126/0F/0E, legal 107/0F/0E/1-skip, every API call
  authorized via the federated CI-MSI token.
- Test source that we forward-port from ADME (legal `AzureLegalTagUtils`,
  `CreateLegalTagApiAcceptanceTests`) is a divergence from the community
  `fork_upstream`. It is kept byte-identical to the ADME fork, not custom, so a
  future upstream that adopts the same change merges cleanly; until then it must
  survive template/upstream sync.
- Excluding the crs-conversion storage cases means a regression in storage's
  frame-of-reference conversion path would not be caught here. The exclusion is
  documented as a scoped-stack dependency gap, not a permanent decision — once
  `crs-conversion` is deployed the exclusion should be removed.
- The `NO_DATA_ACCESS_TESTER` placeholder for storage leaves the negative
  authorization test (`TestRecordAccessAuthorization`) unexercised. Wiring a real
  second identity is follow-up work.

Rejected alternatives:

- **Provision a real tester service principal + secret for legal's COO blob.**
  Tried and discarded: it works but adds an SP secret and RBAC the rest of the
  stack deliberately avoids, and ADME itself does not do it — ADME comments the
  blob upload out instead. Matching ADME is both cleaner and secret-less.
- **Keep the SPI-custom acceptance harness instead of the ADME modules.** Less
  coverage and drifts from what ADME validates; contradicts the directive to run
  the same suites ADME runs.
