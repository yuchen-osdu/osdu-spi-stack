# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The shared service chart renders env items as either a value or a valueFrom.

Skipped when Helm is not installed.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from spi.shell import run_process

CHART_DIR = Path(__file__).resolve().parent.parent / "software" / "charts" / "osdu-spi-service"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="Helm not installed")


def _container_env(values: dict) -> list[dict]:
    result = run_process(
        ["helm", "template", "chart-env-test", str(CHART_DIR), "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    deployment = next(
        doc for doc in yaml.safe_load_all(result.stdout) if doc and doc["kind"] == "Deployment"
    )
    return deployment["spec"]["template"]["spec"]["containers"][0]["env"]


def test_env_renders_values_and_secret_references():
    secret_ref = {"secretKeyRef": {"name": "workflow-airflow-credentials", "key": "password"}}
    env = _container_env(
        {
            "env": [
                {"name": "PLAIN", "value": "true"},
                {"name": "FROM_SECRET", "valueFrom": secret_ref},
            ]
        }
    )

    assert {"name": "PLAIN", "value": "true"} in env
    assert {"name": "FROM_SECRET", "valueFrom": secret_ref} in env
