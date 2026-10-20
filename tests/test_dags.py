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

from types import SimpleNamespace

import pytest
import yaml

from spi import dags
from spi.environment import EnvironmentDeclarationError, load_stack_dags, load_stack_file

CATALOG = [
    {"name": "spi-smoke", "default": True, "dags": ["Echo_DAG"]},
    {"name": "core-ingestion", "default": True, "dags": ["Osdu_ingest"]},
    {"name": "worker-smoke", "dags": ["osdu_worker_smoke"]},
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


def test_the_selection_is_one_watched_key():
    doc = yaml.safe_load(dags.selection_configmap(("worker-smoke", "wellbore")))
    assert doc["metadata"]["name"] == "spi-dags"
    assert doc["metadata"]["namespace"] == "osdu-flux"
    assert doc["metadata"]["labels"]["reconcile.fluxcd.io/watch"] == "Enabled"
    assert doc["data"] == {"SPI_DAG_FAMILIES": "worker-smoke,wellbore"}


class TestApply:
    def test_a_selection_is_applied(self, monkeypatch):
        applied = []
        monkeypatch.setattr(dags, "kubectl_apply_yaml", lambda content, _d: applied.append(content))
        dags.apply_dag_selection(("worker-smoke",))
        assert "SPI_DAG_FAMILIES: worker-smoke" in applied[0]

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
