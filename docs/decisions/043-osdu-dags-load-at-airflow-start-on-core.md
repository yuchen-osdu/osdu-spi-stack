# ADR-043: OSDU DAGs Load at Airflow Start on the Core Profile

## Context

The workflow service hands every OSDU workflow run to Airflow, and the stack's Airflow 3.2.2 release carries no DAGs, no OSDU Python libraries, and no workflow registrations. The workflow service is not wired to the Airflow 3 API: with `OSDU_AIRFLOW_VERSION` unset it selects its Airflow 2 client, and no Airflow credentials reach it. The upstream ingestion DAGs need three things in every process that parses or runs them: the `osdu-api`, `osdu-airflow`, and `osdu-ingestion` libraries; environment-backed Variables naming the OSDU service URLs; and an identity the services accept. The stack stores no credentials (ADR-005, ADR-023), every pod passes the Safeguards admission policy (ADR-002, ADR-004), and the stack publishes no images: every runtime image comes from a service fork or the community registry (ADR-017, ADR-033). The DAG sources live in six community repositories, five of which carry Azure provider code and become SPI forks.

## Decision

The core profile loads pinned DAG files and hash-locked libraries into the stock Airflow image when the scheduler and dag-processor start, runs tasks inside the scheduler, and registers exactly the declared DAGs with the workflow service.

- **LocalExecutor for every profile that runs Airflow.** `software/components/airflow/release.yaml` sets `executor: LocalExecutor`. Tasks run as scheduler subprocesses, so no task pod exists and no per-task image, identity, or admission contract has to hold. The scheduler's requests and limits carry the task load, and `core.parallelism` caps concurrent tasks.
  - `workers.persistence.enabled` stays `false`. Under LocalExecutor the chart applies it to the scheduler, and `true` would make the scheduler a StatefulSet with a log volume claim.
  - `core.hostname_callable` records the scheduler pod's IP, because the API server fetches task logs from that host and a Deployment pod's name does not resolve.
- **DAG sources are pinned file by file.** `software/components/airflow-osdu/dag-sources.yaml` names each DAG family:
  - its repository, which is the SPI fork once one exists and community GitLab otherwise;
  - the commit;
  - every file with its sha256;
  - the DAG IDs the family defines.

  The first families are `spi-smoke` (`Echo_DAG` and `airflow_monitoring`, shipped in a ConfigMap) and `core-ingestion` (`Osdu_ingest` and `manifest_ingestion` from ingestion-dags `v0.30.0`, commit `193f921e`).
- **The loader fails closed.** An init container on the scheduler and the dag-processor runs on the pinned Airflow image. It fetches each file at its commit, verifies the sha256, and writes it under `/opt/airflow/dags/<family>/`, keeping its path below the family's source prefix. A fetch or hash failure exits non-zero and holds the pod in init.
- **Libraries install from a hash-locked requirements file.**
  - `_PIP_ADDITIONAL_REQUIREMENTS` on the scheduler and dag-processor installs `osdu-api==1.2.4`, `osdu-airflow==0.30.9`, `osdu-ingestion==0.30.2`, and their resolved dependencies with `--require-hashes`.
  - Packages come from the GitLab package pages through `--find-links`, with no extra index.
  - `osdu-api` installs without its `[azure]` extra, whose pins would downgrade the image's Azure SDKs. `osdu-airflow` installs without `[AF3]`, which requires Airflow 3.3.
- **Only the core profile loads DAGs.** `profiles/core/stack.yaml` points `spi-airflow` at `software/components/airflow-osdu`, an overlay on the base component. `minimal` keeps the base component with no loader, libraries, or OSDU settings, and `bare` runs no Airflow.
- **Tasks authenticate with Workload Identity.**
  - The scheduler runs as `platform/workload-identity-sa` with the `azure.workload.identity/use` label.
  - `CLOUD_PROVIDER=azure`, `AZURE_ENABLE_WORKLOAD_IDENTITY=true`, and `AZURE_RESOURCE_ID` set to the audience `spi.info.token_audience` derives make `osdu-api` request `<audience>/.default`.
  - No client secret, MSI mode, or `AIRFLOW_VAR_AAD_CLIENT_ID` is set. The dag-processor parses DAGs without an Azure identity.
- **Variables are environment-backed.** `AIRFLOW_VAR_CORE__SERVICE__*` carry `http://<service>.osdu.svc.cluster.local/api/...` on both components, because operators read some of them when the DAG file is parsed.
- **The workflow service uses its Airflow 3 engine.**
  - `software/stacks/osdu/services/workflow.yaml` sets `OSDU_AIRFLOW_VERSION=airflow3` and `OSDU_AIRFLOW_AIRFLOW3_URL=http://airflow-api-server.platform.svc.cluster.local:8080/airflow`.
  - The username and password reach the service through `valueFrom` on `osdu/workflow-airflow-credentials`.
  - The CLI writes that Secret from the same seed `createUserJob` uses to create the `osdu-workflow` Airflow user.
- **Registration follows import.** DAGs are created paused. `spi-osdu-workflow-registration` runs a Job as `osdu/workload-identity-sa` once the workflow service and Airflow are Ready. The Job:
  1. requires Airflow's parsed DAG IDs to equal the declared set, with no import errors;
  2. registers each ID through `POST /api/workflow/v1/workflow/system`;
  3. compares a 409 response against the expected registration;
  4. unpauses exactly those IDs.

Rejected: an Airflow image built with the libraries and rendered DAGs, pinned by digest. It removes every network fetch from pod start, but the stack would publish an image built from its own sources.

Rejected: an image built in a dedicated repository. Image publishing stays outside the stack, but the DAGs already live in their community repositories and SPI forks, and a second home duplicates their pins.

Rejected: KubernetesExecutor. Each task gets its own pod and resource limits, but every task pod then needs the DAGs, the libraries, a Safeguards-compliant template, Workload Identity, and the pinned image, so an install at start would repeat for every task.

Rejected: a loader that tracks a branch, flattens paths, and continues past a failed download. It needs no source list, but a restart can change the parsed code and a partial DAG set loads without an error.

Rejected: git-sync. A commit pins the DAG source, but the libraries still need an install, and the DAGs sit in several repositories rather than one tree.

Rejected: DAGs in the `airflow-dags` Blob container or a file share. The container exists, but its contents are mutable and mounting it needs a CSI driver or a copy step.

Rejected: per-partition workflow registration. Each partition gets its own record, but a system workflow is visible from every partition through one root-identity call per DAG.

## Consequences

- Tasks share the scheduler pod's CPU and memory. A memory-heavy ingestion can take the scheduler down with it, so the scheduler's limits and `core.parallelism` bound concurrent work, and isolation between tasks is a process boundary, not a pod.
- An Airflow pod start depends on community GitLab and its package registry. A failed fetch holds the pod in init with a named error, and while the scheduler is held no task runs.
- The commit pin and per-file hashes make the parsed DAG set identical across restarts. A DAG or library change is a reviewed edit to `dag-sources.yaml` or the requirements file and ships in a stack release.
- Editing any file the loader reads restarts the scheduler and the dag-processor. A hashed copy of those files in `osdu-flux` stamps its name into a pod annotation, because Kustomize rewrites a generated name only within one namespace.
- `_PIP_ADDITIONAL_REQUIREMENTS` is the Airflow image's development path. Every scheduler and dag-processor start pays the install.
- `osdu-airflow` 0.30.9 runs on Airflow 3.2.2, below the 3.3 floor its `[AF3]` extra declares. The DAG tests exercise that combination, not the declared one.
- DAG code runs as the OSDU identity, which holds service-account rights in every partition. The trust boundary is the reviewed, commit-pinned source list.
- The chart binds pod launch rights to the scheduler's ServiceAccount, so `platform/workload-identity-sa` can create, delete, and exec into pods in `platform`. Nothing else in `platform` runs as that ServiceAccount.
- Task logs live on the scheduler pod and do not survive its restart.
- The workflow service's run limit calls `activeDagRuns/`, which Airflow 3 does not serve. The failure is logged and the limit is not enforced.
- A family whose services the profile does not run is never declared. CSV needs a Workload Identity csv-parser worker, by-reference ingestion needs the Dataset service, and SEG-Y and EDS arrive with their add-ons.
