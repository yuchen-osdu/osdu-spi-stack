# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The workflow registration Job: its script and its wiring to the overlay."""

import importlib.util
import sys
import urllib.error
from contextlib import nullcontext
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

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
DAGS_QUERY = "/api/v2/dags?limit=1000&exclude_stale=true"


class FakeApi:
    """Records calls and answers from a script of (method, url-suffix) -> response."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []
        self.request_kwargs: list[dict] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("body")))
        self.request_kwargs.append(kwargs)
        for (want_method, suffix), response in self.responses.items():
            if method == want_method and url.endswith(suffix):
                status, body = response() if callable(response) else response
                if status not in kwargs.get("expected", (200,)):
                    raise register.RegistrationError(f"{method} {url} -> {status}")
                return status, body
        raise AssertionError(f"unexpected {method} {url}")


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("SPI_DAG_FAMILIES", raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("federated\n")
    for key, value in {
        "AIRFLOW_URL": AIRFLOW,
        "WORKFLOW_URL": WORKFLOW,
        "DATA_PARTITION": "osdu",
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
                    {"name": "spi-smoke", "default": True, "dags": ["Echo_DAG"]},
                    {"name": "core-ingestion", "default": True, "dags": ["Osdu_ingest"]},
                ]
            }
        )
    )
    monkeypatch.setattr(register, "SOURCES_PATH", sources)
    monkeypatch.setattr(register, "POLL_SECONDS", 0)
    monkeypatch.setattr(register, "PARSE_TIMEOUT_SECONDS", 0)
    return sources


def _api(dag_ids, import_errors=(), register_status=201):
    return FakeApi(
        {
            ("POST", "/auth/token"): (201, {"access_token": "airflow-jwt"}),
            ("GET", DAGS_QUERY): (
                200,
                {"dags": [{"dag_id": dag_id} for dag_id in dag_ids]},
            ),
            ("GET", "/api/v2/importErrors?limit=1000"): (
                200,
                {"import_errors": [{"filename": name} for name in import_errors]},
            ),
            ("POST", "/oauth2/v2.0/token"): (200, {"access_token": "osdu-jwt"}),
            ("POST", "/workflow/system"): (register_status, {}),
            **{("PATCH", f"/api/v2/dags/{dag_id}"): (200, {}) for dag_id in dag_ids},
        }
    )


def _add_family(sources_path, name="optional", dags=("Optional_DAG",)):
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources["families"].append({"name": name, "dags": list(dags)})
    sources_path.write_text(yaml.safe_dump(sources), encoding="utf-8")


def _owned_workflow():
    return {
        "workflowName": "Optional_DAG",
        "description": "Optional_DAG from the optional DAG family",
    }


def _cleanup_api(workflow=None, *, read_status=200, pause_status=200, delete_status=204):
    api = _api(["Echo_DAG", "Osdu_ingest", "Optional_DAG"])
    api.responses.update(
        {
            ("GET", "/workflow/Optional_DAG"): (
                read_status,
                _owned_workflow() if workflow is None else workflow,
            ),
            ("GET", "/workflow?prefix=Optional_DAG"): (200, [_owned_workflow()]),
            ("PATCH", "/api/v2/dags/Optional_DAG"): (pause_status, {}),
            ("DELETE", "/workflow/system/Optional_DAG"): (delete_status, {}),
        }
    )
    return api


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


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("", ["spi-smoke", "core-ingestion"]),
        # Flux's default when no add-on is selected.
        ("none", ["spi-smoke", "core-ingestion"]),
        (" , \t, \n", ["spi-smoke", "core-ingestion"]),
        ("optional-first", ["optional-first", "spi-smoke", "core-ingestion"]),
        ("explicit-false", ["spi-smoke", "explicit-false", "core-ingestion"]),
        (
            " core-ingestion, explicit-false, optional-first,optional-first, ",
            ["optional-first", "spi-smoke", "explicit-false", "core-ingestion"],
        ),
    ],
)
def test_family_selection_preserves_catalog_order(requested, expected):
    sources = {
        "families": [
            {"name": "optional-first"},
            {"name": "spi-smoke", "default": True},
            {"name": "explicit-false", "default": False},
            {"name": "core-ingestion", "default": True},
        ]
    }

    assert register.selected_families(sources, requested) == expected


@pytest.mark.parametrize("requested", [None, "", "none", " , \t, \n"])
def test_defaults_only_ignore_deselected_dags(env, monkeypatch, capsys, requested):
    _add_family(env)
    if requested is not None:
        monkeypatch.setenv("SPI_DAG_FAMILIES", requested)
    api = _cleanup_api(read_status=404)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert capsys.readouterr().out.splitlines()[0] == (
        "families selected: spi-smoke, core-ingestion"
    )
    assert [
        body["workflowName"] for method, url, body in api.calls if url.endswith("/workflow/system")
    ] == ["Echo_DAG", "Osdu_ingest"]
    assert [body for method, _, body in api.calls if method == "PATCH"] == [
        {"is_paused": False},
        {"is_paused": False},
    ]
    assert not [call for call in api.calls if call[0] == "DELETE"]


@pytest.mark.parametrize("requested", ["optional", " core-ingestion, optional, optional , "])
def test_explicit_families_are_registered_and_unpaused(env, monkeypatch, capsys, requested):
    _add_family(env)
    monkeypatch.setenv("SPI_DAG_FAMILIES", requested)
    api = _api(["Echo_DAG", "Osdu_ingest", "Optional_DAG"])
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert capsys.readouterr().out.splitlines()[0] == (
        "families selected: spi-smoke, core-ingestion, optional"
    )
    assert [
        body["workflowName"] for method, url, body in api.calls if url.endswith("/workflow/system")
    ] == ["Echo_DAG", "Optional_DAG", "Osdu_ingest"]
    assert [body for method, _, body in api.calls if method == "PATCH"] == [
        {"is_paused": False},
        {"is_paused": False},
        {"is_paused": False},
    ]


def test_unknown_family_fails_before_any_api_call(env, monkeypatch, capsys):
    monkeypatch.setenv("SPI_DAG_FAMILIES", " spi-smoke, typo ")
    api = _api([])
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 1

    assert "Unknown DAG families: typo" in capsys.readouterr().err
    assert not api.calls


def test_duplicate_dags_in_deselected_families_are_still_rejected(env, monkeypatch, capsys):
    _add_family(env, dags=("Echo_DAG",))
    api = _api([])
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 1

    assert "declared by spi-smoke and optional" in capsys.readouterr().err
    assert not api.calls


def test_an_empty_selection_only_cleans_up_catalog_dags(env, monkeypatch, capsys):
    sources = yaml.safe_load(env.read_text(encoding="utf-8"))
    for family in sources["families"]:
        family["default"] = False
    env.write_text(yaml.safe_dump(sources), encoding="utf-8")
    api = _api(["Echo_DAG", "Osdu_ingest"])
    for dag_id in ("Echo_DAG", "Osdu_ingest"):
        api.responses["GET", f"/workflow/{dag_id}"] = (404, {})
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert "Airflow parsed exactly the 0 declared DAGs" in capsys.readouterr().out
    assert not [call for call in api.calls if call[1].endswith("/workflow/system")]
    assert not [call for call in api.calls if call[0] in {"PATCH", "DELETE"}]


@pytest.mark.parametrize(
    "flags",
    [
        {"is_stale": True},
        {"is_active": False},
        {"is_stale": False, "is_active": False},
    ],
)
def test_stale_or_inactive_foreign_dags_do_not_count_as_extras(env, monkeypatch, flags):
    api = _api(["Echo_DAG", "Osdu_ingest"])
    api.responses["GET", DAGS_QUERY][1]["dags"].append({"dag_id": "Foreign_DAG", **flags})
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert not [call for call in api.calls if "Foreign_DAG" in call[1]]


@pytest.mark.parametrize("flags", [{"is_stale": True}, {"is_active": False}])
def test_stale_selected_dags_do_not_satisfy_the_parse_wait(env, monkeypatch, flags):
    api = _api(["Echo_DAG", "Osdu_ingest"])
    api.responses["GET", DAGS_QUERY][1]["dags"][0].update(flags)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 1

    assert not [call for call in api.calls if call[1].endswith("/workflow/system")]
    assert not [call for call in api.calls if call[0] in {"PATCH", "DELETE"}]


def test_paused_selected_dags_are_still_live(env, monkeypatch):
    api = _api(["Echo_DAG", "Osdu_ingest"])
    for dag in api.responses["GET", DAGS_QUERY][1]["dags"]:
        dag.update(is_paused=True, is_stale=False)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0


@pytest.mark.parametrize("selected", [False, True])
def test_import_errors_only_block_selected_families(env, monkeypatch, selected):
    _add_family(env)
    if selected:
        monkeypatch.setenv("SPI_DAG_FAMILIES", "optional")
    api = _cleanup_api(read_status=404)
    api.responses["GET", "/api/v2/importErrors?limit=1000"] = (
        200,
        {"import_errors": [{"filename": "/opt/airflow/dags/optional/broken.py"}]},
    )
    monkeypatch.setattr(register, "request", api)

    assert register.main() == (1 if selected else 0)


def test_the_four_default_dag_output_lines_are_unchanged(env, monkeypatch, capsys):
    sources = yaml.safe_load(env.read_text(encoding="utf-8"))
    sources["families"][0]["dags"].append("airflow_monitoring")
    sources["families"][1]["dags"].append("manifest_ingestion")
    env.write_text(yaml.safe_dump(sources), encoding="utf-8")
    dag_ids = ["Echo_DAG", "Osdu_ingest", "airflow_monitoring", "manifest_ingestion"]
    api = _api(dag_ids, register_status=409)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert capsys.readouterr().out.splitlines() == [
        "families selected: spi-smoke, core-ingestion",
        "Airflow parsed exactly the 4 declared DAGs",
        *(f"{dag_id}: exists" for dag_id in dag_ids),
        *(f"{dag_id}: unpaused" for dag_id in dag_ids),
    ]
    assert next(
        body
        for method, url, body in api.calls
        if url.endswith("/workflow/system") and body["workflowName"] == "Echo_DAG"
    ) == {
        "workflowName": "Echo_DAG",
        "description": "Echo_DAG from the spi-smoke DAG family",
        "registrationInstructions": {},
    }


def test_cleanup_follows_all_wanted_work_and_reads_with_a_partition(env, monkeypatch, capsys):
    _add_family(env)
    api = _cleanup_api()
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert api.calls[-4:] == [
        ("GET", f"{WORKFLOW}/workflow/Optional_DAG", None),
        ("GET", f"{WORKFLOW}/workflow?prefix=Optional_DAG", None),
        ("PATCH", f"{AIRFLOW}/api/v2/dags/Optional_DAG", {"is_paused": True}),
        ("DELETE", f"{WORKFLOW}/workflow/system/Optional_DAG", None),
    ]
    assert all(
        body == {"is_paused": False} for method, _, body in api.calls[:-4] if method == "PATCH"
    )
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "Optional_DAG: paused",
        "Optional_DAG: deregistered",
    ]
    for (method, url, _), kwargs in zip(api.calls, api.request_kwargs):
        if method == "GET" and url.startswith(WORKFLOW):
            assert kwargs["partition"] == "osdu"
            assert kwargs["token"] == "osdu-jwt"
        else:
            assert "partition" not in kwargs


@pytest.mark.parametrize(
    "workflow",
    [
        {},
        {"workflowName": "Optional_DAG"},
        {"workflowName": "Optional_DAG", "description": "Manually registered"},
        {
            "workflowName": "Optional_DAG",
            "description": "Optional_DAG from the other DAG family",
        },
        {
            "workflowName": "Optional_DAG",
            "description": "Optional_DAG from the optional DAG family ",
        },
        {
            "workflowName": "Other_DAG",
            "description": "Optional_DAG from the optional DAG family",
        },
    ],
)
def test_foreign_records_are_neither_deleted_nor_paused(env, monkeypatch, capsys, workflow):
    _add_family(env)
    api = _cleanup_api(workflow)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert "Optional_DAG: not registered" in capsys.readouterr().out
    assert not [call for call in api.calls if call[0] == "DELETE"]
    assert not [call for call in api.calls if call[2] == {"is_paused": True}]


@pytest.mark.parametrize(
    ("status", "workflows"),
    [
        (404, {}),
        (200, []),
        (
            200,
            [
                _owned_workflow(),
                {"workflowName": "Optional_DAG", "description": "Foreign system workflow"},
            ],
        ),
    ],
)
def test_a_tenant_marker_cannot_authorize_cleanup_of_foreign_system_metadata(
    env, monkeypatch, capsys, status, workflows
):
    _add_family(env)
    api = _cleanup_api()
    api.responses["GET", "/workflow?prefix=Optional_DAG"] = status, workflows
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert "Optional_DAG: not registered" in capsys.readouterr().out
    assert not [call for call in api.calls if call[0] == "DELETE"]
    assert not [call for call in api.calls if call[2] == {"is_paused": True}]


def test_prefix_lookup_never_cleans_up_other_workflow_names(env, monkeypatch):
    _add_family(env)
    api = _cleanup_api()
    api.responses["GET", "/workflow?prefix=Optional_DAG"][1].append(
        {
            "workflowName": "Optional_DAG_other",
            "description": "Optional_DAG_other from the optional DAG family",
        }
    )
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    assert not [call for call in api.calls if "Optional_DAG_other" in call[1]]
    assert api.calls[-1] == ("DELETE", f"{WORKFLOW}/workflow/system/Optional_DAG", None)


@pytest.mark.parametrize(
    ("pause_status", "delete_status"),
    [(404, 204), (200, 404), (404, 404)],
)
def test_cleanup_tolerates_missing_airflow_dags_and_deleted_records(
    env, monkeypatch, capsys, pause_status, delete_status
):
    _add_family(env)
    api = _cleanup_api(pause_status=pause_status, delete_status=delete_status)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0

    output = capsys.readouterr().out
    assert ("Optional_DAG: paused" in output) == (pause_status == 200)
    assert ("Optional_DAG: deregistered" in output) == (delete_status == 204)
    assert ("Optional_DAG: not registered" in output) == (delete_status == 404)
    assert api.calls[-1][0] == "DELETE"


def test_cleanup_is_idempotent(env, monkeypatch):
    _add_family(env)
    api = _cleanup_api()
    exists = True

    def read():
        return (200, _owned_workflow()) if exists else (404, {})

    def delete():
        nonlocal exists
        exists = False
        return 204, {}

    api.responses["GET", "/workflow/Optional_DAG"] = read
    api.responses["DELETE", "/workflow/system/Optional_DAG"] = delete
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 0
    assert register.main() == 0

    assert len([call for call in api.calls if call[0] == "DELETE"]) == 1
    assert len([call for call in api.calls if call[2] == {"is_paused": True}]) == 1


def test_failed_pause_preserves_the_marker_for_a_retry(env, monkeypatch):
    _add_family(env)
    api = _cleanup_api(pause_status=403)
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 1
    assert not [call for call in api.calls if call[0] == "DELETE"]

    api.responses["PATCH", "/api/v2/dags/Optional_DAG"] = (200, {})
    assert register.main() == 0
    assert len([call for call in api.calls if call[0] == "DELETE"]) == 1


def test_cleanup_requires_a_partition_before_any_api_call(env, monkeypatch, capsys):
    _add_family(env)
    monkeypatch.delenv("DATA_PARTITION")
    api = _api([])
    monkeypatch.setattr(register, "request", api)

    assert register.main() == 1

    assert "DATA_PARTITION is required" in capsys.readouterr().err
    assert not api.calls


def test_wanted_only_registration_still_needs_no_partition(env, monkeypatch):
    monkeypatch.delenv("DATA_PARTITION")
    monkeypatch.setattr(register, "request", _api(["Echo_DAG", "Osdu_ingest"]))

    assert register.main() == 0


@pytest.mark.parametrize(
    "operation",
    [
        ("GET", "/workflow/Optional_DAG"),
        ("GET", "/workflow?prefix=Optional_DAG"),
        ("PATCH", "/api/v2/dags/Optional_DAG"),
        ("DELETE", "/workflow/system/Optional_DAG"),
    ],
)
def test_cleanup_uses_the_existing_transient_retry(env, monkeypatch, operation):
    _add_family(env)
    api = _cleanup_api()
    response = api.responses[operation]
    attempts = 0
    delays = []

    def transient_then_success():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise register._Transient("connection lost")
        return response

    api.responses[operation] = transient_then_success
    monkeypatch.setattr(register, "_send", api)
    monkeypatch.setattr(register.time, "sleep", delays.append)

    assert register.main() == 0

    assert attempts == 2
    assert delays == [5]


@pytest.mark.parametrize("partition", [None, "osdu"])
def test_send_only_attaches_the_partition_header_when_requested(monkeypatch, partition):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return nullcontext(SimpleNamespace(status=200, read=lambda: b"{}"))

    monkeypatch.setattr(register.urllib.request, "urlopen", urlopen)

    assert register._send("GET", WORKFLOW, token="jwt", partition=partition) == (200, {})

    headers = {key.lower(): value for key, value in requests[0].header_items()}
    assert headers["authorization"] == "Bearer jwt"
    assert headers.get("data-partition-id") == partition


@pytest.mark.parametrize("status", [None, 429, 500, 503])
def test_connection_throttling_and_server_errors_still_retry(monkeypatch, status):
    attempts = 0
    delays = []

    def urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if status is None:
                raise OSError("connection reset")
            raise urllib.error.HTTPError(
                request.full_url, status, "busy", Message(), BytesIO(b"{}")
            )
        return nullcontext(SimpleNamespace(status=204, read=lambda: b""))

    monkeypatch.setattr(register.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(register.time, "sleep", delays.append)

    assert register.request("DELETE", WORKFLOW, expected=(204, 404)) == (204, {})

    assert attempts == 2
    assert delays == [5]


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
