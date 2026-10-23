"""Exercise the OpenAI-compatible endpoints through the official OpenAI SDK."""

from types import SimpleNamespace
from typing import Any

import openai
import pytest
from fastapi.testclient import TestClient

from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.analysis.schemas import RequestAnalysis
from ugen_ai_gateway.api import access
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.gateway import GatewayService


class EchoProvider:
    """Replies with fixed text and records what it was sent."""

    def __init__(self, name: str, reply: str = "Hello there") -> None:
        self.name = name
        self.reply = reply
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model=f"{self.name}-resolved",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.reply), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )

    async def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        for index in range(0, len(self.reply), 4):
            yield SimpleNamespace(
                model=f"{self.name}-resolved",
                usage=None,
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=self.reply[index : index + 4]),
                        finish_reason=None,
                    )
                ],
            )
        yield SimpleNamespace(
            model=f"{self.name}-resolved",
            usage=None,
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None), finish_reason="stop"
                )
            ],
        )
        yield SimpleNamespace(
            model=f"{self.name}-resolved",
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=3, total_tokens=7),
            choices=[],
        )


class SimpleAnalyzer:
    async def analyze(self, user_input: str) -> RequestAnalysis:
        return RequestAnalysis(
            contains_personal_data="@" in user_input,
            personal_data_types=["email"] if "@" in user_input else [],
            complexity="complex" if "design" in user_input else "simple",
            task_type="general",
            reason="test",
        )


@pytest.fixture
def gateway(monkeypatch) -> Any:
    settings = Settings(
        local_model="local-model",
        cloud_model="cloud-model",
        cloud_api_key="cloud-key",
        gateway_api_key="",
    )
    monkeypatch.setattr("ugen_ai_gateway.api.access.get_settings", lambda: settings)
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
    http_client = TestClient(app)
    client = openai.OpenAI(
        api_key="unused", base_url="http://testserver/v1", http_client=http_client
    )
    return SimpleNamespace(
        client=client,
        local=local,
        cloud=cloud,
        settings=settings,
        http=http_client,
        service=service,
    )


def test_chat_completion_matches_openai_shape(gateway: Any) -> None:
    raw = gateway.client.chat.completions.with_raw_response.create(
        model="ugen-auto", messages=[{"role": "user", "content": "hi"}]
    )
    completion = raw.parse()

    assert completion.object == "chat.completion"
    assert completion.choices[0].message.content == "Hello there"
    assert completion.choices[0].finish_reason == "stop"
    assert completion.model == "local-resolved"
    assert completion.usage.total_tokens == 6
    assert raw.headers["x-ugen-provider"] == "local"
    assert completion.model_extra["ugen"]["routing"]["provider"] == "local"
    assert gateway.cloud.calls == []


def test_streaming_completion_yields_sdk_chunks_and_usage(gateway: Any) -> None:
    stream = gateway.client.chat.completions.create(
        model="ugen-auto",
        messages=[{"role": "user", "content": "please design a system"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = list(stream)

    text = "".join(
        chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices
    )
    assert text == "Hello there"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert [c.choices[0].finish_reason for c in chunks if c.choices][-1] == "stop"
    assert chunks[-1].choices == []
    assert chunks[-1].usage.completion_tokens == 3
    assert gateway.cloud.calls and gateway.local.calls == []


def test_model_aliases_and_configured_names_pick_the_provider(gateway: Any) -> None:
    for model, provider in [
        ("ugen-cloud", "cloud"),
        ("ugen-local", "local"),
        ("cloud-model", "cloud"),
        ("local-model", "local"),
    ]:
        raw = gateway.client.chat.completions.with_raw_response.create(
            model=model, messages=[{"role": "user", "content": "hi"}]
        )
        assert raw.headers["x-ugen-provider"] == provider


def test_unknown_model_returns_openai_not_found(gateway: Any) -> None:
    with pytest.raises(openai.NotFoundError) as raised:
        gateway.client.chat.completions.create(
            model="gpt-nope", messages=[{"role": "user", "content": "hi"}]
        )
    assert raised.value.body["code"] == "model_not_found"


def test_tool_calling_is_rejected_clearly(gateway: Any) -> None:
    with pytest.raises(openai.BadRequestError, match="Tool calling"):
        gateway.client.chat.completions.create(
            model="ugen-auto",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {"name": "f", "parameters": {"type": "object"}},
                }
            ],
        )


def test_generation_options_are_forwarded_to_the_provider(gateway: Any) -> None:
    gateway.client.chat.completions.create(
        model="ugen-local",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=50,
        top_p=0.5,
        stop=["END"],
        temperature=0.9,
    )

    call = gateway.local.calls[0]
    assert call["max_tokens"] == 50
    assert call["top_p"] == 0.5
    assert call["stop"] == ["END"]
    assert call["temperature"] == 0.9


def test_personal_data_is_redacted_upstream_and_restored_for_the_client(
    gateway: Any,
) -> None:
    gateway.cloud.reply = "Sent to [EMAIL_1]."

    completion = gateway.client.chat.completions.create(
        model="ugen-cloud",
        messages=[{"role": "user", "content": "Email alex@example.com"}],
    )

    assert completion.choices[0].message.content == "Sent to alex@example.com."
    assert "alex@example.com" not in str(gateway.cloud.calls[0]["messages"])


def test_bearer_token_is_accepted_as_the_gateway_key(gateway: Any) -> None:
    gateway.settings.gateway_api_key = "secret"
    authed = gateway.client.with_options(api_key="secret")

    assert authed.chat.completions.create(
        model="ugen-auto", messages=[{"role": "user", "content": "hi"}]
    ).choices
    with pytest.raises(openai.AuthenticationError):
        gateway.client.with_options(api_key="wrong").chat.completions.create(
            model="ugen-auto", messages=[{"role": "user", "content": "hi"}]
        )


def test_models_endpoint_lists_aliases_and_configured_models(gateway: Any) -> None:
    ids = [model.id for model in gateway.client.models.list()]

    assert ids == [
        "ugen-auto",
        "ugen-local",
        "ugen-cloud",
        "local-model",
        "cloud-model",
    ]


def test_streaming_request_errors_get_an_http_status(gateway: Any) -> None:
    del gateway.service._providers["cloud"]

    with pytest.raises(openai.APIStatusError) as raised:
        gateway.client.chat.completions.create(
            model="ugen-cloud",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    assert raised.value.status_code == 503


def test_dashboard_config_endpoint(gateway: Any) -> None:
    body = gateway.http.get("/v1/ugen/config").json()

    assert body["local_model"] == "local-model"
    assert body["cloud_enabled"] is True
    assert body["redact_local"] is True


def test_streaming_fallback_is_reported_in_headers(gateway: Any) -> None:
    import httpx

    gateway.cloud.error = openai.APIConnectionError(
        request=httpx.Request("POST", "https://cloud.test")
    )

    with gateway.client.chat.completions.with_streaming_response.create(
        model="ugen-auto",
        messages=[{"role": "user", "content": "please design a system"}],
        stream=True,
    ) as raw:
        text = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in raw.parse()
            if chunk.choices
        )
        headers = raw.headers

    assert text == "Hello there"
    assert headers["x-ugen-provider"] == "local"
    assert headers["x-ugen-fallback-from"] == "cloud"


def test_routing_config_models_are_listed_and_can_be_pinned(monkeypatch) -> None:
    from tests.test_routing import config

    local, gemini = EchoProvider("local"), EchoProvider("gemini")
    service = GatewayService(
        settings=Settings(),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=SimpleAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        routing=config(),
        providers={"ollama": local, "gemini": gemini},  # type: ignore[dict-item]
    )
    monkeypatch.setattr(
        "ugen_ai_gateway.api.access.get_settings", lambda: Settings(gateway_api_key="")
    )
    access.rate_limiter.clear()
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    client = openai.OpenAI(
        api_key="x", base_url="http://testserver/v1", http_client=TestClient(app)
    )

    ids = [model.id for model in client.models.list()]
    raw = client.chat.completions.with_raw_response.create(
        model="pro", messages=[{"role": "user", "content": "hi"}]
    )

    assert ids == ["ugen-auto", "ugen-local", "ugen-cloud", "qwen", "flash", "pro"]
    assert gemini.calls[0]["model"] == "pro"
    assert raw.headers["x-ugen-provider"] == "cloud"


def test_non_ascii_rule_names_do_not_break_response_headers(monkeypatch) -> None:
    from urllib.parse import unquote

    from tests.test_routing import config

    local, gemini = EchoProvider("local"), EchoProvider("gemini")
    service = GatewayService(
        settings=Settings(),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=SimpleAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        routing=config(
            rules=[{"name": "長文送雲端", "when": {}, "route": {"provider": "gemini"}}]
        ),
        providers={"ollama": local, "gemini": gemini},  # type: ignore[dict-item]
    )
    access.rate_limiter.clear()
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    client = TestClient(app)

    for stream in (False, True):
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "ugen-auto",
                "stream": stream,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert response.status_code == 200
        assert unquote(response.headers["x-ugen-route-reason"]) == (
            "Matched rule '長文送雲端'."
        )


def test_text_content_parts_are_accepted(gateway: Any) -> None:
    completion = gateway.client.chat.completions.create(
        model="ugen-local",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi there"}]}],
    )

    assert completion.choices[0].message.content == "Hello there"
