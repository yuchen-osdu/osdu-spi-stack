# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The core profile's Airflow overlay: DAG loader, sources, and library lock."""

import ast
import hashlib
import http.client
import importlib.util
import io
import re
import sys
import tarfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

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
pin_script = _load_module("pin_dag_family", REPO_ROOT / "scripts" / "pin_dag_family.py")

COMMIT = "a" * 40
# GitLab names an archive's top folder <project>-<sha>-<sha>, GitHub <repo>-<sha>.
TOP = f"dags-{COMMIT}-{COMMIT}"
NO_DIGEST = "sha256:" + "0" * 64
PINNED_IMAGE = "ghcr.io/azure/osdu-spi-csv-parser@sha256:" + "c" * 64


@pytest.fixture
def dag_dirs(tmp_path, monkeypatch):
    config = tmp_path / "config"
    target = tmp_path / "dags"
    config.mkdir()
    target.mkdir()
    monkeypatch.setattr(load_dags, "CONFIG_DIR", config)
    monkeypatch.setattr(load_dags, "TARGET_DIR", target)
    return config, target


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remote_family(files: list[dict]) -> dict:
    return {
        "name": "core-ingestion",
        "default": True,
        "source": {
            "type": "gitlab",
            "url": "https://gitlab.example",
            "project": "group/dags",
            "commit": COMMIT,
            "strip_prefix": "src/dags",
        },
        "files": files,
    }


def _local_family(name: str, **extra) -> dict:
    return {"name": name, "source": {"type": "local"}, "files": [{"path": f"{name}.py"}], **extra}


def _archive_family(host: str = "gitlab", **source) -> dict:
    location = (
        {"url": "https://gitlab.example", "project": "group/dags"}
        if host == "gitlab"
        else {"repository": "owner/dags"}
    )
    return {
        "name": "add-on",
        "default": True,
        "source": {
            "type": "archive",
            "host": host,
            **location,
            "commit": COMMIT,
            "strip_prefix": "src/dags",
            "include": ["*.py"],
            **source,
        },
        "dags": ["add_on"],
    }


def _tar_gz(entries, pax: dict | None = None) -> bytes:
    """A tar.gz as the hosts serve one: each member name maps to bytes for a
    file, None for a folder, or a (type, target) pair for a link."""
    items = entries.items() if isinstance(entries, dict) else entries
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=pax or {}
    ) as archive:
        for name, body in items:
            info = tarfile.TarInfo(name)
            if body is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif isinstance(body, tuple):
                info.type, info.linkname = body
                archive.addfile(info)
            else:
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _digest(files: dict[str, bytes]) -> str:
    """The archive pin, restated from its definition rather than taken from the loader."""
    lines = "".join(f"{_sha256(files[path])}  {path}\n" for path in sorted(files))
    return f"sha256:{_sha256(lines.encode())}"


def test_loader_copies_local_files_and_clears_a_previous_attempt(dag_dirs):
    config, target = dag_dirs
    (config / "echo_dag.py").write_text("# echo\n")
    (target / "stale").mkdir()
    (target / "stale" / "old.py").write_text("# old\n")
    sources = {
        "families": [
            {
                "name": "spi-smoke",
                "default": True,
                "source": {"type": "local"},
                "files": [{"path": "echo_dag.py"}],
            }
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


# Standard values for the render tests; the shipped ones are checked further down.
VALUES = {
    "worker_env": '{"STORAGE": "{{ var.value.core__service__storage__url }}"}',
    "worker_pod_kwargs": '{"container_resources": k8s_models.V1ResourceRequirements()}',
    "unpinned_image": "ghcr.io/azure/osdu-spi-csv-parser:latest",
}


@pytest.mark.parametrize(
    ("family", "message"),
    [
        ({**_local_family("a"), "default": "yes"}, "a: default must be true or false"),
        ({**_local_family("a"), "requires": ["CSV_Parser"]}, "requires must list lowercase"),
        ({**_local_family("a"), "defualt": True}, "has unknown keys: defualt"),
        (_local_family("a", files=[{"path": "dir/a.py"}]), "cannot nest"),
        (_local_family("a", files=[]), "files must be a non-empty list"),
        (_remote_family([{"path": "src/dags/a.py", "sha256": "0" * 64}] * 2), "two files land"),
        ({**_archive_family(digest=NO_DIGEST), "files": []}, "with include, not files"),
        (_archive_family(digest=NO_DIGEST, include=[]), "include must be a non-empty list"),
        (_archive_family(digest=NO_DIGEST, include=["**/*.py"]), "relative globs without"),
        (_archive_family(), "needs digest: sha256:<64 hex>"),
        (_archive_family(digest="sha256:abc"), "needs digest: sha256:<64 hex>"),
        (_archive_family(digest=NO_DIGEST, commit="main"), "full 40-character SHA"),
        (_archive_family(digest=NO_DIGEST, url="gitlab.example"), "url must be an http(s) URL"),
        (_archive_family(digest=NO_DIGEST, ref="main"), "has unknown keys: ref"),
        (_archive_family("bitbucket", digest=NO_DIGEST), "host must be gitlab or github"),
        (_archive_family("github", digest=NO_DIGEST, repository="../x"), "owner/name"),
        ({**_local_family("a"), "render": {"dag_name": "x"}}, "must be upper case"),
        ({**_local_family("a"), "render": {"RETRIES": 3}}, "RETRIES must be text"),
        ({**_local_family("a"), "render": {"ENV_VARS": "@nope"}}, "unknown standard value @nope"),
        (
            {**_local_family("a"), "render": {"DOCKER_IMAGE": "ghcr.io/azure/csv:latest"}},
            "DOCKER_IMAGE must pin its image",
        ),
        (
            {**_local_family("a"), "render": {"WORKER_IMAGE": "@unpinned_image"}},
            "WORKER_IMAGE must pin its image",
        ),
    ],
)
def test_loader_refuses_a_malformed_family_before_fetching_anything(
    dag_dirs, monkeypatch, family, message
):
    monkeypatch.setattr(load_dags, "_download", lambda url: pytest.fail(f"fetched {url}"))

    with pytest.raises(load_dags.SourceError, match=re.escape(message)):
        load_dags.load({"families": [family]}, values=VALUES)


def test_loader_refuses_a_malformed_catalog(dag_dirs):
    with pytest.raises(load_dags.SourceError, match="names must be unique: a"):
        load_dags.load({"families": [_local_family("a"), _local_family("a")]})
    with pytest.raises(load_dags.SourceError, match="unknown keys: version"):
        load_dags.load({"families": [], "version": 1})


@pytest.fixture
def three_families(dag_dirs):
    config, _ = dag_dirs
    for name in ("a", "b", "c"):
        (config / f"{name}.py").write_text(f"# {name}\n")
    return {
        "families": [
            _local_family("c", default=True),
            _local_family("b"),
            _local_family("a", default=True),
        ]
    }


@pytest.mark.parametrize(
    ("requested", "selected"),
    [("", "c, a"), ("b", "c, b, a"), (" b , ,a ", "c, b, a"), (" , ", "c, a")],
)
def test_loader_takes_the_defaults_and_the_requested_families_in_catalog_order(
    three_families, dag_dirs, capsys, requested, selected
):
    _, target = dag_dirs
    names = selected.split(", ")

    assert load_dags.load(three_families, requested) == len(names)

    assert sorted(child.name for child in target.iterdir()) == sorted(names)
    out = capsys.readouterr().out
    assert out.startswith(f"families selected: {selected}\nfamily {names[0]} (local)\n")


def test_an_unknown_requested_family_fails_before_anything_is_written(three_families, dag_dirs):
    _, target = dag_dirs
    (target / "previous.py").write_text("# from an earlier attempt\n")

    with pytest.raises(
        load_dags.SourceError, match=r"unknown families: nope \(the catalog has c, b, a\)"
    ):
        load_dags.load(three_families, "b, nope")

    assert [child.name for child in target.iterdir()] == ["previous.py"]


def test_a_family_that_is_not_selected_is_never_fetched(dag_dirs, monkeypatch):
    config, _ = dag_dirs
    (config / "a.py").write_text("# a\n")
    add_on = {**_remote_family([{"path": "src/dags/r3.py", "sha256": "0" * 64}]), "default": False}
    monkeypatch.setattr(load_dags, "_download", lambda url: pytest.fail(f"fetched {url}"))

    assert load_dags.load({"families": [_local_family("a", default=True), add_on]}) == 1


def test_main_reads_the_selection_from_spi_dag_families(dag_dirs, monkeypatch, capsys):
    config, _ = dag_dirs
    for name in ("a", "b"):
        (config / f"{name}.py").write_text(f"# {name}\n")
    catalog = {"families": [_local_family("a", default=True), _local_family("b")]}
    (config / "dag-sources.yaml").write_text(yaml.safe_dump(catalog), encoding="utf-8")

    monkeypatch.delenv("SPI_DAG_FAMILIES", raising=False)
    assert load_dags.main() == 0
    assert "families selected: a\n" in capsys.readouterr().out

    monkeypatch.setenv("SPI_DAG_FAMILIES", "b")
    assert load_dags.main() == 0
    assert "loaded 2 DAG files into" in capsys.readouterr().out

    monkeypatch.setenv("SPI_DAG_FAMILIES", "nope")
    assert load_dags.main() == 1
    assert "DAG load failed: SPI_DAG_FAMILIES names unknown families: nope" in (
        capsys.readouterr().err
    )


def test_the_shipped_catalog_loads_its_default_families_as_documented(
    dag_dirs, monkeypatch, capsys
):
    _, target = dag_dirs
    monkeypatch.setattr(load_dags, "CONFIG_DIR", OVERLAY)
    monkeypatch.delenv("SPI_DAG_FAMILIES", raising=False)
    content = load_dags._content
    # The local files are the real ones; the remote ones stand in for their pinned bytes.
    monkeypatch.setattr(
        load_dags,
        "_content",
        lambda family, entry: (
            content(family, entry) if family["source"]["type"] == "local" else b"# upstream\n"
        ),
    )

    assert load_dags.main() == 0

    assert capsys.readouterr().out == (
        "families selected: spi-smoke, core-ingestion\n"
        "family spi-smoke (local)\n"
        "  spi-smoke/echo_dag.py\n"
        "  spi-smoke/airflow_monitoring.py\n"
        "family core-ingestion (gitlab)\n"
        "  core-ingestion/osdu-ingest-r3.py\n"
        "  core-ingestion/manifest_ingestion.py\n"
        f"loaded 4 DAG files into {target}\n"
    )


@pytest.mark.parametrize(
    ("host", "top", "pax", "url"),
    [
        (
            "gitlab",
            TOP,
            None,
            "https://gitlab.example/api/v4/projects/group%2Fdags/repository/archive.tar.gz"
            f"?sha={COMMIT}",
        ),
        # git archive opens GitHub's tarballs with a pax global header.
        (
            "github",
            f"dags-{COMMIT}",
            {"comment": COMMIT},
            f"https://codeload.github.com/owner/dags/tar.gz/{COMMIT}",
        ),
    ],
)
def test_loader_takes_an_archive_familys_included_files_below_the_prefix(
    dag_dirs, monkeypatch, capsys, host, top, pax, url
):
    _, target = dag_dirs
    files = {"r3.py": b"r3 = 1\n", "pkg/helper.py": b"helper = 1\n"}
    archive = _tar_gz(
        {
            f"{top}/": None,
            f"{top}/README.md": b"# dags\n",
            f"{top}/src/dags/": None,
            f"{top}/src/dags/r3.py": files["r3.py"],
            f"{top}/src/dags/pkg/": None,
            f"{top}/src/dags/pkg/helper.py": files["pkg/helper.py"],
            f"{top}/src/dags/pkg/schema.json": b"{}\n",
            f"{top}/tests/test_r3.py": b"# outside the prefix\n",
        },
        pax,
    )
    urls = []
    monkeypatch.setattr(load_dags, "_download", lambda u: urls.append(u) or archive)

    assert load_dags.load({"families": [_archive_family(host, digest=_digest(files))]}) == 2

    assert urls == [url]
    written = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert written == {"add-on/r3.py", "add-on/pkg/helper.py"}
    assert (target / "add-on" / "pkg" / "helper.py").read_bytes() == files["pkg/helper.py"]
    out = capsys.readouterr().out
    assert f"family add-on ({host} archive)\n  add-on/pkg/helper.py\n  add-on/r3.py\n" in out


def test_archive_include_globs_match_the_path_below_the_prefix_from_the_right(monkeypatch):
    archive = _tar_gz(
        {
            f"{TOP}/src/dags/eds_ingest/ingest.py": b"1",
            f"{TOP}/src/dags/eds_ingest/sub/deep.py": b"2",
            f"{TOP}/src/dags/eds_scheduler/schedule.py": b"3",
            f"{TOP}/src/dags/eds_naturalization/naturalize.py": b"4",
            f"{TOP}/src/dags/settings.yaml": b"5",
            f"{TOP}/src/other/eds_ingest/elsewhere.py": b"6",
        }
    )
    monkeypatch.setattr(load_dags, "_download", lambda url: archive)
    include = ["eds_ingest/*.py", "eds_scheduler/*.py", "settings.yaml"]

    files = load_dags.fetch_archive(_archive_family(include=include)["source"])

    assert list(files) == ["eds_ingest/ingest.py", "eds_scheduler/schedule.py", "settings.yaml"]


def test_an_archive_digest_mismatch_names_both_digests(dag_dirs, monkeypatch):
    archive = _tar_gz({f"{TOP}/src/dags/r3.py": b"tampered\n"})
    monkeypatch.setattr(load_dags, "_download", lambda url: archive)
    actual = _digest({"r3.py": b"tampered\n"})

    with pytest.raises(
        load_dags.SourceError,
        match=f"add-on: archive digest {actual} does not match pinned {NO_DIGEST}",
    ):
        load_dags.load({"families": [_archive_family(digest=NO_DIGEST)]})


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ({f"{TOP}/src/dags/README.md": b"x"}, "no regular file below strip_prefix matches"),
        ({f"{TOP}/src/dags/../../../../etc/cron.py": b"x"}, "escapes the archive"),
        ({"/etc/cron.py": b"x"}, "escapes the archive"),
        ({f"{TOP}/src/dags/../link.py": (tarfile.SYMTYPE, "r3.py")}, "escapes the archive"),
        ({f"{TOP}/src/dags/a.py": b"x", f"other-{COMMIT}/src/dags/b.py": b"x"}, "is outside"),
        ([(f"{TOP}/src/dags/a.py", b"1"), (f"{TOP}/src/dags/a.py", b"2")], "appears twice"),
    ],
)
def test_loader_refuses_an_unsafe_or_empty_archive(dag_dirs, monkeypatch, entries, message):
    archive = _tar_gz(entries)
    monkeypatch.setattr(load_dags, "_download", lambda url: archive)

    with pytest.raises(load_dags.SourceError, match=message):
        load_dags.load({"families": [_archive_family(digest=NO_DIGEST)]})


def test_archive_links_are_skipped_never_followed(dag_dirs, monkeypatch):
    _, target = dag_dirs
    body = b"r3 = 1\n"
    archive = _tar_gz(
        {
            f"{TOP}/src/dags/r3.py": body,
            f"{TOP}/src/dags/passwd.py": (tarfile.SYMTYPE, "/etc/passwd"),
            f"{TOP}/src/dags/copy.py": (tarfile.LNKTYPE, f"{TOP}/src/dags/r3.py"),
        }
    )
    monkeypatch.setattr(load_dags, "_download", lambda url: archive)

    family = _archive_family(digest=_digest({"r3.py": body}))
    assert load_dags.load({"families": [family]}) == 1

    assert [child.name for child in (target / "add-on").iterdir()] == ["r3.py"]


def test_archive_files_are_capped_but_members_include_skips_are_not(monkeypatch):
    monkeypatch.setattr(load_dags, "_EXTRACT_LIMIT_MIB", 1)
    source = _archive_family()["source"]
    skipped = _tar_gz(
        {f"{TOP}/src/dags/a.py": b"a = 1\n", f"{TOP}/src/dags/data.bin": bytes(2 << 20)}
    )
    monkeypatch.setattr(load_dags, "_download", lambda url: skipped)
    assert list(load_dags.fetch_archive(source)) == ["a.py"]

    half = bytes((1 << 19) + 1)
    selected = _tar_gz({f"{TOP}/src/dags/a.py": half, f"{TOP}/src/dags/b.py": half})
    monkeypatch.setattr(load_dags, "_download", lambda url: selected)
    with pytest.raises(load_dags.SourceError, match="exceed 1 MiB"):
        load_dags.fetch_archive(source)


@pytest.mark.parametrize(
    "data",
    [b"not a tarball", _tar_gz({f"{TOP}/src/dags/a.py": bytes(range(256)) * 400})[:-40]],
)
def test_a_corrupt_archive_fails(monkeypatch, data):
    monkeypatch.setattr(load_dags, "_download", lambda url: data)

    with pytest.raises(load_dags.SourceError, match="not a readable tar.gz"):
        load_dags.fetch_archive(_archive_family()["source"])


def test_download_retries_a_broken_transfer_but_not_an_oversized_one(monkeypatch):
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"")
        return io.BytesIO(bytes((1 << 20) + 1))

    monkeypatch.setattr(load_dags.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(load_dags.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(load_dags, "_DOWNLOAD_LIMIT_MIB", 1)

    with pytest.raises(load_dags.SourceError, match="larger than 1 MiB"):
        load_dags._download("https://gitlab.example/archive.tar.gz")
    assert len(calls) == 2

    monkeypatch.setattr(
        load_dags.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(bytes(1 << 20))
    )
    assert len(load_dags._download("https://gitlab.example/archive.tar.gz")) == 1 << 20


TEMPLATE = (
    '"""A DAG published as a CImpl-style template."""\n'
    "from airflow import DAG\n"
    "\n"
    'DAG_NAME = "{| DAG_NAME |}"\n'
    'DOCKER_IMAGE = "{| DOCKER_IMAGE |}"\n'
    "env_vars = {| ENV_VARS or {} |}\n"
    "env_vars.update({| EXTRA_ENV_VARS|default('{}') |})\n"
    "operator_kwargs = {|  K8S_POD_OPERATOR_KWARGS or {} |}\n"
)
RENDER = {
    "DAG_NAME": "csv_ingestion",
    "DOCKER_IMAGE": PINNED_IMAGE,
    "ENV_VARS": "@worker_env",
    "EXTRA_ENV_VARS": "{}",
    "K8S_POD_OPERATOR_KWARGS": "@worker_pod_kwargs",
}


def _template_family(render: dict) -> dict:
    published = TEMPLATE.encode()
    files = [{"path": "src/dags/csv.py", "sha256": _sha256(published)}]
    return {**_remote_family(files), "render": render}


def test_loader_renders_a_template_family_after_checking_its_pins(dag_dirs, monkeypatch, capsys):
    _, target = dag_dirs
    monkeypatch.setattr(load_dags, "_download", lambda url: TEMPLATE.encode())

    load_dags.load({"families": [_template_family(RENDER)]}, values=VALUES)

    rendered = (target / "core-ingestion" / "csv.py").read_text()
    assert rendered == (
        '"""A DAG published as a CImpl-style template."""\n'
        "from kubernetes.client import models as k8s_models\n"
        "from airflow import DAG\n"
        "\n"
        'DAG_NAME = "csv_ingestion"\n'
        f'DOCKER_IMAGE = "{PINNED_IMAGE}"\n'
        'env_vars = {"STORAGE": "{{ var.value.core__service__storage__url }}"}\n'
        "env_vars.update({})\n"
        'operator_kwargs = {"container_resources": k8s_models.V1ResourceRequirements()}\n'
    )
    ast.parse(rendered)
    assert "  rendered csv.py (5 tokens)\n  core-ingestion/csv.py\n" in capsys.readouterr().out


def test_a_placeholder_the_render_map_misses_fails_the_load(dag_dirs, monkeypatch):
    monkeypatch.setattr(load_dags, "_download", lambda url: TEMPLATE.encode())
    render = {token: value for token, value in RENDER.items() if token != "EXTRA_ENV_VARS"}

    with pytest.raises(
        load_dags.SourceError,
        match=re.escape("csv.py: placeholder \"{| EXTRA_ENV_VARS|default('{}') |}\" is left"),
    ):
        load_dags.load({"families": [_template_family(render)]}, values=VALUES)


def test_a_family_without_render_refuses_a_templated_file(dag_dirs):
    config, _ = dag_dirs
    (config / "a.py").write_text('DAG_NAME = "{| DAG_NAME |}"\n')

    with pytest.raises(
        load_dags.SourceError,
        match=re.escape("a: a.py: placeholder '{| DAG_NAME |}' is left; the family has no render"),
    ):
        load_dags.load({"families": [_local_family("a", default=True)]})


def test_placeholders_render_apart_and_only_in_python_files():
    family = {**_local_family("a"), "render": {"SECOND": "2", "FIRST": "1"}}
    files = {"a.py": b"pair = ({| FIRST |}, {| SECOND |})\n", "b.json": b'["{| FIRST |}"]\n'}

    assert load_dags.render(family, files, {}) == {
        "a.py": b"pair = (1, 2)\n",
        "b.json": files["b.json"],
    }


@pytest.mark.parametrize(
    ("published", "rendered"),
    [
        (
            "from kubernetes.client import models as k8s_models\nkw = {| KW |}\n",
            "from kubernetes.client import models as k8s_models\nkw = k8s_models.V1Pod()\n",
        ),
        (
            "from __future__ import annotations\nimport os\nkw = {| KW |}\n",
            "from __future__ import annotations\n"
            "from kubernetes.client import models as k8s_models\n"
            "import os\nkw = k8s_models.V1Pod()\n",
        ),
        (
            '"""Ingest\nfrom the workflow service."""\nimport os\nkw = {| KW |}\n',
            '"""Ingest\nfrom the workflow service."""\n'
            "from kubernetes.client import models as k8s_models\n"
            "import os\nkw = k8s_models.V1Pod()\n",
        ),
        (
            "kw = {| KW |}\n",
            "from kubernetes.client import models as k8s_models\nkw = k8s_models.V1Pod()\n",
        ),
    ],
)
def test_a_rendered_file_imports_k8s_models_when_a_value_uses_it(published, rendered):
    family = {**_local_family("a"), "render": {"KW": "k8s_models.V1Pod()"}}

    assert load_dags.render(family, {"a.py": published.encode()}, {})["a.py"] == rendered.encode()


def test_shipped_render_values_are_python_expressions_for_worker_pods():
    values = load_dags.read_values(OVERLAY)
    assert set(values) == {"worker_namespace", "worker_pod_kwargs", "worker_env"}
    for text in values.values():
        ast.parse(text, mode="eval")
    assert values["worker_namespace"] == "platform"

    stub = SimpleNamespace(V1ResourceRequirements=lambda **fields: fields)
    kwargs = eval(values["worker_pod_kwargs"], {"__builtins__": {}, "k8s_models": stub})

    assert kwargs["service_account_name"] == "workload-identity-sa"
    assert kwargs["labels"]["azure.workload.identity/use"] == "true"
    assert kwargs["annotations"] == {"sidecar.istio.io/inject": "false"}
    assert kwargs["container_resources"] == {
        "requests": {"cpu": "200m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }
    assert kwargs["container_security_context"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert kwargs["on_finish_action"] == "delete_succeeded_pod"
    # The upstream templates pass these to KubernetesPodOperator themselves, so
    # a second value would be a duplicate keyword argument.
    assert not {"namespace", "image", "name", "task_id", "env_vars", "arguments"} & set(kwargs)


def test_shipped_worker_env_reads_variables_every_airflow_container_has():
    env = ast.literal_eval(load_dags.read_values(OVERLAY)["worker_env"])
    variables = (OVERLAY / "airflow-variables.env").read_text(encoding="utf-8").splitlines()
    provided = {
        line.split("=", 1)[0].removeprefix("AIRFLOW_VAR_").lower()
        for line in variables
        if line.startswith("AIRFLOW_VAR_")
    }
    # AZURE_RESOURCE_ID reaches every container from spi-airflow-osdu, which
    # extraEnvFrom maps with the AIRFLOW_VAR_ prefix.
    provided.add("azure_resource_id")

    assert env.pop("CLOUD_PROVIDER") == "azure"
    for key, value in env.items():
        variable = re.fullmatch(r"\{\{ var\.value\.(\w+) \}\}", value)
        assert variable and variable.group(1) in provided, key


def _write_catalog(directory: Path, families: list[dict]) -> Path:
    catalog = directory / "dag-sources.yaml"
    catalog.write_text(yaml.safe_dump({"families": families}, sort_keys=False), encoding="utf-8")
    return catalog


def test_pin_prints_the_archive_digest_the_loader_accepts(tmp_path, dag_dirs, monkeypatch, capsys):
    files = {"r3.py": b"r3 = 1\n", "pkg/helper.py": b"helper = 1\n"}
    archive = _tar_gz(
        {
            **{f"{TOP}/src/dags/{path}": body for path, body in files.items()},
            f"{TOP}/README.md": b"# dags\n",
        }
    )
    monkeypatch.setattr(pin_script.load_dags, "_download", lambda url: archive)
    monkeypatch.setattr(load_dags, "_download", lambda url: archive)
    family = _archive_family()

    assert pin_script.main([str(_write_catalog(tmp_path, [family])), "add-on"]) == 1

    out = capsys.readouterr().out
    assert (
        f"    {_sha256(files['pkg/helper.py'])}  pkg/helper.py\n"
        f"    {_sha256(files['r3.py'])}  r3.py\n"
    ) in out
    printed = re.search(r"digest: (sha256:[0-9a-f]{64}) \(the catalog has none\)", out)
    assert printed and printed.group(1) == _digest(files)

    family["source"]["digest"] = printed.group(1)
    assert load_dags.load({"families": [family]}) == 2
    assert pin_script.main([str(_write_catalog(tmp_path, [family])), "add-on"]) == 0
    assert "add-on: the catalog's pins match\n" in capsys.readouterr().out


def test_pin_reports_each_files_sha256(tmp_path, monkeypatch, capsys):
    bodies = {"src/dags/r3.py": b"r3 = 1\n", "src/dags/manifest.py": b"manifest = 1\n"}

    def download(url):
        return bodies[urllib.parse.unquote(url.split("/files/")[1].split("/raw")[0])]

    monkeypatch.setattr(pin_script.load_dags, "_download", download)
    family = _remote_family(
        [
            {"path": "src/dags/r3.py", "sha256": _sha256(bodies["src/dags/r3.py"])},
            {"path": "src/dags/manifest.py", "sha256": "0" * 64},
        ]
    )

    assert pin_script.main([str(_write_catalog(tmp_path, [family])), "core-ingestion"]) == 1

    out = capsys.readouterr().out
    r3, manifest = _sha256(bodies["src/dags/r3.py"]), _sha256(bodies["src/dags/manifest.py"])
    assert f"  src/dags/r3.py\n    sha256: {r3} (matches the catalog)\n" in out
    assert f"  src/dags/manifest.py\n    sha256: {manifest} (the catalog has {'0' * 64})\n" in out
    assert "core-ingestion: pin the values above in" in out

    family["files"][1]["sha256"] = manifest
    assert pin_script.main([str(_write_catalog(tmp_path, [family])), "core-ingestion"]) == 0


def test_pin_fails_for_an_unknown_family_or_one_that_does_not_render(tmp_path, monkeypatch, capsys):
    published = b'NAME = "{| NAME |}"\n'
    monkeypatch.setattr(pin_script.load_dags, "_download", lambda url: published)
    files = [{"path": "src/dags/a.py", "sha256": _sha256(published)}]
    catalog = _write_catalog(tmp_path, [{**_remote_family(files), "render": {"OTHER": "x"}}])

    assert pin_script.main([str(catalog), "nope"]) == 1
    assert "has no family 'nope'" in capsys.readouterr().err
    assert pin_script.main([str(catalog), "core-ingestion"]) == 1
    assert "a.py: placeholder '{| NAME |}' is left" in capsys.readouterr().err


def test_pin_has_nothing_to_pin_for_a_local_family(tmp_path, capsys):
    catalog = _write_catalog(tmp_path, [_local_family("a")])

    assert pin_script.main([str(catalog), "a"]) == 0
    assert "nothing to pin" in capsys.readouterr().out


def test_declared_sources_are_pinned_and_shipped():
    sources = yaml.safe_load((OVERLAY / "dag-sources.yaml").read_text(encoding="utf-8"))
    families = load_dags.check_catalog(sources, load_dags.read_values(OVERLAY))
    # Core loads these without a selection; any other family is an add-on.
    assert [family["name"] for family in families if family.get("default")] == [
        "spi-smoke",
        "core-ingestion",
    ]
    local_files = set()
    for family in families:
        assert family["dags"], family["name"]
        source = family["source"]
        if source["type"] == "local":
            local_files.update(entry["path"] for entry in family["files"])
            continue
        assert re.fullmatch(r"[0-9a-f]{40}", source["commit"])
        if source["type"] == "archive":
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", source["digest"]), family["name"]
            continue
        for entry in family["files"]:
            assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), entry["path"]

    kustomization = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text(encoding="utf-8"))
    generators = {
        generator["name"]: set(generator["files"])
        for generator in kustomization["configMapGenerator"]
        if "files" in generator
    }
    assert set(generators) == {"airflow-osdu-dags", "airflow-osdu-dags-revision"}
    mounted = {"load_dags.py", "dag-sources.yaml", "render-values.yaml", "requirements.txt"}
    assert generators["airflow-osdu-dags"] == mounted | local_files
    # Any change to what the pods mount must change the revision that restarts them.
    assert generators["airflow-osdu-dags"] <= generators["airflow-osdu-dags-revision"]
    for name in set().union(*generators.values()):
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
