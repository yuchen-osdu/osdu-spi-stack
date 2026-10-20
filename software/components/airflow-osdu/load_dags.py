#!/usr/bin/env python3
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

"""Load the pinned OSDU DAG files into the Airflow DAGs folder.

Runs as an init container on the scheduler and dag-processor. The catalog,
dag-sources.yaml, names every DAG family; its header documents the schema.

Selection: every family marked `default: true` loads, plus the families named
in SPI_DAG_FAMILIES (comma-separated). `none`, Flux's default when no add-on is
selected, names no family. A name the catalog lacks fails the load.
Families load in catalog order, and a family that is not selected is never
fetched.

Sources: local files are keys of the same ConfigMap as this script. A gitlab or
github family fetches each file at a full commit SHA and checks its sha256. An
archive family downloads one tar.gz of the repository at the commit and takes
the regular files below strip_prefix that match an include glob; together they
must match the family's digest (see archive_digest). Links are skipped, and a
member path that is absolute or contains '..' fails the load. Files land in
<target>/<family>/ at their path below the prefix.

Rendering: a family's `render:` map fills the CImpl-style `{| TOKEN |}`
placeholders in its .py files once the bytes are verified. A value `@name` is
the standard value `name` from render-values.yaml, beside the catalog. A
placeholder left over, or one in a family without a render map, fails the load.

Any failure exits non-zero so the pod never starts with a partial DAG set.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import io
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path, PurePosixPath

import yaml

CONFIG_DIR = Path(os.environ.get("SPI_DAG_CONFIG_DIR", "/opt/spi/osdu"))
TARGET_DIR = Path(os.environ.get("SPI_DAG_TARGET_DIR", "/opt/airflow/dags"))

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")
_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PINNED_IMAGE = re.compile(r"@sha256:[0-9a-f]{64}(?![0-9a-f])")
# From an opening {| to its |} on the same line, or to the end of that line.
# SPI_DAG_FAMILIES when no add-on is selected: Flux turns an empty default into
# YAML null, which the Airflow chart refuses. No family may take the name.
NO_SELECTION = "none"
_LEFTOVER = re.compile(r"\{\|.*?(?:\|\}|$)", re.MULTILINE)
_K8S_MODELS_IMPORT = re.compile(
    r"^\s*(from\s+kubernetes\.client\s+import\s+models\s+as\s+k8s_models"
    r"|import\s+.*\bk8s_models\b)",
    re.MULTILINE,
)
_FAMILY_KEYS = {"name", "default", "requires", "source", "files", "render", "dags"}
_HOST_KEYS = {"gitlab": {"url", "project"}, "github": {"repository"}}
_ATTEMPTS = 4
_TIMEOUT_SECONDS = 30
_DOWNLOAD_LIMIT_MIB = 200
_EXTRACT_LIMIT_MIB = 50


class SourceError(Exception):
    pass


def _mapping(value: object, allowed: set[str], what: str) -> dict:
    if not isinstance(value, dict):
        raise SourceError(f"{what} must be a mapping")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise SourceError(f"{what} has unknown keys: {', '.join(unknown)}")
    return value


def _relative(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts:
        raise SourceError(f"{field} must be a relative path without '..': {value!r}")
    return path


def _text(source: dict, field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value:
        raise SourceError(f"source needs {field}")
    return value


def _commit(source: dict) -> str:
    commit = str(source.get("commit", ""))
    if not _COMMIT.match(commit):
        raise SourceError(f"commit must be a full 40-character SHA: {commit!r}")
    return commit


def _repository(source: dict) -> str:
    repository = _text(source, "repository")
    if not _REPOSITORY.match(repository):
        raise SourceError(f"repository must be owner/name: {repository!r}")
    return repository


def _prefix(source: dict) -> tuple[str, ...]:
    strip = source.get("strip_prefix")
    return _relative(str(strip), "strip_prefix").parts if strip else ()


def _includes(source: dict) -> list[str]:
    patterns = source.get("include")
    if not isinstance(patterns, list) or not patterns:
        raise SourceError("include must be a non-empty list of glob patterns")
    for pattern in patterns:
        # PurePosixPath.match reads ** as *, so refuse it rather than surprise.
        if not isinstance(pattern, str) or not pattern or pattern[0] == "/" or "**" in pattern:
            raise SourceError(f"include patterns are relative globs without '**': {pattern!r}")
    return patterns


def describe(source: dict) -> str:
    kind = source.get("type")
    return f"{source.get('host')} archive" if kind == "archive" else str(kind)


def _raw_url(source: dict, path: str) -> str:
    kind = source.get("type")
    commit = _commit(source)
    if kind == "gitlab":
        project = urllib.parse.quote(_text(source, "project"), safe="")
        file_path = urllib.parse.quote(path, safe="")
        base = _text(source, "url").rstrip("/")
        return f"{base}/api/v4/projects/{project}/repository/files/{file_path}/raw?ref={commit}"
    if kind == "github":
        return f"https://raw.githubusercontent.com/{_repository(source)}/{commit}/{path}"
    raise SourceError(f"unsupported source type {kind!r}")


def _archive_url(source: dict) -> str:
    host = source.get("host")
    commit = _commit(source)
    if host == "gitlab":
        project = urllib.parse.quote(_text(source, "project"), safe="")
        base = _text(source, "url").rstrip("/")
        return f"{base}/api/v4/projects/{project}/repository/archive.tar.gz?sha={commit}"
    if host == "github":
        return f"https://codeload.github.com/{_repository(source)}/tar.gz/{commit}"
    raise SourceError(f"archive host must be gitlab or github: {host!r}")


def _download(url: str) -> bytes:
    limit = _DOWNLOAD_LIMIT_MIB << 20
    delay = 5
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
                data = response.read(limit + 1)
        except (OSError, http.client.HTTPException) as error:
            if attempt == _ATTEMPTS:
                raise SourceError(f"{url}: {error}") from error
            print(f"  attempt {attempt} failed ({error}); retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay *= 2
            continue
        if len(data) > limit:
            raise SourceError(f"{url}: larger than {_DOWNLOAD_LIMIT_MIB} MiB")
        return data
    raise AssertionError("unreachable")


def fetch_file(source: dict, path: str) -> bytes:
    """One file of a gitlab or github source at its commit, not yet checked."""
    return _download(_raw_url(source, str(_relative(path, "path"))))


def fetch_archive(source: dict) -> dict[str, bytes]:
    """The archive's regular files below strip_prefix that match include.

    Keyed by their path below the prefix, sorted, and not yet checked against
    the digest.
    """
    url = _archive_url(source)
    prefix = _prefix(source)
    patterns = _includes(source)
    data = _download(url)
    limit = _EXTRACT_LIMIT_MIB << 20
    files: dict[str, bytes] = {}
    top = None
    total = 0
    try:
        # Stream mode reads each member once, in order; nothing touches the disk.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise SourceError(f"archive member {member.name!r} escapes the archive")
                if not path.parts:
                    continue
                # GitLab and GitHub put the tree below one <repository>-<commit> folder.
                top = top or path.parts[0]
                if path.parts[0] != top:
                    raise SourceError(f"archive member {member.name!r} is outside {top}/")
                parts = path.parts[1:]
                if (
                    not member.isreg()
                    or len(parts) <= len(prefix)
                    or parts[: len(prefix)] != prefix
                ):
                    continue
                relative = PurePosixPath(*parts[len(prefix) :])
                if not any(relative.match(pattern) for pattern in patterns):
                    continue
                total += member.size
                if total > limit:
                    raise SourceError(f"the files include selects exceed {_EXTRACT_LIMIT_MIB} MiB")
                if relative.as_posix() in files:
                    raise SourceError(f"archive member {member.name!r} appears twice")
                reader = archive.extractfile(member)
                if reader is not None:
                    files[relative.as_posix()] = reader.read()
    except (tarfile.TarError, EOFError, zlib.error) as error:
        raise SourceError(f"{url}: not a readable tar.gz: {error}") from error
    if not files:
        raise SourceError("no regular file below strip_prefix matches include")
    return dict(sorted(files.items()))


def digest_lines(files: dict[str, bytes]) -> list[str]:
    """A "<sha256>  <path>" line per file, sorted by path: what archive_digest hashes."""
    return [f"{hashlib.sha256(files[path]).hexdigest()}  {path}\n" for path in sorted(files)]


def archive_digest(files: dict[str, bytes]) -> str:
    """The pin of an archive family: sha256 over its digest lines in UTF-8."""
    return "sha256:" + hashlib.sha256("".join(digest_lines(files)).encode("utf-8")).hexdigest()


def target_path(family: dict, entry: dict) -> PurePosixPath:
    """Where a `files` entry lands below its family's folder."""
    path = _relative(str(entry.get("path", "")), "path")
    prefix = _prefix(family["source"])
    if path.parts[: len(prefix)] != prefix:
        raise SourceError(f"{path} is outside strip_prefix {PurePosixPath(*prefix)}")
    if len(path.parts) == len(prefix):
        raise SourceError(f"{path} is the strip_prefix itself")
    return PurePosixPath(*path.parts[len(prefix) :])


def _content(family: dict, entry: dict) -> bytes:
    path = str(entry["path"])
    if family["source"]["type"] == "local":
        return (CONFIG_DIR / path).read_bytes()
    data = fetch_file(family["source"], path)
    actual = hashlib.sha256(data).hexdigest()
    if actual != entry["sha256"]:
        raise SourceError(f"{path}: sha256 {actual} does not match pinned {entry['sha256']}")
    return data


def fetch(family: dict) -> dict[str, bytes]:
    """The family's files by their path below the prefix, checked against the pins."""
    source = family["source"]
    if source["type"] != "archive":
        return {target_path(family, e).as_posix(): _content(family, e) for e in family["files"]}
    files = fetch_archive(source)
    actual = archive_digest(files)
    if actual != source["digest"]:
        raise SourceError(f"archive digest {actual} does not match pinned {source['digest']}")
    return files


def _render_map(family: dict, values: dict[str, str]) -> dict[str, str]:
    """The family's TOKEN -> text map, with @name values resolved."""
    render = family.get("render")
    if render is None:
        return {}
    if not isinstance(render, dict) or not render:
        raise SourceError("render must map TOKEN names to text")
    tokens = {}
    for token, value in render.items():
        if not isinstance(token, str) or not _TOKEN.match(token):
            raise SourceError(f"render token {token!r} must be upper case, like DOCKER_IMAGE")
        if not isinstance(value, str):
            raise SourceError(f"render value for {token} must be text")
        if value.startswith("@"):
            if value[1:] not in values:
                raise SourceError(f"render value for {token} names unknown standard value {value}")
            value = values[value[1:]]
        if token.endswith("IMAGE") and not _PINNED_IMAGE.search(value):
            raise SourceError(f"render value for {token} must pin its image by @sha256: digest")
        tokens[token] = value
    return tokens


def _placeholder(token: str) -> re.Pattern[str]:
    # CImpl's pattern, except that a span cannot run past a |}, so two
    # placeholders on one line render apart.
    inside = r"(?:(?!\|\}).)*?"
    return re.compile(r"\{\|" + inside + r"\b" + re.escape(token) + r"\b" + inside + r"\|\}")


def _import_k8s_models(path: str, text: str) -> str:
    """Import k8s_models where a rendered value uses it, as CImpl's loader does.

    The import goes before the first top-level import statement other than a
    __future__ one, so a docstring line starting with "from" is never taken
    for an import.
    """
    if "k8s_models." not in text or _K8S_MODELS_IMPORT.search(text):
        return text
    try:
        body = ast.parse(text, filename=path).body
    except SyntaxError as error:
        raise SourceError(f"{path} is not valid Python once rendered: {error}") from error
    line = 0
    for node in body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            line = node.end_lineno or node.lineno
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            line = node.lineno - 1
            break
    lines = text.split("\n")
    lines.insert(line, "from kubernetes.client import models as k8s_models")
    return "\n".join(lines)


def _render_file(path: str, data: bytes, tokens: dict[str, str]) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceError(f"{path} is not UTF-8: {error}") from error
    count = 0
    for token, value in tokens.items():
        text, replaced = _placeholder(token).subn(lambda _match: value, text)
        count += replaced
    leftover = _LEFTOVER.search(text)
    if leftover:
        reason = "no render token matches it" if tokens else "the family has no render map"
        raise SourceError(f"{path}: placeholder {leftover.group(0)!r} is left; {reason}")
    if not count:
        return data
    print(f"  rendered {path} ({count} tokens)", flush=True)
    return _import_k8s_models(path, text).encode("utf-8")


def render(family: dict, files: dict[str, bytes], values: dict[str, str]) -> dict[str, bytes]:
    """Fill the family's placeholders in its .py files and refuse any left over."""
    tokens = _render_map(family, values)
    rendered = {}
    for path, data in files.items():
        if path.endswith(".py") and (tokens or b"{|" in data):
            data = _render_file(path, data, tokens)
        rendered[path] = data
    return rendered


def _check_source(source: object, pinned: bool) -> None:
    if not isinstance(source, dict):
        raise SourceError("source must be a mapping")
    kind = source.get("type")
    if kind == "local":
        _mapping(source, {"type"}, "a local source")
        return
    if kind not in ("gitlab", "github", "archive"):
        raise SourceError(f"unsupported source type {kind!r}")
    host = source.get("host") if kind == "archive" else kind
    if host not in _HOST_KEYS:
        raise SourceError(f"archive host must be gitlab or github: {host!r}")
    archive_keys = {"host", "include", "digest"} if kind == "archive" else set()
    _mapping(source, {"type", "commit", "strip_prefix", *_HOST_KEYS[host], *archive_keys}, "source")
    _commit(source)
    if host == "gitlab":
        if not re.match(r"https?://", _text(source, "url")):
            raise SourceError(f"url must be an http(s) URL: {source['url']!r}")
        _text(source, "project")
    else:
        _repository(source)
    _prefix(source)
    if kind == "archive":
        _includes(source)
        if pinned and not _DIGEST.match(str(source.get("digest", ""))):
            raise SourceError("an archive source needs digest: sha256:<64 hex>")


def _check_files(family: dict, pinned: bool) -> None:
    entries = family.get("files")
    if not isinstance(entries, list) or not entries:
        raise SourceError("files must be a non-empty list")
    local = family["source"]["type"] == "local"
    seen = set()
    for entry in entries:
        _mapping(entry, {"path"} if local else {"path", "sha256"}, "a files entry")
        target = target_path(family, entry)
        if local and len(target.parts) != 1:
            raise SourceError(f"local files are ConfigMap keys and cannot nest: {target}")
        if not local and pinned and not _SHA256.match(str(entry.get("sha256", ""))):
            raise SourceError(f"{entry['path']}: remote files need a 64-character sha256")
        if target in seen:
            raise SourceError(f"two files land at {target}")
        seen.add(target)


def check_family(family: dict, values: dict[str, str], pinned: bool = True) -> None:
    """Refuse a family the loader could not load exactly as declared.

    With pinned false, sha256 and digest may be missing or stale: the pin
    script checks a family it is about to pin.
    """
    _mapping(family, _FAMILY_KEYS, "a family")
    if not isinstance(family.get("default", False), bool):
        raise SourceError("default must be true or false")
    requires = family.get("requires") or []
    if not isinstance(requires, list) or not all(
        isinstance(service, str) and _NAME.match(service) for service in requires
    ):
        raise SourceError("requires must list lowercase service names")
    _check_source(family.get("source"), pinned)
    if family["source"]["type"] != "archive":
        _check_files(family, pinned)
    elif "files" in family:
        raise SourceError("an archive family selects its files with include, not files")
    _render_map(family, values)


def check_catalog(sources: object, values: dict[str, str], pinned: bool = True) -> list[dict]:
    """The catalog's families; one malformed family refuses the whole catalog."""
    families = _mapping(sources, {"families"}, "dag-sources.yaml").get("families") or []
    if not isinstance(families, list):
        raise SourceError("families must be a list")
    names = set()
    for family in families:
        name = family.get("name") if isinstance(family, dict) else None
        if not isinstance(name, str) or not _NAME.match(name) or name == NO_SELECTION:
            raise SourceError(f"invalid family name {name!r}")
        if name in names:
            raise SourceError(f"family names must be unique: {name}")
        names.add(name)
        try:
            check_family(family, values, pinned)
        except SourceError as error:
            raise SourceError(f"{name}: {error}") from error
    return families


def read_values(config_dir: Path) -> dict[str, str]:
    """The standard render values in render-values.yaml; none without the file."""
    path = config_dir / "render-values.yaml"
    if not path.is_file():
        return {}
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = _mapping(document, {"values"}, path.name).get("values") or {}
    if not isinstance(values, dict) or not all(
        isinstance(name, str) and isinstance(text, str) for name, text in values.items()
    ):
        raise SourceError(f"{path.name}: values must map names to text")
    return values


def select(families: list[dict], requested: str) -> list[dict]:
    """The default families plus those `requested` names, in catalog order."""
    names = {name.strip() for name in requested.split(",")} - {"", NO_SELECTION}
    unknown = sorted(names - {family["name"] for family in families})
    if unknown:
        raise SourceError(
            f"SPI_DAG_FAMILIES names unknown families: {', '.join(unknown)}"
            f" (the catalog has {', '.join(family['name'] for family in families)})"
        )
    return [family for family in families if family.get("default") or family["name"] in names]


def load(sources: dict, requested: str = "", values: dict[str, str] | None = None) -> int:
    values = values or {}
    selected = select(check_catalog(sources, values), requested)
    print(f"families selected: {', '.join(f['name'] for f in selected) or 'none'}", flush=True)

    # The folder is an emptyDir that outlives container restarts within the
    # pod, so start clean rather than merge with a previous attempt.
    for child in TARGET_DIR.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    count = 0
    for family in selected:
        name = family["name"]
        print(f"family {name} ({describe(family['source'])})", flush=True)
        try:
            files = render(family, fetch(family), values)
        except SourceError as error:
            raise SourceError(f"{name}: {error}") from error
        for path, data in files.items():
            destination = TARGET_DIR / name / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            print(f"  {name}/{path}", flush=True)
            count += 1
    return count


def main() -> int:
    try:
        sources = yaml.safe_load((CONFIG_DIR / "dag-sources.yaml").read_text(encoding="utf-8"))
        values = read_values(CONFIG_DIR)
        count = load(sources or {}, os.environ.get("SPI_DAG_FAMILIES", ""), values)
    except (SourceError, OSError, KeyError, yaml.YAMLError) as error:
        print(f"DAG load failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(f"loaded {count} DAG files into {TARGET_DIR}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
