# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0.

"""The workflow service's Airflow login and its backfill on reconcile."""

from unittest.mock import patch

from spi import secrets

_SEED = {key: f"{key}-value" for key in secrets.SEED_KEYS}


def _written() -> tuple[dict, list]:
    applied: dict = {}
    seeds: list = []
    with (
        patch(
            "spi.secrets._kubectl_apply_secret",
            side_effect=lambda ns, name, data: applied.__setitem__((ns, name), data),
        ),
        patch("spi.secrets._create_seed", side_effect=seeds.append),
    ):
        secrets.refresh_workflow_airflow_credentials()
    return applied, seeds


def test_ensure_secrets_writes_the_workflow_login_on_both_sides():
    applied: dict = {}
    with (
        patch("spi.secrets.get_or_create_seed", return_value=dict(_SEED)),
        patch(
            "spi.secrets._kubectl_apply_secret",
            side_effect=lambda ns, name, data: applied.__setitem__((ns, name), data),
        ),
    ):
        secrets.ensure_secrets()

    password = _SEED["airflow_workflow_password"]
    assert applied[("platform", "airflow-api-credentials")]["workflow-password"] == password
    assert applied[("osdu", "workflow-airflow-credentials")] == {
        "username": "osdu-workflow",
        "password": password,
    }


def test_reconcile_extends_a_seed_that_predates_the_workflow_login():
    old_seed = {k: v for k, v in _SEED.items() if k != "airflow_workflow_password"}
    with patch("spi.secrets._get_seed", return_value=dict(old_seed)):
        applied, seeds = _written()

    assert len(seeds) == 1
    assert {k: seeds[0][k] for k in old_seed} == old_seed
    password = seeds[0]["airflow_workflow_password"]
    assert password
    assert applied[("osdu", "workflow-airflow-credentials")]["password"] == password
    platform = applied[("platform", "airflow-api-credentials")]
    assert platform["password"] == _SEED["airflow_admin_password"]
    assert platform["workflow-password"] == password


def test_reconcile_reuses_an_existing_workflow_login():
    with patch("spi.secrets._get_seed", return_value=dict(_SEED)):
        applied, seeds = _written()

    assert seeds == []
    assert (
        applied[("osdu", "workflow-airflow-credentials")]["password"]
        == _SEED["airflow_workflow_password"]
    )


def test_reconcile_never_regenerates_an_unreadable_seed():
    for unreadable in (None, {"elastic_password": "x"}):
        with patch("spi.secrets._get_seed", return_value=unreadable):
            applied, seeds = _written()

        assert applied == {}
        assert seeds == []
