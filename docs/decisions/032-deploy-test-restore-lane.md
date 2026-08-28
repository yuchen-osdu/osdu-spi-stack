# ADR-032: Direct Deploy, Test, and Restore on Suspended Flux

## Context

Service pull requests need to test one image digest without changing the
stack's baseline image lock. Flux Kustomizations and HelmReleases continue to
reconcile a cached source artifact after the GitRepository stops polling, so a
source-only suspension can revert a direct Deployment update during a test.

## Decision

Use a bounded direct-mutation lane against a fully suspended Flux state.
`spi reconcile --suspend` patches the Stack GitRepository and every
Kustomization and HelmRelease in `osdu-flux` with `spec.suspend: true`.
`spi reconcile --resume` clears those flags.

The service template's `🔒 Deploy, Test & Restore` workflow records the running
image, deploys the pull-request digest directly to the target Deployment,
waits for the new pod, runs the descriptor-selected acceptance suite, and
restores the recorded image before the run releases its service concurrency
lock. Digest capture selects a Running, Ready, non-terminating target pod.

`spi onboard --verify` establishes the suspended state, dispatches Validation
for the repository's resolved `main` SHA with `force_full_pipeline=true`, and
requires the deploy and integration-test jobs to succeed. It leaves Flux
suspended for later deploy-lane runs; resuming is an explicit operator action.

Rejected: committing pull-request digests gives Flux sole ownership, but mutates the baseline and couples two repositories.

Rejected: suspending only the GitRepository preserves cached reconciliation, but permits controllers to overwrite the test image.

## Consequences

- A service image can be tested and removed without changing
  `osdu-image-lock`, but direct mutation is safe only while all three Flux
  resource kinds remain suspended.
- A failed restore can leave the test digest running; the cluster stays frozen
  so Flux does not hide that state before investigation.
- Baseline refreshes require `spi reconcile --resume` or an explicit image-lock
  refresh outside an active deploy lane.
