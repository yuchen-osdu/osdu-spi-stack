from unittest import mock

from spi.deploy import DEFAULT_SERVICE_AUDIENCE, _resolve_aad_client_id
from spi.templates import istio_auth_resources


def test_service_tokens_default_to_arm_audience():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert _resolve_aad_client_id("managed-identity-client-id") == DEFAULT_SERVICE_AUDIENCE


def test_service_token_audience_can_use_an_app_registration():
    with mock.patch.dict("os.environ", {"AAD_CLIENT_ID": "api-app-client-id"}, clear=True):
        assert _resolve_aad_client_id("managed-identity-client-id") == "api-app-client-id"


def test_arm_default_is_not_duplicated_in_istio_audiences():
    resources = istio_auth_resources(
        namespace="osdu",
        tenant_id="tenant-id",
        entra_client_id="managed-identity-client-id",
        aad_client_id=DEFAULT_SERVICE_AUDIENCE,
    )

    assert resources.count('        - "https://management.azure.com"') == 1
