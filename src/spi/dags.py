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
by default, and the services it needs. A stack file's `dags:` list names the
add-on families to load as well. `spi up` checks each against the catalog and
against the services the stack deploys, then writes the selection to the
`spi-dags` ConfigMap, which the Airflow loader and the registration Job read
through Flux substitution. Without a selection core loads its defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import yaml

from .console import console, display_result, display_yaml
from .images import IMAGE_LOCK_NAMESPACE, IMAGE_REGISTRY
from .paths import REPO_ROOT
from .shell import kubectl_apply_yaml, run_command, run_process

DAG_SELECTION_CONFIGMAP = "spi-dags"
DAG_SELECTION_KEY = "SPI_DAG_FAMILIES"
CATALOG = REPO_ROOT / "software" / "components" / "airflow-osdu" / "dag-sources.yaml"


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


def selection_configmap(names: Sequence[str]) -> str:
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
            "data": {DAG_SELECTION_KEY: ",".join(names)},
        },
        sort_keys=False,
    )


def apply_dag_selection(names: Sequence[str]) -> None:
    """Make `spi-dags` hold these add-on families; with none, remove it."""

    if names:
        console.print("\n[bold]Writing the DAG family selection...[/bold]")
        content = selection_configmap(names)
        display_yaml(content, f"ConfigMap: {DAG_SELECTION_CONFIGMAP}")
        kubectl_apply_yaml(content, f"apply {DAG_SELECTION_CONFIGMAP} ConfigMap")
        display_result(f"{DAG_SELECTION_CONFIGMAP} selects {', '.join(names)}")
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
