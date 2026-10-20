# ADR-044: OSDU DAGs Load at Airflow Start on the Core Profile

## Context

The workflow service hands every OSDU workflow run to Airflow, and the stack's Airflow 3.2.2 release carries no DAGs, no OSDU Python libraries, and no workflow registrations. Since v0.30.1 the workflow service calls the Airflow 3 API with its own `osdu-workflow` login. The upstream ingestion DAGs need three things in every process that parses or runs them: the `osdu-api`, `osdu-airflow`, and `osdu-ingestion` libraries; environment-backed Variables naming the OSDU service URLs; and an identity the services accept. The stack stores no credentials (ADR-005, ADR-023), every pod passes the Safeguards admission policy (ADR-002, ADR-004), and the stack publishes no images: every runtime image comes from a service fork or the community registry (ADR-017, ADR-033). The DAG sources live in six community repositories, five of which carry Azure provider code and become SPI forks. Core needs only the ingestion DAGs; domain services such as DDMS and EDS bring their own DAGs, published as CImpl-style templates whose `{| TOKEN |}` placeholders the deployment fills in, and most run their work in KubernetesPodOperator worker pods.

## Decision

The core profile loads pinned DAG files and hash-locked libraries into the stock Airflow image when the scheduler and dag-processor start, runs tasks inside the scheduler, and registers exactly the selected DAGs with the workflow service. The DAGs come from a catalog of families: the default families load on every core environment, and an environment adds others by name.

- **LocalExecutor for every profile that runs Airflow.** `software/components/airflow/release.yaml` sets `executor: LocalExecutor`. Tasks run as scheduler subprocesses, so no task pod exists and no per-task image, identity, or admission contract has to hold. The scheduler's requests and limits carry the task load, and `core.parallelism` caps concurrent tasks.
  - `workers.persistence.enabled` stays `false`. Under LocalExecutor the chart applies it to the scheduler, and `true` would make the scheduler a StatefulSet with a log volume claim.
  - `core.hostname_callable` records the scheduler pod's IP, because the API server fetches task logs from that host and a Deployment pod's name does not resolve.
- **DAG families are cataloged and pinned.** `software/components/airflow-osdu/dag-sources.yaml` names each family with:
  - its source, from the SPI fork once one exists and community GitLab otherwise: files shipped in the ConfigMap (`local`); raw files from GitLab or GitHub at a full commit SHA, each with its sha256; or an `archive` of the repository at a commit, filtered by `include` globs below `strip_prefix` and pinned by a digest over the selected files' hashes;
  - `default: true` for the families every core environment loads;
  - `requires:`, the stack services the family's DAGs call;
  - an optional `render:` map for templates;
  - the DAG IDs the family defines.

  The default families are `spi-smoke` (`Echo_DAG` and `airflow_monitoring`, shipped in a ConfigMap) and `core-ingestion` (`Osdu_ingest` and `manifest_ingestion` from ingestion-dags `v0.30.0`, commit `193f921e`). `scripts/pin_dag_family.py` fetches a family the way the loader does and prints or checks its pins.
- **An environment selects add-on families by name.** A stack file passed to `spi up -f` lists them under `dags:` (provisional). Before any change, `spi up` refuses an unknown name, a default family, a duplicate, and a family whose `requires:` names a service the stack does not deploy, reporting every problem at once. It writes the selection to `osdu-flux/spi-dags`, which Flux substitutes as `SPI_DAG_FAMILIES` into the loader, the Airflow pod annotations, and the registration Job, so a changed selection restarts Airflow and re-registers. A stack file without `dags:` clears the selection; `spi up` without `-f` keeps it.
- **Templates render with standard values.** A family with `render:` has every `{| TOKEN |}` placeholder in its files replaced verbatim, after its pins are checked, using the CImpl token names, so an upstream template loads unchanged. A value of `@name` takes `name` from `render-values.yaml`: `worker_namespace`, `worker_pod_kwargs`, and `worker_env`. A value for a token ending in `IMAGE` must pin a digest. A placeholder left over fails the load.
- **Worker pods run as the OSDU identity beside the scheduler.** The standard pod values place a KubernetesPodOperator worker in `platform` on the platform nodes, as `workload-identity-sa` with the Workload Identity label, without an Istio sidecar, with requests, limits, and a restricted security context, and delete it when it succeeds. `worker_env` passes the service URLs and the token audience as Jinja that reads Airflow Variables when the task runs.
- **The loader fails closed.** An init container on the scheduler and the dag-processor runs on the pinned Airflow image. It checks the whole catalog, then for each selected family fetches the files at the commit, verifies every sha256 or the archive digest, renders templates, and writes the files under `/opt/airflow/dags/<family>/`, keeping their paths below the family's source prefix. An unknown catalog key, or a fetch, hash, digest, or render failure, exits non-zero and holds the pod in init.
- **Libraries install from a hash-locked requirements file.**
  - `_PIP_ADDITIONAL_REQUIREMENTS` on the scheduler and dag-processor installs `osdu-api==1.2.4`, `osdu-airflow==0.30.9`, `osdu-ingestion==0.30.2`, and their resolved dependencies with `--require-hashes`.
  - Packages come from the GitLab package pages through `--find-links`, with no extra index.
  - `osdu-api` installs without its `[azure]` extra, whose pins would downgrade the image's Azure SDKs. `osdu-airflow` installs without `[AF3]`, which requires Airflow 3.3.
- **Only the core profile loads DAGs.** `profiles/core/stack.yaml` points `spi-airflow` at `software/components/airflow-osdu`, an overlay on the base component. `minimal` keeps the base component with no loader, libraries, or OSDU settings, and `bare` runs no Airflow.
- **Tasks authenticate with Workload Identity.**
  - The scheduler runs as `platform/workload-identity-sa` with the `azure.workload.identity/use` label.
  - `CLOUD_PROVIDER=azure`, `AZURE_ENABLE_WORKLOAD_IDENTITY=true`, and `AZURE_RESOURCE_ID` set to the audience `spi.info.token_audience` derives make `osdu-api` request `<audience>/.default`.
  - No client secret, MSI mode, or `AIRFLOW_VAR_AAD_CLIENT_ID` is set. The dag-processor parses DAGs without an Azure identity.
- **A run's caller must be impersonable.**
  - `osdu-api` sends `on-behalf-of` and `x-on-behalf-of` with the run's `userId` on every service call, unless the ID ends in `@service.local` or starts with `service-account-`.
  - Entitlements accepts the header only when the token's identity is in `users.datalake.delegation` and the named user is in `users.datalake.impersonation`. The OSDU identity is in both.
  - The `entitlements-members` Job (ADR-015) adds the deploy identity to `users.datalake.impersonation`, so an ingestion run it starts through the workflow service can read schemas and write records. It also seeds the person who last ran `spi up` as an `admin`, and the `spi users add` roles that can start a run add a person to it too (ADR-039).
- **Variables are environment-backed.** `airflow-variables.env` becomes the `airflow-osdu-variables` ConfigMap, read through `extraEnvFrom` by every Airflow container: `AIRFLOW_VAR_CORE__SERVICE__*` carry `http://<service>.osdu.svc.cluster.local/api/...`, because operators read some of them when the DAG file is parsed. The CLI-written `spi-airflow-osdu` adds `azure_resource_id` and `data_partition` with the `AIRFLOW_VAR_` prefix. A family that needs another service URL adds it to the one map.
- **Registration follows import.** DAGs are created paused. `spi-osdu-workflow-registration` runs a Job in `platform`, beside the DAG declaration and the Airflow login, as `platform/workload-identity-sa`, which federates to the same OSDU identity. It runs once Airflow and OSDU initialization are Ready, and again whenever the loaded DAG set or the selection changes. It logs in to Airflow as `osdu-workflow`, the login v0.30.1 gives the workflow service. The Job:
  1. waits until Airflow's live (not stale) DAG IDs equal the selected families' set, ignoring deselected catalog DAGs, with no import errors in the selected families;
  2. registers each ID through `POST /api/workflow/v1/workflow/system`, with the DAG ID as the workflow name, which is the DAG the Azure engine triggers, and a description naming its family;
  3. treats a 409 response as already registered;
  4. unpauses exactly those IDs;
  5. for each deselected catalog DAG whose workflow carries that description, pauses the DAG and deletes the system workflow. Workflow has no system-scoped read, so it reads the workflow back in the primary partition, `DATA_PARTITION` from `spi-airflow-osdu`, and leaves alone a name that another tenant or system record holds.

Rejected: an Airflow image built with the libraries and rendered DAGs, pinned by digest. It removes every network fetch from pod start, but the stack would publish an image built from its own sources.

Rejected: an image built in a dedicated repository. Image publishing stays outside the stack, but the DAGs already live in their community repositories and SPI forks, and a second home duplicates their pins.

Rejected: KubernetesExecutor. Each task gets its own pod and resource limits, but every task pod then needs the DAGs, the libraries, a Safeguards-compliant template, Workload Identity, and the pinned image, so an install at start would repeat for every task.

Rejected: a loader that tracks a branch, flattens paths, and continues past a failed download. It needs no source list, but a restart can change the parsed code and a partial DAG set loads without an error.

Rejected: git-sync. A commit pins the DAG source, but the libraries still need an install, and the DAGs sit in several repositories rather than one tree.

Rejected: DAGs in the `airflow-dags` Blob container or a file share. The container exists, but its contents are mutable and mounting it needs a CSI driver or a copy step.

Rejected: a family per Kustomize component, selected by listing it in the profile. Selection stays in Git, but every environment that adds a family needs its own overlay, and nothing checks a family's required services before Flux applies it.

Rejected: per-partition workflow registration. Each partition gets its own record, but a system workflow is visible from every partition through one root-identity call per DAG.

## Consequences

- Tasks share the scheduler pod's CPU and memory. A memory-heavy ingestion can take the scheduler down with it, so the scheduler's limits and `core.parallelism` bound concurrent work, and isolation between tasks is a process boundary, not a pod.
- An Airflow pod start depends on community GitLab and its package registry. A failed fetch holds the pod in init with a named error, and while the scheduler is held no task runs.
- The commit pins, per-file hashes, and archive digests make the parsed DAG set identical across restarts. A DAG or library change, or a new family, is a reviewed edit to `dag-sources.yaml`, `render-values.yaml`, or the requirements file and ships in a stack release. Selecting an existing family needs no stack change.
- Every family shares one Python environment, so a family whose libraries conflict with the core lock cannot load. A family that needs other libraries runs them in its worker image.
- An archive source is held in the loader's memory, up to 200 MiB, so the init container's limit is 512Mi.
- `requires:` is checked only by `spi up`; the loader trusts the selection Flux substitutes.
- Editing any file the loader reads restarts the scheduler and the dag-processor. A hashed copy of those files in `osdu-flux` stamps its name into a pod annotation, because Kustomize rewrites a generated name only within one namespace.
- `_PIP_ADDITIONAL_REQUIREMENTS` is the Airflow image's development path. Every scheduler and dag-processor start pays the install.
- `osdu-airflow` 0.30.9 runs on Airflow 3.2.2, below the 3.3 floor its `[AF3]` extra declares. The DAG tests exercise that combination, not the declared one.
- DAG code runs as the OSDU identity, which holds service-account rights in every partition. The trust boundary is the reviewed, commit-pinned source list.
- DAG code can act as the deploy identity. That adds no rights, because the OSDU identity it runs as already holds service-account rights.
- A person starts an ingestion run with their own token once `spi users add` has given them `editor`, `admin`, or `ops`, each of which holds `users.datalake.impersonation` (ADR-039). A `viewer` cannot start a run.
- The chart binds pod launch rights to the scheduler's ServiceAccount, so `platform/workload-identity-sa` can create, delete, and exec into pods in `platform`. Nothing else in `platform` runs as that ServiceAccount.
- Task logs live on the scheduler pod and do not survive its restart.
- The workflow service's run limit calls `activeDagRuns/`, which Airflow 3 does not serve. The failure is logged and the limit is not enforced.
- A family whose required services the stack does not run is refused at `spi up`. `ingestion-by-reference` is cataloged with `requires: [dataset]` for that reason. CSV needs a csv-parser worker image that supports Workload Identity, and SEG-Y and EDS arrive with their services.
- `osdu-worker-smoke`, an example family under `examples/dags`, is a CImpl-style template whose worker lists its Entitlements groups. It proves the archive, render, and worker path until a DDMS or EDS family is onboarded.
