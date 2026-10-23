from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient

from tests.test_openai_api import EchoProvider, SimpleAnalyzer
from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.api import access
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.routing.config import (
    config_from_settings,
)
from ugen_ai_gateway.services.gateway import GatewayService

HI = [{"role": "user", "content": "hi"}]


@pytest.fixture
def aliased(monkeypatch) -> Any:
    settings = Settings(
        local_model="qwen2.5:7b",
        local_model_alias="house-model",
        cloud_model="gpt-real",
        cloud_model_alias="smart",
        cloud_api_key="cloud-key",
    )
    for module in ("access",):
        monkeypatch.setattr(
            f"ugen_ai_gateway.api.{module}.get_settings", lambda: settings
        )
    access.rate_limiter.clear()
    local, cloud = EchoProvider("local"), EchoProvider("cloud")
    service = GatewayService(
        settings=settings,
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud,  # type: ignore[arg-type]
        analyzer=SimpleAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(cloud_enabled=True),
    )
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    http = TestClient(app)
    client = openai.OpenAI(
        api_key="unused", base_url="http://testserver/v1", http_client=http
    )
    return client, http, local, cloud


def test_alias_replaces_the_model_name_everywhere_clients_look(aliased: Any) -> None:
    client, http, local, _ = aliased

    ids = [model.id for model in client.models.list()]
    assert "house-model" in ids and "smart" in ids
    assert "qwen2.5:7b" not in ids and "gpt-real" not in ids

    raw = client.chat.completions.with_raw_response.create(
        model="ugen-local", messages=HI
    )
    assert raw.parse().model == "house-model"
    assert raw.headers["x-ugen-model"] == "house-model"
    # The provider is still called with the real model name.
    assert local.calls[-1]["model"] == "qwen2.5:7b"

    config = http.get("/v1/ugen/config").json()
    assert config["local_model"] == "house-model"
    assert config["cloud_model"] == "smart"


def test_alias_and_real_name_both_select_the_model(aliased: Any) -> None:
    client, _, _, cloud = aliased
    for name in ("smart", "gpt-real"):
        raw = client.chat.completions.with_raw_response.create(model=name, messages=HI)
        assert raw.headers["x-ugen-provider"] == "cloud"
        assert cloud.calls[-1]["model"] == "gpt-real"


def test_streamed_events_use_the_alias(aliased: Any) -> None:
    _, http, _, _ = aliased
    lines = http.post(
        "/v1/ugen/chat/stream", json={"messages": HI, "provider_selection": "local"}
    ).text.splitlines()
    assert '"model": "qwen2.5:7b"' not in "".join(lines)
    assert any('"model": "house-model"' in line for line in lines)


def test_local_and_cloud_may_share_a_model_name() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        local_model="gemma3:4b",
        cloud_model="gemma3:4b",
        cloud_api_key="key",
    )
    config = config_from_settings(settings, cloud_enabled=True)
    assert [p.models[0].id for p in config.providers] == ["gemma3:4b", "gemma3:4b"]
