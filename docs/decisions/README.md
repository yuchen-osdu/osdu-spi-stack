# Architectural Decision Records (ADRs)

An Architectural Decision (AD) is a justified software design choice that
addresses a functional or non-functional requirement that is architecturally
significant. An Architectural Decision Record (ADR) captures a single AD and
its rationale.

## The register model

This directory is a decision register, not a time-boxed log. Each record states
the ruling as it stands; git history carries the chronology, authorship, and
every prior form. Records therefore have no status, no dates, no deciders, and
no amendment trail; a record exists exactly as long as its decision stands.

- **A decision that changes is rewritten in place** through a PR, with the old
  choice moved to a one-line `Rejected:` entry when the contrast still teaches
  something.
- **A decision that stops standing alone is folded** into the record that owns
  its subject, and the corpus is renumbered to stay contiguous, with references
  updated across the repository.
- **A record must earn its file.** The test is operational: without it, would a
  competent contributor plausibly re-propose the rejected alternative or walk
  into the trap it documents? If not, the content belongs in a design doc or
  in the code.

## How to add an ADR

1. Copy `adr-template.md` to `NNN-title-with-dashes.md`, where NNN is the next
   number in sequence. Check open PRs so the number does not collide.
2. Shape: `# ADR-NNN: Title`, `## Context`, `## Decision` with inline
   `Rejected:` one-liners, `## Consequences`. No frontmatter.
3. For each rejected option, write one line preserving its real advantage. The
   decision is what was chosen; alternatives get enough space to show the
   trade-off, no more.
4. Prose follows [docs/STYLE.md](../STYLE.md): impersonal active voice, claims
   backed by a named artifact or exact number, no moment-in-time status.

## ADR style

- **No `## Validation` sections.** Phase-by-phase acceptance logs belong in
  the PR description.
- **No incident narrative in Context.** State the structural problem the
  decision addresses; triggering incidents and specific clusters age poorly.
- **One-line option rejections.** Write `Rejected: <one clause>` rather than
  paragraphs re-litigating prior attempts.
- **Consequences mix good and bad unsorted**, and the honest limitation is
  worth leading with.

## When to create an ADR

Create an ADR for a decision that could plausibly have gone a different way
and where the alternative would be defensible:

- Architecture patterns such as deployment strategies, dependency ordering,
  and GitOps boundaries.
- Technology choices such as middleware, operators, and provisioning tools.
- Design patterns such as namespace models, credential handling, and ingress.
- Security boundaries such as identity, certificates, and admission policy.

## ADR Index

Each row states the ruling so the index can answer "what was decided" without
opening the record.

| ADR | Title | Decision |
|---|---|---|
| [001](001-azure-paas-for-data.md) | Azure PaaS for OSDU Data Services | Every data service with a managed equivalent runs as Azure PaaS; the stack is Azure-only by design. |
| [002](002-aks-automatic.md) | AKS Automatic as the Compute Substrate | The only cluster topology is AKS Automatic on Kubernetes 1.36 with managed Istio `asm-1-30` and automatic node provisioning. |
| [003](003-in-cluster-middleware-scope.md) | In-Cluster Middleware Scope | Elasticsearch, Redis, and PostgreSQL for Airflow are the stateful systems that run in-cluster. |
| [004](004-local-helm-chart-safeguards.md) | Local Helm Chart for Safeguards Compliance | The local `osdu-spi-service` chart applies the pod settings required by AKS Deployment Safeguards. |
| [005](005-workload-identity.md) | Workload Identity for Azure PaaS Access | One OSDU UAMI federated with `workload-identity-sa` carries PaaS access; `dns` ingress uses a separate ExternalDNS UAMI. |
| [006](006-three-namespace-model.md) | Three-Namespace Model | Workloads split across `foundation`, `platform`, and `osdu`. |
| [007](007-layered-kustomization-ordering.md) | Layered Flux Kustomization Ordering | Flux reconciles health-gated layers 0a through 6; graduated adds Layer 7 for Wellbore. |
| [008](008-bicep-for-azure-provisioning.md) | Bicep for Azure Provisioning | Raw AKS Bicep plus hand-written PaaS modules declare Azure resources; the CLI handles client-side seams. |
| [009](009-flux-cd-for-gitops.md) | Flux CD + AKS GitOps Extension for In-Cluster Reconciliation | One Flux configuration reconciles the selected stack and ingress trees. |
| [010](010-keyvault-secret-management.md) | Key Vault + ConfigMap Secret Model | Entra tokens, Key Vault values, and CLI-generated Kubernetes Secrets have separate ownership. |
| [011](011-trust-manager-ca-distribution.md) | Cross-Namespace CA Distribution via trust-manager | trust-manager mirrors Redis and Elasticsearch CAs into `osdu`; Redis traffic disables Istio mTLS. |
| [012](012-ingress-profiles.md) | Three Ingress Profiles | `azure`, `dns`, and `ip` are separate Flux trees with one Gateway owner per deployment. |
| [013](013-schema-load-flux-job.md) | Schema Load via a Flux-Managed Job | A Flux Job loads schemas after core services and partition initialization become Ready. |
| [014](014-suspend-gitops-after-deploy.md) | Suspend GitOps Reconciliation After Deploy | `spi up` pins the GitRepository to the deployed commit until reconciliation is resumed. |
| [015](015-partition-entitlements-bootstrap.md) | Partition and Entitlements Bootstrap through Flux Jobs | Two Jobs per partition create the Partition record and Entitlements root groups before schema load. |
| [016](016-istio-jwt-projection.md) | Istio JWT Projection for Azure-Provider OSDU Services | Istio validates Entra JWTs and projects each caller's `x-app-id` and `x-user-id` headers. |
| [017](017-osdu-image-lock.md) | Per-Deploy Image Lock via ConfigMap + Flux Substitution | `osdu-image-lock` records the selected service images and preserves explicit pins across refreshes. |
| [018](018-karpenter-nodepool-authoring.md) | Karpenter NodePool Authoring as Workload Manifests | Flux-managed NodePools separate platform and OSDU workloads with stack-owned labels and taints. |
| [019](019-osdu-flux-gitops-namespace.md) | SPI-Owned GitOps Objects in a Dedicated osdu-flux Namespace | The CLI stores SPI-owned Flux inputs in the user-managed `osdu-flux` namespace. |
| [020](020-optional-application-insights.md) | Opt-In Application Insights Provisioning | Application Insights and Log Analytics deploy only when enabled, and reruns preserve the environment choice. |
| [021](021-middleware-only-minimal-profile.md) | Middleware-Only `minimal` Profile, Replacing the Unbacked `full` | `minimal` stops before OSDU services; `bare` deploys no in-cluster stack. |
| [022](022-tls-certificates-in-platform.md) | TLS Certificates in platform with Gateway ReferenceGrants | Certificates live in `platform`; ReferenceGrants let the managed Istio Gateway read them. |
| [023](023-entra-only-data-plane.md) | Entra-Only Data Plane: Disable Local Auth on Cosmos and Service Bus | Cosmos DB and Service Bus disable local authentication and retain only Entra-backed access. |
| [024](024-windows-batch-shim-launcher.md) | Windows Batch Shim Launcher via an Escaped cmd.exe Command Line | Windows batch shims launch through one escaped `cmd.exe` command line in `spi.shell.run_process`. |
| [025](025-single-flux-inventory-owner.md) | One Flux Inventory Owner per Kubernetes Object | Each Kubernetes object has one Flux owner; ingress trees use a stable Gateway inventory name. |
| [026](026-bind-managed-istio-ingress.md) | Bind to the AKS Managed Istio Ingress | Stack routes bind to the AKS-provisioned external ingress Service instead of deploying another LoadBalancer. |
| [027](027-adme-aligned-integration-tests.md) | ADME-Aligned Integration Tests with Federated Identities | Service lanes run ADME-aligned Azure modules with federated positive and negative test identities. |
| [028](028-descriptor-aware-onboarding.md) | Descriptor-Aware Cluster-Side Onboarding | `spi onboard` validates schema version 2 descriptors, reconciles environment access, and optionally verifies the deploy lane. |
| [029](029-spi-ghcr-service-images.md) | SPI GHCR Images as the Service Baseline | New deployments resolve the selected GHCR fleet to immutable digests; community images remain an explicit whole-fleet source. |
| [030](030-runtime-resolved-istio-revision.md) | Runtime-Resolved Managed Istio Revision | The CLI publishes the live mesh revision for Flux substitution into the `osdu` namespace label. |
| [031](031-per-identity-authorization.md) | Per-Identity Authorization with Explicit Membership | Istio preserves caller identity and Entitlements membership grants access through public APIs. |
| [032](032-deploy-test-restore-lane.md) | Direct Deploy, Test, and Restore on Suspended Flux | Pull-request images deploy directly by digest only while the GitRepository, Kustomizations, and HelmReleases are suspended. |
| [033](033-subscription-resolved-availability-zones.md) | Subscription-Resolved System-Pool Availability Zones | The CLI derives the full usable zone set for `Standard_D4lds_v5` before AKS deployment. |
| [034](034-graduated-wellbore-profile.md) | Graduated Profile Starts with the Wellbore Service Pair | `graduated` adds the public Wellbore service and its internal worker at Layer 7. |
| [035](035-spi-pool-node-label.md) | `spi-pool` as the Workload Placement Label | Stack-owned `spi-pool` labels select the Karpenter platform and OSDU pools. |
| [036](036-airflow-3.md) | Airflow 3 as the Single Workflow Engine | The middleware layer deploys one Airflow 3.2.2 topology with CLI-owned signing material. |
| [037](037-azure-data-plane-rbac-paths.md) | Two Azure Data-Plane RBAC Resource Paths | Cosmos-native grants and standard Azure role assignments provision the Entra-only data plane. |
