# Copyright 2026, Microsoft
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for CI-mode freeze (spi reconcile --suspend/--resume)."""

from unittest import mock

from spi import cli, guard


def test_resolve_flux_namespace_reads_gitrepository_namespace():
    data = {"items": [{"metadata": {"name": "osdu-spi-stack-system", "namespace": "osdu-flux"}}]}
    with mock.patch.object(guard, "kubectl_json", return_value=data):
        assert guard.resolve_flux_namespace() == "osdu-flux"


def test_resolve_flux_namespace_ignores_other_gitrepositories():
    data = {
        "items": [
            {"metadata": {"name": "some-other", "namespace": "flux-system"}},
            {"metadata": {"name": "osdu-spi-stack-system", "namespace": "osdu-flux"}},
        ]
    }
    with mock.patch.object(guard, "kubectl_json", return_value=data):
        assert guard.resolve_flux_namespace() == "osdu-flux"


def test_resolve_flux_namespace_falls_back_when_absent():
    with mock.patch.object(guard, "kubectl_json", return_value=None):
        assert guard.resolve_flux_namespace(default="flux-system") == "flux-system"


def test_set_flux_suspend_freezes_gitrepository_kustomizations_and_helmreleases():
    def fake_kubectl_json(args):
        kind = args[1]
        if kind == "kustomization":
            return {"items": [{"metadata": {"name": "spi-osdu-services"}}]}
        if kind == "helmrelease":
            return {"items": [{"metadata": {"name": "storage"}}]}
        return None

    calls = []
    with mock.patch.object(cli, "kubectl_json", side_effect=fake_kubectl_json), mock.patch.object(
        cli, "run_command", side_effect=lambda cmd, **kw: calls.append(cmd)
    ):
        cli._set_flux_suspend("osdu-flux", True)

    patched = {(c[2], c[3]) for c in calls}
    assert ("gitrepository", "osdu-spi-stack-system") in patched
    assert ("kustomization", "spi-osdu-services") in patched
    assert ("helmrelease", "storage") in patched
    # every patch targets the resolved namespace and sets suspend:true
    for cmd in calls:
        assert "osdu-flux" in cmd
        assert '{"spec":{"suspend":true}}' in cmd


def test_set_flux_suspend_resume_sets_false():
    with mock.patch.object(cli, "kubectl_json", return_value={"items": []}):
        calls = []
        with mock.patch.object(cli, "run_command", side_effect=lambda cmd, **kw: calls.append(cmd)):
            cli._set_flux_suspend("osdu-flux", False)
    # only the GitRepository (no kustomizations/helmreleases returned), suspend:false
    assert any(c[2] == "gitrepository" for c in calls)
    for cmd in calls:
        assert '{"spec":{"suspend":false}}' in cmd
