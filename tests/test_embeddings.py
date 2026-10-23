from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient

from tests.test_openai_api import EchoProvider, SimpleAnalyzer
from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.api import access
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service, get_usage_store
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.gateway import GatewayService
from ugen_ai_gateway.services.usage import UsageStore


class EmbeddingProvider(EchoProvider):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.embedded: list[dict[str, Any]] = []

    async def embed(self, **kwargs: Any) -> Any:
        self.embedded.append(kwargs)
        inputs = (
            [kwargs["input"]] if isinstance(kwargs["input"], str) else kwargs["input"]
        )
        return SimpleNamespace(
            model_dump=lambda: {
                "object": "list",
                "model": kwargs["model"],
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]}
                    for i, _ in enumerate(inputs)
                ],
                "usage": {"prompt_tokens": 7, "total_tokens": 7},
            },
            usage=SimpleNamespace(prompt_tokens=7),
        )


def make(monkeypatch, embedding_model: str = "nomic-embed-text") -> Any:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        local_model="local-model",
        cloud_model="cloud-model",
        cloud_api_key="cloud-key",
        embedding_model=embedding_model,
    )
    for module in ("access",):
        monkeypatch.setattr(
            f"ugen_ai_gateway.api.{module}.get_settings", lambda: settings
        )
    access.rate_limiter.clear()
    local, cloud = EmbeddingProvider("local"), EmbeddingProvider("cloud")
    store = UsageStore(":memory:")
    service = GatewayService(
        settings=settings,
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud,  # type: ignore[arg-type]
        analyzer=SimpleAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(cloud_enabled=True),
        usage_store=store,
    )
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    app.dependency_overrides[get_usage_store] = lambda: store
    client = openai.OpenAI(
        api_key="unused", base_url="http://testserver/v1", http_client=TestClient(app)
    )
    return client, local, cloud, store


def test_embeddings_run_on_the_local_model_only(monkeypatch) -> None:
    client, local, cloud, store = make(monkeypatch)
    result = client.embeddings.create(
        model="ugen-embed", input=["alex@example.com", "two"]
    )

    assert len(result.data) == 2 and result.data[0].embedding == [0.1, 0.2, 0.3]
    assert local.embedded[-1]["model"] == "nomic-embed-text"
    # Indexed text never leaves the machine, so it is sent as written.
    assert local.embedded[-1]["input"] == ["alex@example.com", "two"]
    assert cloud.embedded == []
    assert "ugen-embed" in [model.id for model in client.models.list()]
    import asyncio

    stats = asyncio.run(store.stats(days=1))
    assert stats["requests"] == 1 and stats["prompt_tokens"] == 7


def test_the_configured_model_name_also_works(monkeypatch) -> None:
    client, local, _, _ = make(monkeypatch)
    client.embeddings.create(model="nomic-embed-text", input="hello")
    assert local.embedded[-1]["input"] == "hello"


def test_unknown_or_unconfigured_embedding_model(monkeypatch) -> None:
    client, _, _, _ = make(monkeypatch)
    with pytest.raises(openai.NotFoundError):
        client.embeddings.create(model="text-embedding-3-small", input="x")
    client, _, _, _ = make(monkeypatch, embedding_model="")
    with pytest.raises(openai.APIStatusError) as raised:
        client.embeddings.create(model="ugen-embed", input="x")
    assert "Models page" in str(raised.value)
