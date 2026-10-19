# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The core profile's Airflow overlay: DAG loader, sources, and library lock."""

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERLAY = REPO_ROOT / "software" / "components" / "airflow-osdu"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_dags = _load_module("load_dags", OVERLAY / "load_dags.py")
lock = _load_module(
    "lock_airflow_requirements", REPO_ROOT / "scripts" / "lock_airflow_requirements.py"
)

COMMIT = "a" * 40


@pytest.fixture
def dag_dirs(tmp_path, monkeypatch):
    config = tmp_path / "config"
    target = tmp_path / "dags"
    config.mkdir()
    target.mkdir()
    monkeypatch.setattr(load_dags, "CONFIG_DIR", config)
    monkeypatch.setattr(load_dags, "TARGET_DIR", target)
    return config, target


def _remote_family(files: list[dict]) -> dict:
    return {
        "name": "core-ingestion",
        "source": {
            "type": "gitlab",
            "url": "https://gitlab.example",
            "project": "group/dags",
            "commit": COMMIT,
            "strip_prefix": "src/dags",
        },
        "files": files,
    }


def test_loader_copies_local_files_and_clears_a_previous_attempt(dag_dirs):
    config, target = dag_dirs
    (config / "echo_dag.py").write_text("# echo\n")
    (target / "stale").mkdir()
    (target / "stale" / "old.py").write_text("# old\n")
    sources = {
        "families": [
            {"name": "spi-smoke", "source": {"type": "local"}, "files": [{"path": "echo_dag.py"}]}
        ]
    }

    assert load_dags.load(sources) == 1

    assert (target / "spi-smoke" / "echo_dag.py").read_text() == "# echo\n"
    assert not (target / "stale").exists()


def test_loader_writes_remote_files_below_the_stripped_prefix(dag_dirs, monkeypatch):
    _, target = dag_dirs
    body = b"dag = 1\n"
    urls = []
    monkeypatch.setattr(load_dags, "_download", lambda url: urls.append(url) or body)
    entry = {"path": "src/dags/pkg/r3.py", "sha256": hashlib.sha256(body).hexdigest()}

    load_dags.load({"families": [_remote_family([entry])]})

    assert (target / "core-ingestion" / "pkg" / "r3.py").read_bytes() == body
    assert urls == [
        "https://gitlab.example/api/v4/projects/group%2Fdags/repository/files/"
        f"src%2Fdags%2Fpkg%2Fr3.py/raw?ref={COMMIT}"
    ]


def test_loader_rejects_a_hash_mismatch(dag_dirs, monkeypatch):
    monkeypatch.setattr(load_dags, "_download", lambda url: b"tampered\n")
    entry = {"path": "src/dags/r3.py", "sha256": "0" * 64}

    with pytest.raises(load_dags.SourceError, match="does not match pinned"):
        load_dags.load({"families": [_remote_family([entry])]})


@pytest.mark.parametrize(
    ("family", "message"),
    [
        (_remote_family([{"path": "src/dags/../../etc/x.py", "sha256": "0" * 64}]), "without '..'"),
        (_remote_family([{"path": "src/dags/r3.py"}]), "need a 64-character sha256"),
        (_remote_family([{"path": "other/r3.py", "sha256": "0" * 64}]), "outside strip_prefix"),
        (
            {
                **_remote_family([]),
                "source": {"type": "gitlab", "commit": "main"},
                "files": [{"path": "a.py", "sha256": "0" * 64}],
            },
            "full 40-character SHA",
        ),
        ({"name": "Bad_Name", "source": {"type": "local"}, "files": []}, "invalid family name"),
    ],
)
def test_loader_rejects_unsafe_sources(dag_dirs, monkeypatch, family, message):
    monkeypatch.setattr(load_dags, "_download", lambda url: b"")

    with pytest.raises(load_dags.SourceError, match=message):
        load_dags.load({"families": [family]})


def test_declared_sources_are_pinned_and_shipped():
    sources = yaml.safe_load((OVERLAY / "dag-sources.yaml").read_text(encoding="utf-8"))
    local_files = set()
    for family in sources["families"]:
        assert family["dags"], family["name"]
        source = family["source"]
        if source["type"] == "local":
            local_files.update(entry["path"] for entry in family["files"])
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])
        for entry in family["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), entry["path"]

    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text(encoding="utf-8"))
    expected = {"load_dags.py", "dag-sources.yaml", "requirements.txt", *local_files}
    file_generators = [g for g in kustomization["configMapGenerator"] if "files" in g]
    assert len(file_generators) == 2
    for generator in file_generators:
        assert set(generator["files"]) == expected, generator["name"]
        for name in generator["files"]:
            assert (OVERLAY / name).is_file(), name


def test_lock_takes_find_links_hashes_only_for_packages_without_one():
    links = ["https://gitlab.example/simple/osdu-api/"]
    pins = {
        "osdu-api==1.2.4": [("md5", "ffff")],
        "requests==2.34.2": [("sha256", "b" * 64), ("md5", "ffff")],
    }
    linked = {("osdu-api", Version("1.2.4")): ["a" * 64]}

    rendered = lock.render("3.2.2", links, pins, linked)

    assert "--require-hashes\n" in rendered
    assert f"--find-links {links[0]}\n" in rendered
    assert f"osdu-api==1.2.4 \\\n    --hash=sha256:{'a' * 64}\n" in rendered
    assert f"requests==2.34.2 \\\n    --hash=sha256:{'b' * 64}\n" in rendered
    assert "md5" not in rendered


def test_lock_fails_when_a_package_has_no_sha256():
    with pytest.raises(SystemExit, match="osdu-api==1.2.4"):
        lock.render("3.2.2", [], {"osdu-api==1.2.4": [("md5", "ffff")]}, {})


def test_committed_lock_hashes_every_pin_and_matches_its_input():
    text = (OVERLAY / "requirements.txt").read_text(encoding="utf-8")
    assert text.splitlines()[2:5] == [
        "--require-hashes",
        "--only-binary :all:",
        f"--index-url {lock.RUNTIME_INDEX}",
    ]
    assert lock.find_links(text) == lock.find_links(
        (OVERLAY / "requirements.in").read_text(encoding="utf-8")
    )
    blocks = re.findall(
        r"^(\S+==\S+) \\\n((?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", text, re.M
    )
    pinned = {pin for pin, _ in blocks}
    assert len(pinned) == len(re.findall(r"^\S+==", text, re.M))
    for requirement in ("osdu-api==1.2.4", "osdu-airflow==0.30.9", "osdu-ingestion==0.30.2"):
        assert requirement in pinned
