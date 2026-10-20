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

"""A stack file's dags: list becomes the spi-dags selection (ADR-044)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from spi import dags
from spi.environment import EnvironmentDeclarationError, load_stack_dags, load_stack_file

CATALOG = [
    {"name": "spi-smoke", "default": True, "dags": ["Echo_DAG"]},
    {"name": "core-ingestion", "default": True, "dags": ["Osdu_ingest"]},
    {"name": "worker-smoke", "workers": True, "dags": ["osdu_worker_smoke"]},
    {"name": "by-reference", "requires": ["dataset"], "dags": ["Osdu_ingest_by_reference"]},
    {"name": "wellbore", "requires": ["storage", "search"], "dags": ["wellbore_x"]},
]


def test_add_ons_come_back_in_catalog_order():
    selected = dags.check_selection(["wellbore", "worker-smoke"], CATALOG, {"storage", "search"})
    assert selected == ("worker-smoke", "wellbore")


def test_no_add_ons_selects_nothing():
    assert dags.check_selection([], CATALOG, set()) == ()


@pytest.mark.parametrize(
    ("names", "refusal"),
    [
        (["nope"], "unknown family 'nope'"),
        (["spi-smoke"], "spi-smoke is a default family"),
        (["by-reference"], "by-reference requires dataset, which this stack does not deploy"),
        (["worker-smoke", "worker-smoke"], "worker-smoke is listed twice"),
        ([""], "is not a family name"),
    ],
)
def test_a_selection_the_stack_cannot_load_is_refused(names, refusal):
    with pytest.raises(dags.DagSelectionError, match=refusal):
        dags.check_selection(names, CATALOG, {"storage", "search"})


def test_every_problem_is_reported_at_once():
    with pytest.raises(dags.DagSelectionError) as caught:
        dags.check_selection(["nope", "by-reference"], CATALOG, set())
    assert "unknown family 'nope'" in str(caught.value)
    assert "requires dataset" in str(caught.value)


def test_the_stack_deploys_the_core_and_reference_services_but_no_dataset():
    services = dags.deployed_services()
    assert {"storage", "search", "workflow", "unit"} <= services
    assert "dataset" not in services


@pytest.mark.parametrize(
    ("family", "refusal"),
    [
        (
            {"name": "core-x", "default": True, "workers": True, "dags": ["x"]},
            "core-x is a default family that declares workers",
        ),
        ({"name": "odd", "workers": "yes", "dags": ["x"]}, "odd workers must be true or false"),
    ],
)
def test_a_catalog_that_would_grant_pod_launching_wrongly_is_refused(family, refusal):
    with pytest.raises(dags.DagSelectionError, match=refusal):
        dags.check_selection([], [*CATALOG, family], set())


def test_only_a_selected_worker_family_launches_pods():
    assert dags.launches_pods(("worker-smoke",), CATALOG)
    assert dags.launches_pods(("wellbore", "worker-smoke"), CATALOG)
    assert not dags.launches_pods(("wellbore",), CATALOG)
    assert not dags.launches_pods((), CATALOG)


def test_the_selection_is_two_watched_keys():
    doc = yaml.safe_load(dags.selection_configmap(("worker-smoke", "wellbore")))
    assert doc["metadata"]["name"] == "spi-dags"
    assert doc["metadata"]["namespace"] == "osdu-flux"
    assert doc["metadata"]["labels"]["reconcile.fluxcd.io/watch"] == "Enabled"
    assert doc["data"] == {
        "SPI_DAG_FAMILIES": "worker-smoke,wellbore",
        "SPI_AIRFLOW_POD_LAUNCHING": "false",
    }
    launching = yaml.safe_load(dags.selection_configmap(("worker-smoke",), pod_launching=True))
    assert launching["data"]["SPI_AIRFLOW_POD_LAUNCHING"] == "true"


class TestRemoval:
    """Removing a family restarts the scheduler, which would end its runs."""

    @pytest.fixture
    def cluster(self, monkeypatch):
        execs: list[list[str]] = []
        state = {"selected": "worker-smoke,wellbore", "exec": None, "execs": execs}

        def run_process(argv, **_kwargs):
            if argv[1] == "get":
                data = {"data": {"SPI_DAG_FAMILIES": state["selected"]}}
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(data) if state["selected"] else ""
                )
            execs.append(argv[argv.index("-c", argv.index("--")) + 2 :])
            return state["exec"]

        monkeypatch.setattr(dags, "run_process", run_process)
        return state

    @staticmethod
    def _answer(rows, returncode=0):
        log = "2026-10-20T21:36:11Z [info] setup plugin alembic\n"
        out = log + dags.ACTIVE_RUNS_PREFIX + json.dumps(rows) + "\n"
        return SimpleNamespace(returncode=returncode, stdout=out)

    def test_a_kept_selection_asks_airflow_nothing(self, cluster):
        dags.check_dag_removal(("worker-smoke", "wellbore", "by-reference"), CATALOG)
        assert cluster["execs"] == []

    def test_no_previous_selection_removes_nothing(self, cluster):
        cluster["selected"] = ""
        dags.check_dag_removal((), CATALOG)
        assert cluster["execs"] == []

    def test_a_quiet_family_is_removed(self, cluster):
        cluster["exec"] = self._answer([])
        dags.check_dag_removal(("wellbore",), CATALOG)
        assert cluster["execs"] == [["osdu_worker_smoke"]]

    def test_a_family_with_active_runs_is_refused(self, cluster):
        cluster["exec"] = self._answer([["osdu_worker_smoke", "queued"]])
        with pytest.raises(dags.DagSelectionError) as caught:
            dags.check_dag_removal((), CATALOG)
        assert "removal_busy: worker-smoke has active runs (osdu_worker_smoke queued)" in str(
            caught.value
        )
        assert cluster["execs"] == [["osdu_worker_smoke", "wellbore_x"]]

    @pytest.mark.parametrize(
        "answer",
        [
            SimpleNamespace(returncode=1, stdout="", stderr="pod not found"),
            SimpleNamespace(returncode=0, stdout="no marker\n"),
        ],
    )
    def test_an_unreachable_scheduler_runs_no_task_so_removal_proceeds(self, cluster, answer):
        cluster["exec"] = answer
        dags.check_dag_removal((), CATALOG)

    def test_an_unreadable_selection_is_not_read_as_none(self, monkeypatch):
        monkeypatch.setattr(
            dags,
            "run_process",
            lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="refused"),
        )
        with pytest.raises(dags.DagSelectionError, match="refused"):
            dags.check_dag_removal((), CATALOG)

    def test_the_query_runs_in_the_scheduler_and_counts_only_active_states(self):
        script = dags.ACTIVE_RUNS_SCRIPT
        assert "DagRunState.RUNNING, DagRunState.QUEUED" in script
        assert dags.SCHEDULER == ["-n", "platform", "deploy/airflow-scheduler", "-c", "scheduler"]
        compile(script.replace("; ", "\n"), "active_runs", "exec")


class TestApply:
    def test_a_selection_is_applied(self, monkeypatch):
        applied = []
        monkeypatch.setattr(dags, "kubectl_apply_yaml", lambda content, _d: applied.append(content))
        dags.apply_dag_selection(("wellbore",), CATALOG)
        assert "SPI_DAG_FAMILIES: wellbore" in applied[0]
        assert "SPI_AIRFLOW_POD_LAUNCHING: 'false'" in applied[0]

    def test_a_worker_family_lets_the_scheduler_launch_pods(self, monkeypatch):
        applied = []
        monkeypatch.setattr(dags, "kubectl_apply_yaml", lambda content, _d: applied.append(content))
        dags.apply_dag_selection(("worker-smoke",), CATALOG)
        assert "SPI_AIRFLOW_POD_LAUNCHING: 'true'" in applied[0]

    def test_no_selection_removes_an_existing_configmap(self, monkeypatch):
        commands = []
        monkeypatch.setattr(
            dags,
            "run_process",
            lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="configmap/spi-dags\n"),
        )
        monkeypatch.setattr(dags, "run_command", lambda cmd, **_k: commands.append(cmd))
        dags.apply_dag_selection(())
        assert commands == [["kubectl", "delete", "configmap", "spi-dags", "-n", "osdu-flux"]]

    def test_no_selection_and_no_configmap_change_nothing(self, monkeypatch):
        monkeypatch.setattr(
            dags, "run_process", lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="")
        )
        monkeypatch.setattr(dags, "run_command", lambda *_a, **_k: pytest.fail("nothing to delete"))
        dags.apply_dag_selection(())

    def test_an_unreadable_cluster_is_not_read_as_empty(self, monkeypatch):
        monkeypatch.setattr(
            dags,
            "run_process",
            lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="refused"),
        )
        with pytest.raises(dags.DagSelectionError, match="refused"):
            dags.apply_dag_selection(())


class TestStackFile:
    def _write(self, tmp_path, text):
        path = tmp_path / "stack.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_dags_and_resources_live_side_by_side(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dags, "load_catalog", lambda *_a: CATALOG)
        path = self._write(
            tmp_path, "resources:\n  legal: {replicas: 1}\ndags:\n  - worker-smoke\n"
        )
        assert load_stack_file(path) == {"LEGAL_REPLICAS": "1"}
        assert load_stack_dags(path) == ("worker-smoke",)

    def test_no_dags_selects_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dags, "load_catalog", lambda *_a: CATALOG)
        assert load_stack_dags(self._write(tmp_path, "resources: {}\n")) == ()
        assert load_stack_dags(self._write(tmp_path, "dags:\n")) == ()

    def test_a_refused_family_names_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dags, "load_catalog", lambda *_a: CATALOG)
        path = self._write(tmp_path, "dags: [by-reference]\n")
        with pytest.raises(EnvironmentDeclarationError, match="stack.yaml: dags: by-reference"):
            load_stack_dags(path)

    def test_dags_must_be_a_list_of_names(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dags, "load_catalog", lambda *_a: CATALOG)
        with pytest.raises(EnvironmentDeclarationError):
            load_stack_dags(self._write(tmp_path, "dags: {worker-smoke: true}\n"))


def test_every_catalog_requirement_names_a_lowercase_service():
    for family in dags.load_catalog():
        for service in family.get("requires") or []:
            assert service == service.lower() and service.replace("-", "").isalnum(), family


def test_the_example_stack_file_selects_the_example_family():
    path = dags.REPO_ROOT / "examples" / "stacks" / "core-with-worker-smoke.yaml"
    names = load_stack_dags(path)
    assert names == ("osdu-worker-smoke",)
    assert dags.check_selection(names, dags.load_catalog(), dags.deployed_services()) == names


def test_the_shipped_by_reference_family_is_refused_without_dataset():
    with pytest.raises(dags.DagSelectionError, match="requires dataset"):
        dags.check_selection(
            ["ingestion-by-reference"], dags.load_catalog(), dags.deployed_services()
        )


def test_only_the_core_families_load_by_default():
    defaults = [f["name"] for f in dags.load_catalog() if f.get("default") is True]
    assert defaults == ["spi-smoke", "core-ingestion"]


def test_the_example_worker_family_is_the_only_shipped_one_that_launches_pods():
    catalog = dags.load_catalog()
    assert [f["name"] for f in catalog if f.get("workers")] == ["osdu-worker-smoke"]
    assert not dags.launches_pods([f["name"] for f in catalog if f.get("default")], catalog)
    example = dags.REPO_ROOT / "examples" / "stacks" / "core-with-worker-smoke.yaml"
    assert dags.launches_pods(load_stack_dags(example), catalog)
