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

"""Register the declared OSDU DAGs with the workflow service and unpause them.

Waits until Airflow has parsed exactly the DAG IDs dag-sources.yaml declares,
with no import error in their families, then registers each as a system
workflow and unpauses it. Every step is idempotent, so a re-run after a
partial failure converges. Any failure exits non-zero.
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


def _send(method: str, url: str, *, token=None, body=None, form=None, expected=(200,)):
    headers = {"Accept": "application/json"}
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
    """Return the parsed DAG IDs and the import errors in the declared families."""
    _, dags = request("GET", f"{url}/api/v2/dags?limit=1000", token=token)
    _, errors = request("GET", f"{url}/api/v2/importErrors?limit=1000", token=token)
    parsed = {dag["dag_id"] for dag in dags.get("dags") or []}
    failures = [
        error["filename"]
        for error in errors.get("import_errors") or []
        if family_of(error["filename"]) in families
    ]
    return parsed, failures


def wait_for_parse(url: str, wanted: set[str], families: set[str]) -> None:
    deadline = time.monotonic() + PARSE_TIMEOUT_SECONDS
    while True:
        parsed, failures = parse_state(url, airflow_token(url), families)
        missing, extra = wanted - parsed, parsed - wanted
        if not missing and not extra and not failures:
            print(f"Airflow parsed exactly the {len(wanted)} declared DAGs", flush=True)
            return
        state = f"missing={sorted(missing)} extra={sorted(extra)} import_errors={failures}"
        if time.monotonic() >= deadline:
            raise RegistrationError(f"Airflow never parsed the declared set: {state}")
        print(f"  waiting: {state}", flush=True)
        time.sleep(POLL_SECONDS)


def register(workflow_url: str, token: str, dag_id: str, family: str) -> str:
    # The Azure workflow engine triggers the DAG named by workflowName.
    status, _ = request(
        "POST",
        f"{workflow_url}/workflow/system",
        token=token,
        body={
            "workflowName": dag_id,
            "description": f"{dag_id} from the {family} DAG family",
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


def main() -> int:
    airflow_url = os.environ["AIRFLOW_URL"].rstrip("/")
    workflow_url = os.environ["WORKFLOW_URL"].rstrip("/")
    try:
        sources = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
        owners = declared_dags(sources)
        wait_for_parse(airflow_url, set(owners), set(owners.values()))
        token = osdu_token()
        for dag_id, family in sorted(owners.items()):
            print(f"{dag_id}: {register(workflow_url, token, dag_id, family)}", flush=True)
        token = airflow_token(airflow_url)
        for dag_id in sorted(owners):
            unpause(airflow_url, token, dag_id)
            print(f"{dag_id}: unpaused", flush=True)
    except (RegistrationError, OSError, KeyError, yaml.YAMLError) as error:
        print(f"Workflow registration failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
