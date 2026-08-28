# ADR-034: Graduated Profile Starts with the Wellbore Service Pair

## Context

Domain services add image, routing, storage, and authorization contracts that
do not belong in every core deployment. The Wellbore worker performs internal
bulk processing, while the Wellbore main service remains the public API and ACL
boundary.

## Decision

Add a cumulative `graduated` profile above `core`.
`software/stacks/osdu/profiles/graduated/stack.yaml` adds the Layer 7
`spi-wellbore-services` Kustomization after `spi-osdu-reference`. The matching
`<mode>-graduated` ingress tree exposes only the main service.

The profile deploys `wellbore-domain-services` and
`wellbore-domain-services-worker`, applies NetworkPolicy and Istio
AuthorizationPolicy to the worker, and uses the shared Workload Identity. The
worker accesses the existing `wdms-osdu` Blob container through Partition and
Key Vault configuration.

Image resolution is atomic across the 14 core images and two Wellbore images.
`core` remains the default and no missing GHCR image falls back to community.

Rejected: adding Wellbore to `core` gives one full-service profile, but makes core carry a domain-specific API and worker.

Rejected: exposing the worker simplifies direct testing, but bypasses the main service's API and ACL boundary.

## Consequences

- Graduated deployments require 16 resolvable image entries before
  provisioning; core deployments remain independent of Wellbore.
- The main service is externally routable and the worker remains cluster-only.
- Additional graduated families can add later layers, but each expands the
  profile's image and policy surface.
