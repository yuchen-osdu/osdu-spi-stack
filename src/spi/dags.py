# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Add-on DAG families a stack file selects on top of core's defaults (ADR-044).

The catalog, `software/components/airflow-osdu/dag-sources.yaml`, names every
family the stack knows: its pinned source, its DAG IDs, whether core loads it
by default, the services it needs, and whether its DAGs start worker pods. A
stack file's `dags:` list names the add-on families to load as well. `spi up`
checks each against the catalog and against the services the stack deploys,
then writes the selection to the `spi-dags` ConfigMap, which the Airflow loader
and the registration Job read through Flux substitution, together with whether
the scheduler may launch pods. Without a selection core loads its defaults and
the scheduler holds no pod-launch rights.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

import yaml

from .console import console, display_result, display_yaml
from .images import IMAGE_LOCK_NAMESPACE, IMAGE_REGISTRY
from .paths import REPO_ROOT
from .shell import kubectl_apply_yaml, run_command, run_process

DAG_SELECTION_CONFIGMAP = "spi-dags"
DAG_SELECTION_KEY = "SPI_DAG_FAMILIES"
# Core's Airflow overlay reads it as the chart's allowPodLaunching.
POD_LAUNCHING_KEY = "SPI_AIRFLOW_POD_LAUNCHING"
CATALOG = REPO_ROOT / "software" / "components" / "airflow-osdu" / "dag-sources.yaml"

# Core's LocalExecutor scheduler runs every task and holds the Airflow metadata
# connection, so one query there finds a DAG's queued and running runs.
SCHEDULER = ["-n", "platform", "deploy/airflow-scheduler", "-c", "scheduler"]
ACTIVE_RUNS_PREFIX = "SPI_ACTIVE_RUNS="
ACTIVE_RUNS_SCRIPT = (
    "import json, sys; "
    "from airflow.models import DagRun; "
    "from airflow.utils.session import create_session; "
    "from airflow.utils.state import DagRunState; "
    "session = create_session().__enter__(); "
    "rows = session.query(DagRun.dag_id, DagRun.state).filter("
    "DagRun.dag_id.in_(sys.argv[1:]), "
    "DagRun.state.in_([DagRunState.RUNNING, DagRunState.QUEUED])).all(); "
    f"print('{ACTIVE_RUNS_PREFIX}' + json.dumps(sorted("
    "[row[0], str(getattr(row[1], 'value', row[1]))] for row in rows)))"
)


class DagSelectionError(ValueError):
    """A `dags:` list the stack cannot load as given."""


def load_catalog(path: Path = CATALOG) -> list[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise DagSelectionError(
            f"the DAG catalog is not readable at {path}: run spi from a stack checkout"
        ) from exc
    families = data.get("families") or []
    if not isinstance(families, list):
        raise DagSelectionError(f"{path}: families must be a list")
    return families


def deployed_services() -> set[str]:
    """Every service the stack deploys on core, by its image registry name."""

    return {
        name
        for name, entry in IMAGE_REGISTRY.items()
        if entry.file.startswith(("services/", "services-reference/", "services-ddms/"))
    }


def check_selection(
    names: Sequence[str],
    catalog: Iterable[dict],
    services: set[str],
) -> tuple[str, ...]:
    """The add-on families to write, in catalog order; every problem at once."""

    families = {family.get("name"): family for family in catalog}
    problems: list[str] = []
    for family in families.values():
        workers = family.get("workers", False)
        if not isinstance(workers, bool):
            problems.append(f"catalog: {family.get('name')} workers must be true or false")
        elif workers and family.get("default"):
            problems.append(
                f"catalog: {family.get('name')} is a default family that declares workers;"
                " core grants no pod launching"
            )
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not name:
            problems.append(f"dags: {name!r} is not a family name")
            continue
        if name in seen:
            problems.append(f"dags: {name} is listed twice")
        seen.add(name)
        family = families.get(name)
        if family is None:
            problems.append(
                f"dags: unknown family {name!r}; the catalog has "
                + ", ".join(sorted(str(n) for n in families))
            )
            continue
        if family.get("default"):
            problems.append(f"dags: {name} is a default family; core always loads it")
            continue
        missing = [s for s in family.get("requires") or [] if s not in services]
        if missing:
            problems.append(
                f"dags: {name} requires {', '.join(missing)}, which this stack does not deploy"
            )
    if problems:
        raise DagSelectionError("; ".join(problems))
    return tuple(str(f["name"]) for f in families.values() if f.get("name") in seen)


def launches_pods(names: Sequence[str], catalog: Iterable[dict]) -> bool:
    """Whether any selected family's DAGs start KubernetesPodOperator workers."""

    selected = set(names)
    return any(
        family.get("workers") is True for family in catalog if family.get("name") in selected
    )


def current_selection() -> tuple[str, ...]:
    """The add-on families `spi-dags` selects now; none when it is absent."""

    result = run_process(
        [
            "kubectl",
            "get",
            "configmap",
            DAG_SELECTION_CONFIGMAP,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or "kubectl failed"
        raise DagSelectionError(f"could not read {DAG_SELECTION_CONFIGMAP}: {detail}")
    text = (result.stdout or "").strip()
    if not text:
        return ()
    value = (json.loads(text).get("data") or {}).get(DAG_SELECTION_KEY, "")
    return tuple(name for name in (part.strip() for part in value.split(",")) if name)


def active_runs(dag_ids: Sequence[str]) -> list[tuple[str, str]] | None:
    """Queued or running runs of these DAGs; None when the scheduler is unreachable."""

    result = run_process(
        ["kubectl", "exec", *SCHEDULER, "--", "python", "-c", ACTIVE_RUNS_SCRIPT, *dag_ids],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    for line in reversed((result.stdout or "").splitlines()):
        if line.startswith(ACTIVE_RUNS_PREFIX):
            found = json.loads(line[len(ACTIVE_RUNS_PREFIX) :])
            return [(str(dag_id), str(state)) for dag_id, state in found]
    return None


def check_dag_removal(names: Sequence[str], catalog: Iterable[dict] | None = None) -> None:
    """Refuse to drop an add-on family while one of its DAGs has queued or running runs.

    The new selection restarts the scheduler, which ends every task under
    LocalExecutor, so removal waits for quiet rather than cancelling work. A
    scheduler that cannot be reached runs no task, so removal then proceeds.
    """

    families = list(load_catalog() if catalog is None else catalog)
    removed = set(current_selection()) - set(names)
    owner = {
        dag_id: str(family.get("name"))
        for family in families
        if family.get("name") in removed
        for dag_id in family.get("dags") or []
    }
    if not owner:
        return
    runs = active_runs(sorted(owner))
    if runs is None:
        console.print(
            "[warning]Could not reach the Airflow scheduler to check the removed families'"
            " runs; it is running no task, so they are removed.[/warning]"
        )
        return
    if runs:
        busy = sorted({owner[dag_id] for dag_id, _ in runs if dag_id in owner})
        detail = ", ".join(f"{dag_id} {state}" for dag_id, state in runs)
        raise DagSelectionError(
            f"removal_busy: {', '.join(busy)} has active runs ({detail}); let them finish or"
            " mark them failed, then rerun. The DAG selection is unchanged and nothing was"
            " cancelled."
        )


def selection_configmap(names: Sequence[str], pod_launching: bool = False) -> str:
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": DAG_SELECTION_CONFIGMAP,
                "namespace": IMAGE_LOCK_NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "osdu-spi-stack",
                    # Flux re-reconciles the Kustomizations that read it on change.
                    "reconcile.fluxcd.io/watch": "Enabled",
                },
            },
            "data": {
                DAG_SELECTION_KEY: ",".join(names),
                POD_LAUNCHING_KEY: "true" if pod_launching else "false",
            },
        },
        sort_keys=False,
    )


def apply_dag_selection(names: Sequence[str], catalog: Iterable[dict] | None = None) -> None:
    """Make `spi-dags` hold these add-on families; with none, remove it.

    A default family never declares workers, so removing the ConfigMap also
    leaves the scheduler without pod-launch rights.
    """

    if names:
        console.print("\n[bold]Writing the DAG family selection...[/bold]")
        pod_launching = launches_pods(names, load_catalog() if catalog is None else catalog)
        content = selection_configmap(names, pod_launching)
        display_yaml(content, f"ConfigMap: {DAG_SELECTION_CONFIGMAP}")
        kubectl_apply_yaml(content, f"apply {DAG_SELECTION_CONFIGMAP} ConfigMap")
        launch = "; the scheduler may launch worker pods" if pod_launching else ""
        display_result(f"{DAG_SELECTION_CONFIGMAP} selects {', '.join(names)}{launch}")
        return
    present = run_process(
        [
            "kubectl",
            "get",
            "configmap",
            DAG_SELECTION_CONFIGMAP,
            "-n",
            IMAGE_LOCK_NAMESPACE,
            "--ignore-not-found",
            "-o",
            "name",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if present.returncode != 0:
        detail = (present.stderr or "").strip() or "kubectl failed"
        raise DagSelectionError(f"could not read {DAG_SELECTION_CONFIGMAP}: {detail}")
    if (present.stdout or "").strip():
        run_command(
            ["kubectl", "delete", "configmap", DAG_SELECTION_CONFIGMAP, "-n", IMAGE_LOCK_NAMESPACE],
            description="remove the DAG selection; core loads its default families",
        )
        display_result(f"{DAG_SELECTION_CONFIGMAP} ConfigMap removed")
