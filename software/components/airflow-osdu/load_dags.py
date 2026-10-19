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

Runs as an init container on the scheduler and dag-processor. Each family in
dag-sources.yaml names a source at a full commit SHA and the files to take
from it; every remote file must match its sha256. Local files come from the
same ConfigMap as this script. Any failure exits non-zero so the pod never
starts with a partial DAG set.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

import yaml

CONFIG_DIR = Path(os.environ.get("SPI_DAG_CONFIG_DIR", "/opt/spi/osdu"))
TARGET_DIR = Path(os.environ.get("SPI_DAG_TARGET_DIR", "/opt/airflow/dags"))

_FAMILY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTEMPTS = 4
_TIMEOUT_SECONDS = 30


class SourceError(Exception):
    pass


def _relative(value: str, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise SourceError(f"{field} must be a relative path without '..': {value!r}")
    return path


def _raw_url(source: dict, path: str) -> str:
    kind = source.get("type")
    commit = str(source.get("commit", ""))
    if not _COMMIT.match(commit):
        raise SourceError(f"commit must be a full 40-character SHA: {commit!r}")
    if kind == "gitlab":
        project = urllib.parse.quote(source["project"], safe="")
        file_path = urllib.parse.quote(path, safe="")
        base = source["url"].rstrip("/")
        return f"{base}/api/v4/projects/{project}/repository/files/{file_path}/raw?ref={commit}"
    if kind == "github":
        return f"https://raw.githubusercontent.com/{source['repository']}/{commit}/{path}"
    raise SourceError(f"unsupported source type {kind!r}")


def _download(url: str) -> bytes:
    delay = 5
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as response:
                return response.read()
        except OSError as error:
            if attempt == _ATTEMPTS:
                raise SourceError(f"{url}: {error}") from error
            print(f"  attempt {attempt} failed ({error}); retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def _content(family: dict, entry: dict) -> bytes:
    source = family["source"]
    path = _relative(str(entry.get("path", "")), "path")
    if source.get("type") == "local":
        if len(path.parts) != 1:
            raise SourceError(f"local files are ConfigMap keys and cannot nest: {path}")
        return (CONFIG_DIR / path.name).read_bytes()
    expected = str(entry.get("sha256", ""))
    if not _SHA256.match(expected):
        raise SourceError(f"{path}: remote files need a 64-character sha256")
    data = _download(_raw_url(source, str(path)))
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SourceError(f"{path}: sha256 {actual} does not match pinned {expected}")
    return data


def _target(family: dict, entry: dict) -> PurePosixPath:
    path = _relative(str(entry["path"]), "path")
    strip = family["source"].get("strip_prefix")
    if strip:
        prefix = _relative(str(strip), "strip_prefix")
        if path.parts[: len(prefix.parts)] != prefix.parts:
            raise SourceError(f"{path} is outside strip_prefix {prefix}")
        path = PurePosixPath(*path.parts[len(prefix.parts) :])
    return path


def load(sources: dict) -> int:
    families = sources.get("families") or []
    names = [family.get("name", "") for family in families]
    for name in names:
        if not _FAMILY.match(name):
            raise SourceError(f"invalid family name {name!r}")
    if len(set(names)) != len(names):
        raise SourceError("family names must be unique")

    # The folder is an emptyDir that outlives container restarts within the
    # pod, so start clean rather than merge with a previous attempt.
    for child in TARGET_DIR.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    count = 0
    for family in families:
        print(f"family {family['name']} ({family['source'].get('type')})", flush=True)
        root = TARGET_DIR / family["name"]
        for entry in family.get("files") or []:
            destination = root / _target(family, entry)
            data = _content(family, entry)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            print(f"  {destination.relative_to(TARGET_DIR)}", flush=True)
            count += 1
    return count


def main() -> int:
    try:
        sources = yaml.safe_load((CONFIG_DIR / "dag-sources.yaml").read_text(encoding="utf-8"))
        count = load(sources or {})
    except (SourceError, OSError, KeyError, yaml.YAMLError) as error:
        print(f"DAG load failed: {error}", file=sys.stderr, flush=True)
        return 1
    print(f"loaded {count} DAG files into {TARGET_DIR}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
