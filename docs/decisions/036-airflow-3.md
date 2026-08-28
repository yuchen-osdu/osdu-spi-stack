# ADR-036: Airflow 3 as the Single Workflow Engine

## Context

Airflow 3 replaces the webserver with an API server, adds a DAG processor, and
uses new API and signing-key contracts. Carrying Airflow 2 beside it would add
a second middleware topology even though the stack ships no DAGs.

## Decision

Deploy one Airflow 3 topology through
`software/components/airflow/release.yaml`. The Helm chart stays on `1.22.*`
and pins `airflowVersion` and `defaultAirflowTag` to `3.2.2`. The topology uses
KubernetesExecutor, CNPG PostgreSQL, API server, scheduler, DAG processor,
triggerer, and StatsD.

The CLI-created `airflow-api-credentials` Secret supplies the admin password,
API secret, JWT secret, and fernet key through `apiSecretKeySecretName`,
`jwtSecretName`, and `fernetKeySecretName`. Flux runs the chart's migration and
user-creation Jobs without Helm hooks. The API server serves the `/airflow`
subpath without a Gateway rewrite.

Rejected: parallel Airflow 2 and 3 engines permit selection, but duplicate middleware, secrets, migrations, and routes.

Rejected: runtime package installation supports ad hoc DAG dependencies, but makes startup registry-dependent and mutable.

## Consequences

- Signing material remains stable across Flux reconciles, but the CLI owns its
  creation and rotation.
- The stack maintains one Airflow schema and component set.
- `software/stacks/osdu/services/workflow.yaml` still enables the workflow
  service's Airflow 2 client while system DAG processing is disabled; workflow
  execution through Airflow remains outside the supported path.
