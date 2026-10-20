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

"""Print the pins a DAG family in dag-sources.yaml needs.

Fetches the family the way the Airflow loader does, through the loader itself
(software/components/airflow-osdu/load_dags.py), and prints the values to pin:
each file's sha256 for a gitlab or github family, or for an archive family its
digest and the files its include globs select. It then renders the family as
the loader would, so a placeholder the render map misses shows up before a
deploy. The family may leave sha256 or digest out until this prints them, and
standard render values come from render-values.yaml beside the catalog.

Usage: pin_dag_family.py CATALOG FAMILY

  uv run python scripts/pin_dag_family.py software/components/airflow-osdu/dag-sources.yaml core-ingestion

Exit codes:
  0  every pin in the catalog matches what the source serves, and it renders
  1  a pin is missing or differs, or the family cannot be fetched or rendered
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import yaml

LOADER_PATH = (
    Path(__file__).resolve().parent.parent
    / "software"
    / "components"
    / "airflow-osdu"
    / "load_dags.py"
)


def _import_loader() -> ModuleType:
    spec = importlib.util.spec_from_file_location("spi_load_dags", LOADER_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import {LOADER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


load_dags = _import_loader()


def find_family(catalog: Path, name: str, values: dict[str, str]) -> dict:
    sources = yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}
    for family in load_dags.check_catalog(sources, values, pinned=False):
        if family["name"] == name:
            return family
    raise load_dags.SourceError(f"{catalog} has no family {name!r}")


def _verdict(pinned: object, actual: str) -> str:
    if pinned == actual:
        return "(matches the catalog)"
    return "(the catalog has none)" if pinned is None else f"(the catalog has {pinned})"


def report(family: dict) -> tuple[dict[str, bytes], bool]:
    """Print the family's pins; return its files and whether the catalog has them all."""
    source = family["source"]
    if source["type"] == "local":
        print("  local files ship in the ConfigMap; there is nothing to pin")
        return {}, True
    if source["type"] == "archive":
        files = load_dags.fetch_archive(source)
        print(f"  files include matches below strip_prefix: {len(files)}")
        for line in load_dags.digest_lines(files):
            print(f"    {line}", end="")
        digest = load_dags.archive_digest(files)
        print(f"  digest: {digest} {_verdict(source.get('digest'), digest)}")
        return files, source.get("digest") == digest
    files = {}
    matched = True
    for entry in family["files"]:
        data = load_dags.fetch_file(source, entry["path"])
        sha256 = hashlib.sha256(data).hexdigest()
        print(f"  {entry['path']}")
        print(f"    sha256: {sha256} {_verdict(entry.get('sha256'), sha256)}")
        files[load_dags.target_path(family, entry).as_posix()] = data
        matched = matched and entry.get("sha256") == sha256
    return files, matched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("catalog", type=Path, help="path to dag-sources.yaml")
    parser.add_argument("family", help="name of the family to pin")
    args = parser.parse_args(argv)
    try:
        values = load_dags.read_values(args.catalog.parent)
        family = find_family(args.catalog, args.family, values)
        print(f"family {family['name']} ({load_dags.describe(family['source'])})")
        files, matched = report(family)
        load_dags.render(family, files, values)
    except (load_dags.SourceError, OSError, yaml.YAMLError) as error:
        sys.stdout.flush()
        print(f"{args.family}: {error}", file=sys.stderr)
        return 1
    if not matched:
        print(f"{args.family}: pin the values above in {args.catalog}")
        return 1
    print(f"{args.family}: the catalog's pins match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
