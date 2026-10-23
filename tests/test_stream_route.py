import json
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.analysis.schemas import RequestAnalysis
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.gateway import GatewayService


class StreamingProvider:
    def __init__(self, texts: list[str], error: Exception | None = None) -> None:
        self.texts = texts
        self.error = error

    async def stream(self, **_: Any) -> Any:
        for text in self.texts:
            yield SimpleNamespace(
                model="m",
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
            )
        if self.error:
            raise self.error


class SimpleAnalyzer:
    async def analyze(self, user_input: str) -> RequestAnalysis:
        return RequestAnalysis(
            contains_personal_data=False,
            complexity="simple",
            task_type="general",
            reason="test",
        )


def client_for(provider: StreamingProvider) -> TestClient:
    service = GatewayService(
        settings=Settings(local_model="local-model", output_pii_action="off"),
        local_provider=provider,  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=SimpleAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(cloud_enabled=False),
    )
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    return TestClient(app)


def read_events(response: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_route_emits_deltas_and_result(monkeypatch) -> None:
    client = client_for(StreamingProvider(["a", "b"]))

    response = client.post(
        "/v1/ugen/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    events = read_events(response)
    assert [event["type"] for event in events][-3:] == ["delta", "delta", "result"]
    assert events[-1]["data"]["content"] == "ab"


def test_stream_route_reports_errors_after_partial_output(monkeypatch) -> None:
    client = client_for(StreamingProvider(["a"], error=RuntimeError("boom")))

    response = client.post(
        "/v1/ugen/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    events = read_events(response)
    assert events[-2] == {"type": "delta", "content": "a"}
    assert events[-1]["type"] == "error"


def test_stream_errors_do_not_expose_upstream_details() -> None:
    client = client_for(
        StreamingProvider(
            [], error=RuntimeError("upstream said: key sk-secret invalid")
        )
    )

    response = client.post(
        "/v1/ugen/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    error = read_events(response)[-1]
    assert error == {"type": "error", "detail": "Model provider request failed"}
