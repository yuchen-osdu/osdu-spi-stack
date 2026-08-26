from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _kustomizations(relative_path: str) -> list[dict]:
    path = ROOT / relative_path
    return [
        document
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if document and document.get("kind") == "Kustomization"
    ]


def _by_name(documents: list[dict], name: str) -> dict:
    matches = [
        document for document in documents if document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def _dependencies(document: dict) -> set[str]:
    return {dependency["name"] for dependency in document.get("spec", {}).get("dependsOn", [])}


def _resources(relative_path: str) -> set[str]:
    path = ROOT / relative_path
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return set(document.get("resources", []))


def test_core_profile_gateway_kustomization_is_the_orphaned_placeholder():
    """The live Gateway is owned only by the ingress tree's spi-gateway-tls.

    The core spi-gateway Kustomization is retained solely to orphan the old
    Flux inventory without pruning the Gateway during the ownership handoff.
    """
    core = _kustomizations("software/stacks/osdu/profiles/core/stack.yaml")

    gateway = _by_name(core, "spi-gateway")
    assert gateway["spec"]["path"] == "./software/components/inventory-handoff"
    assert gateway["spec"]["prune"] is False
    assert gateway["spec"]["deletionPolicy"] == "Orphan"


def test_each_ingress_profile_has_exactly_one_gateway_owner():
    expected_paths = {
        "azure": "./software/overlays/gateway-tls-single-host",
        "dns": "./software/overlays/gateway-tls-multi-host",
        "ip": "./software/components/gateway",
    }

    for mode, expected_path in expected_paths.items():
        documents = _kustomizations(f"software/stacks/osdu/ingress/{mode}/stack.yaml")
        gateway = _by_name(documents, "spi-gateway-tls")

        assert gateway["spec"]["path"] == expected_path
        assert all(document["metadata"]["name"] != "spi-gateway" for document in documents)
        assert "spi-gateway-tls" in _dependencies(_by_name(documents, "spi-osdu-routes"))


def test_tls_profiles_order_issuer_before_gateway():
    expected_gateway_deps = {
        "azure": {"spi-cert-manager-issuers", "spi-ingress-dns-label"},
        "dns": {"spi-cert-manager-issuers"},
    }
    for mode, expected in expected_gateway_deps.items():
        documents = _kustomizations(f"software/stacks/osdu/ingress/{mode}/stack.yaml")
        issuers = _by_name(documents, "spi-cert-manager-issuers")
        gateway = _by_name(documents, "spi-gateway-tls")

        assert _dependencies(issuers) == {"spi-cert-manager"}
        assert _dependencies(gateway) == expected


def test_tls_overlays_retain_http_gateway_base_for_acme():
    for overlay in ("gateway-tls-single-host", "gateway-tls-multi-host"):
        assert "../../components/gateway" in _resources(
            f"software/overlays/{overlay}/kustomization.yaml"
        )
