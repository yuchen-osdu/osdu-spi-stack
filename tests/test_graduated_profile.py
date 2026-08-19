from pathlib import Path

import yaml

from spi.config import IngressMode, Profile

ROOT = Path(__file__).parents[1]
OSDU = ROOT / "software" / "stacks" / "osdu"


def _flux_documents(path: Path) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if document and document.get("kind") == "Kustomization"
    ]


def test_every_cli_profile_resolves_to_a_real_kustomize_tree():
    for profile in Profile:
        path = (
            OSDU / "profiles" / (Profile.CORE.value if profile == Profile.FULL else profile.value)
        )
        assert (path / "kustomization.yaml").is_file(), profile


def test_graduated_profile_is_cumulative_and_adds_one_wellbore_layer():
    kustomization = yaml.safe_load(
        (OSDU / "profiles" / "graduated" / "kustomization.yaml").read_text(encoding="utf-8")
    )
    assert kustomization["resources"] == ["../core", "stack.yaml"]

    documents = _flux_documents(OSDU / "profiles" / "graduated" / "stack.yaml")
    assert [document["metadata"]["name"] for document in documents] == ["spi-wellbore-services"]
    assert documents[0]["spec"]["dependsOn"] == [{"name": "spi-osdu-reference"}]
    assert documents[0]["spec"]["path"] == "./software/stacks/osdu/services-graduated"


def test_graduated_ingress_variants_exist_for_every_mode():
    for mode in IngressMode:
        path = OSDU / "ingress" / f"{mode.value}-graduated"
        assert (path / "kustomization.yaml").is_file(), mode
        documents = _flux_documents(path / "stack.yaml")
        assert [document["metadata"]["name"] for document in documents] == ["spi-wellbore-routes"]


def test_worker_is_internal_only_and_main_is_the_only_http_route():
    services = (OSDU / "services-graduated" / "wellbore-domain-services-worker.yaml").read_text(
        encoding="utf-8"
    )
    policy = (OSDU / "services-graduated" / "wellbore-worker-policy.yaml").read_text(
        encoding="utf-8"
    )
    routes = "\n".join(
        (OSDU / "routes" / mode / "wellbore" / "route.yaml").read_text(encoding="utf-8")
        for mode in ("single-host", "multi-host", "ip-only")
    )

    assert "SERVICE_HOST_PARTITION" in services
    assert "kind: NetworkPolicy" in policy
    assert "kind: AuthorizationPolicy" in policy
    assert "requestPrincipals:" in policy
    assert policy.count("app.kubernetes.io/name: osdu-wellbore-domain-services-worker") == 2
    assert "app.kubernetes.io/name: wellbore-domain-services-worker\n" not in policy
    assert "name: wellbore-domain-services-worker" not in routes
    assert "name: wellbore-domain-services" in routes
    assert "/api/os-wellbore-ddms/" in routes


def test_generic_chart_supports_worker_client_pod_label():
    template = (
        ROOT / "software" / "charts" / "osdu-spi-service" / "templates" / "deployment.yaml"
    ).read_text(encoding="utf-8")

    assert ".Values.podLabels" in template
