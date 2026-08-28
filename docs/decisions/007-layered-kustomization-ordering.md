# ADR-007: Layered Flux Kustomization Ordering

## Context

The workload graph requires CRDs before custom resources, operators before
instances, middleware before services, and tenant initialization before schema
loading. A flat apply order does not express health-gated dependencies and
turns expected startup races into deployment noise.

## Decision

Encode the dependency graph as Flux Kustomizations with explicit `dependsOn`
and `wait: true`. `software/stacks/osdu/profiles/core/stack.yaml` defines the
core layers; the graduated profile adds Layer 7.

| Layer | Kustomizations | Dependency |
|---|---|---|
| 0a | namespaces | none |
| 0b | Karpenter NodePools | 0a |
| 1 | cert-manager, trust-manager, ECK, CNPG | 0a; trust-manager also depends on cert-manager |
| 2 | Elasticsearch, Redis, PostgreSQL | Layer 1 and NodePools |
| 3 | Airflow | PostgreSQL |
| 4a | OSDU configuration | namespaces |
| 4b | CA bundles and Redis mesh policy | trust-manager, Elasticsearch, Redis, configuration |
| 5 | core OSDU services | Layer 4b and NodePools |
| 5a | partition and Entitlements initialization | core services |
| 5b | schema load | initialization |
| 6 | reference services | core services and schema load |
| 7 | Wellbore services | reference services |

The selected ingress tree owns the Gateway and adds route Kustomizations at
the matching service layers. Kustomizations without a dependency between them
reconcile in parallel. Layer timeouts match the slowest workload, including
155 minutes for schema load.

Rejected: one flat Kustomization is shorter, but apply order does not provide health-gated dependencies across operators and workloads.

## Consequences

- A failed layer blocks its consumers and identifies the dependency that did
  not become Ready.
- The graph is visible in profile `stack.yaml` files and `flux get
  kustomizations`.
- Adding a component requires a Kustomization and dependency wiring; a slow
  dependency delays every downstream layer.
