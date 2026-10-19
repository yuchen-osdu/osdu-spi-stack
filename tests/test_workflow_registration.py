# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The workflow registration Job: its script and its wiring to the overlay."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRATION = REPO_ROOT / "software" / "stacks" / "osdu" / "workflow-registration"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


register = _load_module("register_workflows", REGISTRATION / "register_workflows.py")

AIRFLOW = "http://airflow/airflow"
WORKFLOW = "http://workflow/api/workflow/v1"


class FakeApi:
    """Records calls and answers from a script of (method, url-suffix) -> response."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("body")))
        for (want_method, suffix), response in self.responses.items():
            if method == want_method and url.endswith(suffix):
                status, body = response() if callable(response) else response
                if status not in kwargs.get("expected", (200,)):
                    raise register.RegistrationError(f"{method} {url} -> {status}")
                return status, body
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def env(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("federated\n")
    for key, value in {
        "AIRFLOW_URL": AIRFLOW,
        "WORKFLOW_URL": WORKFLOW,
        "AIRFLOW_USERNAME": "osdu-workflow",
        "AIRFLOW_PASSWORD": "pw",
        "AZURE_TENANT_ID": "tenant",
        "AZURE_CLIENT_ID": "client",
        "AZURE_FEDERATED_TOKEN_FILE": str(token_file),
        "AZURE_RESOURCE_ID": "https://management.azure.com",
    }.items():
        monkeypatch.setenv(key, value)
    sources = tmp_path / "dag-sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "families": [
                    {"name": "spi-smoke", "dags": ["Echo_DAG"]},
                    {"name": "core-ingestion", "dags": ["Osdu_ingest"]},
                ]
            }
        )
    )
    monkeypatch.setattr(register, "SOURCES_PATH", sources)
    monkeypatch.setattr(register, "POLL_SECONDS", 0)


def _api(dag_ids, import_errors=(), register_status=201):
    return FakeApi(
        {
            ("POST", "/auth/token"): (201, {"access_token": "airflow-jwt"}),
            ("GET", "/api/v2/dags?limit=1000"): (
                200,
                {"dags": [{"dag_id": dag_id} for dag_id in dag_ids]},
            ),
            ("GET", "/api/v2/importErrors?limit=1000"): (
                200,
                {"import_errors": [{"filename": name} for name in import_errors]},
            ),
            ("POST", "/oauth2/v2.0/token"): (200, {"access_token": "osdu-jwt"}),
            ("POST", "/workflow/system"): (register_status, {}),
            ("PATCH", "/api/v2/dags/Echo_DAG"): (200, {}),
            ("PATCH", "/api/v2/dags/Osdu_ingest"): (200, {}),
        }
    )


def test_registers_then_unpauses_exactly_the_declared_dags(env, monkeypatch):
    api = _api(["Echo_DAG", "Osdu_ingest"])
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    registered = [
        body["workflowName"] for method, url, body in api.calls if url.endswith("/workflow/system")
    ]
    assert registered == ["Echo_DAG", "Osdu_ingest"]
    unpaused = [url.rsplit("/", 1)[1] for method, url, _ in api.calls if method == "PATCH"]
    assert unpaused == ["Echo_DAG", "Osdu_ingest"]
    first_patch = next(i for i, call in enumerate(api.calls) if call[0] == "PATCH")
    last_register = max(
        i for i, call in enumerate(api.calls) if call[1].endswith("/workflow/system")
    )
    assert last_register < first_patch


def test_an_existing_registration_is_not_an_error(env, monkeypatch):
    monkeypatch.setattr(register, "request", _api(["Echo_DAG", "Osdu_ingest"], register_status=409))

    assert register.main() == 0


@pytest.mark.parametrize(
    ("parsed", "errors"),
    [
        (["Echo_DAG"], ()),
        (["Echo_DAG", "Osdu_ingest", "Unexpected"], ()),
        (["Echo_DAG", "Osdu_ingest"], ("/opt/airflow/dags/core-ingestion/broken.py",)),
        (["Echo_DAG", "Osdu_ingest"], ("core-ingestion/broken.py",)),
    ],
)
def test_nothing_registers_until_the_declared_set_parses_cleanly(env, monkeypatch, parsed, errors):
    api = _api(parsed, errors)
    monkeypatch.setattr(register, "request", api)
    monkeypatch.setattr(register, "PARSE_TIMEOUT_SECONDS", 0)

    assert register.main() == 1

    assert not [call for call in api.calls if call[1].endswith("/workflow/system")]
    assert not [call for call in api.calls if call[0] == "PATCH"]


def test_import_errors_outside_the_declared_families_are_ignored(env, monkeypatch):
    api = _api(["Echo_DAG", "Osdu_ingest"], ("/opt/airflow/dags/other/broken.py", "top.py"))
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0


def test_a_dag_declared_twice_is_rejected():
    with pytest.raises(register.RegistrationError, match="declared by a and b"):
        register.declared_dags(
            {"families": [{"name": "a", "dags": ["X"]}, {"name": "b", "dags": ["X"]}]}
        )


def test_the_job_runs_the_pinned_airflow_image_with_the_overlay_inputs():
    job = yaml.safe_load((REGISTRATION / "job.yaml").read_text(encoding="utf-8"))
    release = yaml.safe_load(
        (REPO_ROOT / "software" / "components" / "airflow" / "release.yaml").read_text(
            encoding="utf-8"
        )
    )
    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == f"apache/airflow:{release['spec']['values']['airflowVersion']}"

    overlay = yaml.safe_load(
        (REPO_ROOT / "software" / "components" / "airflow-osdu" / "kustomization.yaml").read_text(
            encoding="utf-8"
        )
    )
    generated = {gen["name"]: gen for gen in overlay["configMapGenerator"]}
    volumes = {
        v["name"]: v["configMap"]["name"] for v in job["spec"]["template"]["spec"]["volumes"]
    }
    assert volumes["dag-config"] in generated
    assert generated[volumes["dag-config"]]["namespace"] == job["metadata"]["namespace"]
    assert "dag-sources.yaml" in generated[volumes["dag-config"]]["files"]
    assert generated["airflow-osdu-dags-state"]["literals"] == [
        "DAG_CONFIG_REVISION=airflow-osdu-dags-revision"
    ]
    annotations = job["spec"]["template"]["metadata"]["annotations"]
    assert annotations["spi-dag-config-revision"] == "${DAG_CONFIG_REVISION}"
