# ADR-029: SPI GHCR Images as the Service Baseline

## Context

The SPI service repositories publish Azure-provider images before equivalent
community GitLab images exist. Deploying mutable tags would make the running
fleet differ from the image set recorded at deployment time, while removing
the community source would prevent explicit compatibility testing.

## Decision

Default new deployments to public GHCR packages in `yuchen-osdu` selected by
`main-snapshot`. `src/spi/images.py` resolves the selector to OCI digests before
Azure provisioning and writes the complete result to
`osdu-flux/osdu-image-lock`. Helm releases consume
`repository@sha256:<digest>`, not the mutable discovery tag.

The selector surface is:

- `--image-tag <tag>` for one exact GHCR tag across the fleet.
- `--image-ref <git-ref>` for each repository's `sha-<commit>` image.
- `--image-source community --image-branch <branch>` for the explicit OSDU
  GitLab source.

Resolution is all-or-nothing and has no per-service fallback. The core profile
locks 14 images, including schema-load; graduated locks those 14 plus the two
Wellbore images. GHCR fleets resolve schema-load from its community package
because the SPI schema repository publishes no separate loader package.

Rejected: deploying `main-snapshot` directly follows new builds automatically, but changes artifacts without a new image lock.

Rejected: community images avoid the SPI registry dependency, but do not represent the SPI service repositories.

Rejected: copying the fleet into ACR centralizes pulls, but adds replication without changing digest identity.

## Consequences

- A deployment records an immutable fleet, but every selected package must
  resolve before provisioning starts.
- `spi reconcile --refresh-images` preserves the stored source and selector
  unless the operator supplies replacements.
- The community source remains available, but its images must support the
  Entra-only data plane defined by ADR-023.
- Schema-load remains a community runtime dependency for the GHCR fleet.
