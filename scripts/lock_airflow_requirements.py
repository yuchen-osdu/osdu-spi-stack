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

"""Lock the OSDU DAG libraries the Airflow scheduler installs at start-up.

Resolves software/components/airflow-osdu/requirements.in against the Airflow
constraints for the pinned airflowVersion, so every dependency the image already
ships resolves to the version it ships, and writes requirements.txt with a
sha256 for every file pip may download. pip then refuses anything else.

uv emits no hashes for --find-links packages, and some mirrors serve md5 only,
so hashes are rebuilt here: sha256 entries from uv are kept, and packages that
have none take theirs from the #sha256= fragments of the find-links pages.

Usage: lock_airflow_requirements.py [--check] [--index-url URL]

  --check      resolve and fail if requirements.txt differs, without writing
  --index-url  index for resolution only (default: $UV_DEFAULT_INDEX or PyPI);
               the generated file always points pods at public PyPI

Exit codes:
  0  lock written, or up to date with --check
  1  lock out of date (--check) or resolution failed
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import yaml
from packaging.utils import canonicalize_name
from packaging.version import Version

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPONENTS = _REPO_ROOT / "software" / "components"
RELEASE_PATH = _COMPONENTS / "airflow" / "release.yaml"
INPUT_PATH = _COMPONENTS / "airflow-osdu" / "requirements.in"
OUTPUT_PATH = _COMPONENTS / "airflow-osdu" / "requirements.txt"

# Default Python of the apache/airflow:<airflowVersion> image. Check with
# `docker run --rm apache/airflow:<version> python --version` when bumping.
PYTHON_VERSION = "3.13"
PYTHON_PLATFORM = "x86_64-manylinux_2_28"
RUNTIME_INDEX = "https://pypi.org/simple"
CONSTRAINTS_URL = "https://raw.githubusercontent.com/apache/airflow/constraints-{airflow}/constraints-{python}.txt"
_FETCH_TIMEOUT_SECONDS = 60

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")
_HASH = re.compile(r"--hash=(\w+):([0-9a-f]+)")


def airflow_version() -> str:
    release = yaml.safe_load(RELEASE_PATH.read_text(encoding="utf-8"))
    return str(release["spec"]["values"]["airflowVersion"])


def find_links(requirements_in: str) -> list[str]:
    links = []
    for line in requirements_in.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("--find-links", "-f"):
            links.append(parts[1])
    return links


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT_SECONDS) as response:
        return response.read()


def compile_pins(constraints: Path, index_url: str, work: Path) -> dict[str, list[tuple[str, str]]]:
    """Return {name==version: [(algorithm, digest), ...]} as uv resolved them."""
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required on PATH")
    output = work / "uv-requirements.txt"
    subprocess.run(
        [
            uv,
            "pip",
            "compile",
            str(INPUT_PATH),
            "--no-config",
            "--quiet",
            "--constraint",
            str(constraints),
            "--python-version",
            PYTHON_VERSION,
            "--python-platform",
            PYTHON_PLATFORM,
            "--only-binary",
            ":all:",
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--default-index",
            index_url,
            "--output-file",
            str(output),
        ],
        check=True,
    )
    pins: dict[str, list[tuple[str, str]]] = {}
    current = None
    for line in output.read_text(encoding="utf-8").splitlines():
        pin = _PIN.match(line)
        if pin:
            current = f"{pin.group(1)}=={pin.group(2)}"
            pins[current] = []
        if current:
            pins[current].extend(_HASH.findall(line))
    return pins


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def _distribution(filename: str) -> tuple[str, str] | None:
    """Return (name, version) for a wheel or sdist filename."""
    if filename.endswith(".whl"):
        parts = filename[: -len(".whl")].split("-")
        return (parts[0], parts[1]) if len(parts) >= 5 else None
    for suffix in (".tar.gz", ".zip"):
        if filename.endswith(suffix):
            name, _, version = filename[: -len(suffix)].rpartition("-")
            return (name, version) if name else None
    return None


def find_links_hashes(links: list[str]) -> dict[tuple[str, Version], list[str]]:
    """Collect sha256 digests of wheels from the find-links pages."""
    found: dict[tuple[str, Version], list[str]] = {}
    for link in links:
        page = _Anchors()
        page.feed(_fetch(link).decode("utf-8"))
        for href in page.hrefs:
            path, _, fragment = href.partition("#")
            if not fragment.startswith("sha256="):
                continue
            filename = path.rsplit("/", 1)[-1]
            # Wheels only, matching the --only-binary the pods install with.
            if not filename.endswith(".whl"):
                continue
            parsed = _distribution(filename)
            if parsed is None:
                continue
            key = (canonicalize_name(parsed[0]), Version(parsed[1]))
            found.setdefault(key, []).append(fragment[len("sha256=") :])
    return found


def render(
    airflow: str,
    links: list[str],
    pins: dict[str, list[tuple[str, str]]],
    linked: dict[tuple[str, Version], list[str]],
) -> str:
    lines = [
        "# Generated by scripts/lock_airflow_requirements.py from requirements.in; do not edit.",
        f"# Resolved against the Airflow {airflow} constraints for Python {PYTHON_VERSION}.",
        "--require-hashes",
        "--only-binary :all:",
        f"--index-url {RUNTIME_INDEX}",
        *(f"--find-links {link}" for link in links),
    ]
    missing = []
    for pin in sorted(pins, key=lambda value: canonicalize_name(value.split("==")[0])):
        name, version = pin.split("==")
        digests = sorted({digest for algorithm, digest in pins[pin] if algorithm == "sha256"})
        if not digests:
            digests = sorted(set(linked.get((canonicalize_name(name), Version(version)), [])))
        if not digests:
            missing.append(pin)
            continue
        lines.append(f"{canonicalize_name(name)}=={version} \\")
        lines.extend(f"    --hash=sha256:{digest} \\" for digest in digests[:-1])
        lines.append(f"    --hash=sha256:{digests[-1]}")
    if missing:
        raise SystemExit(f"no sha256 for: {', '.join(missing)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--index-url",
        default=os.environ.get("UV_DEFAULT_INDEX", RUNTIME_INDEX),
    )
    args = parser.parse_args(argv)

    airflow = airflow_version()
    links = find_links(INPUT_PATH.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        constraints = work / f"constraints-{PYTHON_VERSION}.txt"
        constraints.write_bytes(
            _fetch(CONSTRAINTS_URL.format(airflow=airflow, python=PYTHON_VERSION))
        )
        pins = compile_pins(constraints, args.index_url, work)
    rendered = render(airflow, links, pins, find_links_hashes(links))

    if args.check:
        current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""
        # A Windows checkout may carry CRLF; git stores the file with LF.
        if current.replace("\r\n", "\n") != rendered:
            print(f"{OUTPUT_PATH.relative_to(_REPO_ROOT)} is out of date; rerun without --check")
            return 1
        print("requirements.txt is up to date")
        return 0
    OUTPUT_PATH.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_PATH.relative_to(_REPO_ROOT)} ({len(pins)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
