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

"""Reconcile selected OSDU DAG families with Workflow and Airflow.

Selects default families plus the comma-separated SPI_DAG_FAMILIES names.
Waits for exactly their live DAGs, ignoring stale and deselected catalog DAGs,
with no import errors in selected families, then registers and unpauses them.
Deselected catalog workflows bearing this script's exact description marker
are paused and deregistered. DATA_PARTITION is required for their read-by-name,
which falls back to system workflows; a listing guards against tenant records
shadowing foreign system metadata. System writes never send that header.
Every step is idempotent, so a re-run after a partial failure converges.
Any failure exits non-zero.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

import yaml

SOURCES_PATH = Path(os.environ.get("SPI_DAG_SOURCES", "/opt/spi/osdu/dag-sources.yaml"))
DAGS_ROOT = PurePosixPath(os.environ.get("SPI_DAG_TARGET_DIR", "/opt/airflow/dags"))
PARSE_TIMEOUT_SECONDS = int(os.environ.get("PARSE_TIMEOUT_SECONDS", "1200"))
POLL_SECONDS = 15
_ATTEMPTS = 5
_TIMEOUT_SECONDS = 30


class RegistrationError(Exception):
    pass


class _Transient(Exception):
    pass


def _send(
    method: str, url: str, *, token=None, body=None, form=None, expected=(200,), partition=None
):
    headers = {"Accept": "application/json"}
    if partition is not None:
        headers["data-partition-id"] = partition
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    except OSError as error:
        raise _Transient(f"{method} {url}: {error}") from error
    if status in expected:
        return status, json.loads(payload) if payload else {}
    detail = f"{method} {url} -> {status}: {payload[:500].decode(errors='replace')}"
    if status >= 500 or status == 429:
        raise _Transient(detail)
    raise RegistrationError(detail)


def request(method: str, url: str, **kwargs):
    """Send with retries on connection errors, 429, and 5xx."""
    delay = 5
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return _send(method, url, **kwargs)
        except _Transient as error:
            if attempt == _ATTEMPTS:
                raise RegistrationError(str(error)) from error
            print(f"  {error}; retrying in {delay}s", flush=True)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def declared_dags(sources: dict) -> dict[str, str]:
    """Return {dag_id: family} for every declared DAG."""
    owners: dict[str, str] = {}
    for family in sources.get("families") or []:
        for dag_id in family.get("dags") or []:
            if dag_id in owners:
                raise RegistrationError(
                    f"{dag_id} is declared by {owners[dag_id]} and {family['name']}"
                )
            owners[dag_id] = family["name"]
    if not owners:
        raise RegistrationError("dag-sources.yaml declares no DAGs")
    return owners


def selected_families(sources: dict, requested: str = "") -> list[str]:
    """Return default and explicitly requested family names in catalog order."""
    families = sources.get("families") or []
    # "none" is Flux's default when no add-on is selected (see load_dags.py).
    extra = {name.strip() for name in requested.split(",") if name.strip()} - {"none"}
    unknown = extra - {family["name"] for family in families}
    if unknown:
        raise RegistrationError(f"Unknown DAG families: {', '.join(sorted(unknown))}")
    return [
        family["name"]
        for family in families
        if family.get("default") is True or family["name"] in extra
    ]


def family_of(filename: str) -> str:
    """The family directory a parsed file sits in, absolute or bundle-relative."""
    path = PurePosixPath(filename)
    if path.is_absolute():
        try:
            path = path.relative_to(DAGS_ROOT)
        except ValueError:
            return ""
    return path.parts[0] if len(path.parts) > 1 else ""


def airflow_token(url: str) -> str:
    _, body = request(
        "POST",
        f"{url}/auth/token",
        body={
            "username": os.environ["AIRFLOW_USERNAME"],
            "password": os.environ["AIRFLOW_PASSWORD"],
        },
        expected=(200, 201),
    )
    return body["access_token"]


def osdu_token() -> str:
    """Exchange the projected Workload Identity token for the OSDU audience."""
    authority = os.environ.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/")
    url = f"{authority.rstrip('/')}/{os.environ['AZURE_TENANT_ID']}/oauth2/v2.0/token"
    assertion = Path(os.environ["AZURE_FEDERATED_TOKEN_FILE"]).read_text(encoding="utf-8")
    _, body = request(
        "POST",
        url,
        form={
            "grant_type": "client_credentials",
            "client_id": os.environ["AZURE_CLIENT_ID"],
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion.strip(),
            "scope": f"{os.environ['AZURE_RESOURCE_ID'].rstrip('/')}/.default",
        },
    )
    return body["access_token"]


def parse_state(url: str, token: str, families: set[str]) -> tuple[set[str], list[str]]:
    """Return live DAG IDs and import errors in the selected families."""
    _, dags = request("GET", f"{url}/api/v2/dags?limit=1000&exclude_stale=true", token=token)
    _, errors = request("GET", f"{url}/api/v2/importErrors?limit=1000", token=token)
    parsed = {
        dag["dag_id"]
        for dag in dags.get("dags") or []
        if not dag.get("is_stale", False) and dag.get("is_active", True)
    }
    failures = [
        error["filename"]
        for error in errors.get("import_errors") or []
        if family_of(error["filename"]) in families
    ]
    return parsed, failures


def wait_for_parse(
    url: str, wanted: set[str], families: set[str], deselected: set[str] | None = None
) -> None:
    deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
    while True:
        parsed, failures = parse_state(url, airflow_token(url), families)
        missing, extra = wanted - parsed, parsed - wanted - (deselected or set())
        if not missing and not extra and not failures:
            print(f"Airflow parsed exactly the {len(wanted)} declared DAGs", flush=True)
            return
        state = f"missing={sorted(missing)} extra={sorted(extra)} import_errors={failures}"
        if time.monotonic() >= deadline:
            raise RegistrationError(f"Airflow never parsed the declared set: {state}")
        print(f"  waiting: {state}", flush=True)
        time.sleep(POLL_SECONDS)


def workflow_description(dag_id: str, family: str) -> str:
    return f"{dag_id} from the {family} DAG family"


def register(workflow_url: str, token: str, dag_id: str, family: str) -> str:
    # The Azure workflow engine triggers the DAG named by workflowName.
    status, _ = request(
        "POST",
        f"{workflow_url}/workflow/system",
        token=token,
        body={
            "workflowName": dag_id,
            "description": workflow_description(dag_id, family),
            "registrationInstructions": {},
        },
        expected=(200, 201, 409),
    )
    return "exists" if status == 409 else "registered"


def unpause(url: str, token: str, dag_id: str) -> None:
    request(
        "PATCH",
        f"{url}/api/v2/dags/{urllib.parse.quote(dag_id, safe='')}",
        token=token,
        body={"is_paused": False},
    )


def deregister(
    workflow_url: str,
    token: str,
    dag_id: str,
    family: str,
    *,
    airflow_url: str,
    airflow_access_token: str,
    partition: str,
) -> None:
    name = urllib.parse.quote(dag_id, safe="")
    # There is no system GET; the partition-scoped lookup falls back to system metadata.
    status, workflow = request(
        "GET",
        f"{workflow_url}/workflow/{name}",
        token=token,
        partition=partition,
        expected=(200, 404),
    )
    if (
        status == 404
        or workflow.get("workflowName") != dag_id
        or workflow.get("description") != workflow_description(dag_id, family)
    ):
        print(f"{dag_id}: not registered", flush=True)
        return
    # A tenant record shadows system metadata in the name lookup; reject foreign collisions.
    status, workflows = request(
        "GET",
        f"{workflow_url}/workflow?prefix={name}",
        token=token,
        partition=partition,
        expected=(200, 404),
    )
    matches = (
        [workflow for workflow in workflows if workflow.get("workflowName") == dag_id]
        if status == 200
        else []
    )
    if not matches or any(
        workflow.get("description") != workflow_description(dag_id, family) for workflow in matches
    ):
        print(f"{dag_id}: not registered", flush=True)
        return
    # Keep the ownership marker until pausing succeeds, so interrupted cleanup can retry.
    status, _ = request(
        "PATCH",
        f"{airflow_url}/api/v2/dags/{name}",
        token=airflow_access_token,
        body={"is_paused": True},
        expected=(200, 404),
    )
    if status == 200:
        print(f"{dag_id}: paused", flush=True)
    status, _ = request(
        "DELETE",
        f"{workflow_url}/workflow/system/{name}",
        token=token,
        expected=(204, 404),
    )
    print(f"{dag_id}: {'deregistered' if status == 204 else 'not registered'}", flush=True)


def main() -> int:
    airflow_url = os.environ["AIRFLOW_URL"].rstrip("/")
    workflow_url = os.environ["WORKFLOW_URL"].rstrip("/")
    try:
        sources = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
        owners = declared_dags(sources)
        families = selected_families(sources, os.environ.get("SPI_DAG_FAMILIES", ""))
        print(f"families selected: {', '.join(families)}", flush=True)
        wanted = {dag: family for dag, family in owners.items() if family in families}
        deselected = {dag: family for dag, family in owners.items() if family not in families}
        partition = os.environ.get("DATA_PARTITION", "").strip()
        if deselected and not partition:
            raise RegistrationError("DATA_PARTITION is required to inspect deselected workflows")
        wait_for_parse(airflow_url, set(wanted), set(families), set(deselected))
        workflow_token = osdu_token()
        for dag_id, family in sorted(wanted.items()):
            print(f"{dag_id}: {register(workflow_url, workflow_token, dag_id, family)}", flush=True)
        api_token = airflow_token(airflow_url)
        for dag_id in sorted(wanted):
            unpause(airflow_url, api_token, dag_id)
            print(f"{dag_id}: unpaused", flush=True)
        for dag_id, family in sorted(deselected.items()):
            deregister(
                workflow_url,
                workflow_token,
                dag_id,
                family,
                airflow_url=airflow_url,
                airflow_access_token=api_token,
                partition=partition,
            )
    except (RegistrationError, OSError, KeyError, yaml.YAMLError) as error:
        print(f"Workflow registration failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
