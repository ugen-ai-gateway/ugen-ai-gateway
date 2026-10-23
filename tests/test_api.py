import asyncio
from importlib import import_module

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from tests.test_gateway import make_service
from ugen_ai_gateway.api.routes import create_chat, health
from ugen_ai_gateway.api.schemas import ChatRequest, ChatResponse, RoutingInfo
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.gateway import GatewayService
from ugen_ai_gateway.services.keys import Principal


def make_http_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/ugen/chat",
            "headers": [],
            "client": ("handler-test", 1234),
            "server": ("test-server", 80),
            "scheme": "http",
            "query_string": b"",
            "http_version": "1.1",
        }
    )


class FakeGateway(GatewayService):
    def __init__(self, result: ChatResponse | Exception) -> None:
        self.result = result

    async def chat(
        self, request: ChatRequest, *_: object, **__: object
    ) -> ChatResponse:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def allow(*_: object) -> Principal:
    return Principal(api_key_id=None)


def test_health_returns_ok() -> None:
    assert asyncio.run(health()) == {"status": "ok"}


def test_api_access_requires_key_and_applies_per_ip_rate_limit(monkeypatch) -> None:
    routes_module = import_module("ugen_ai_gateway.api.access")
    settings = Settings(
        gateway_api_key="secret",
        rate_limit_requests=1,
        local_model="configured",
    )

    monkeypatch.setattr(routes_module, "get_settings", lambda: settings)
    routes_module.rate_limiter.clear()

    def request(headers: list[tuple[bytes, bytes]]) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/ugen/chat",
                "headers": headers,
                "client": ("test-client", 1234),
                "server": ("test-server", 80),
                "scheme": "http",
                "query_string": b"",
                "http_version": "1.1",
            }
        )

    with pytest.raises(HTTPException) as unauthorized:
        asyncio.run(routes_module.authenticate(request([]), None))
    assert unauthorized.value.status_code == 401

    asyncio.run(routes_module.authenticate(request([(b"x-api-key", b"secret")]), None))
    with pytest.raises(HTTPException) as rate_limited:
        asyncio.run(
            routes_module.authenticate(request([(b"x-api-key", b"secret")]), None)
        )
    assert rate_limited.value.status_code == 429


def test_chat_handler_returns_gateway_response(monkeypatch) -> None:
    routes_module = import_module("ugen_ai_gateway.api.routes")
    monkeypatch.setattr(routes_module, "authenticate", allow)
    expected = ChatResponse(
        model="m",
        content="hello",
        provider="local",
        routing=RoutingInfo(
            complexity="simple",
            task_type="general",
            contains_personal_data=False,
            personal_data_types=[],
            provider="local",
            reason="Default local routing.",
        ),
    )
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    response = asyncio.run(
        create_chat(request, FakeGateway(expected), None, make_http_request())
    )

    assert response == expected


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (ValueError("model not configured"), 503, "model not configured"),
        (RuntimeError("provider offline"), 502, "Model provider request failed"),
    ],
)
def test_chat_handler_maps_errors_to_http(
    error: Exception, status_code: int, detail: str, monkeypatch
) -> None:
    routes_module = import_module("ugen_ai_gateway.api.routes")
    monkeypatch.setattr(routes_module, "authenticate", allow)
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(HTTPException) as raised:
        asyncio.run(create_chat(request, FakeGateway(error), None, make_http_request()))

    assert raised.value.status_code == status_code
    assert raised.value.detail == detail


def test_ready_explains_an_invalid_configuration() -> None:
    def broken() -> None:
        raise ValueError("Model ids and aliases must be unique")

    app = create_app()
    app.dependency_overrides[get_gateway_service] = broken
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert "configuration" in response.json()["reason"]


def test_ready_names_the_unreachable_provider() -> None:
    class Down:
        async def health_check(self) -> None:
            raise ConnectionError("connection refused")

    service, *_ = make_service(local_provider=Down())  # type: ignore[arg-type]
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert (
        response.json()["reason"]
        == "Provider 'local' is not reachable; see the server logs"
    )


def test_ready_reuses_a_recent_provider_check() -> None:
    calls = []

    class Counting:
        async def health_check(self) -> None:
            calls.append(1)

    service, *_ = make_service(local_provider=Counting(), cloud_api_key="")  # type: ignore[arg-type]
    for _ in range(3):
        assert asyncio.run(service.readiness_problem()) is None
    assert len(calls) == 1


def test_responses_carry_security_headers() -> None:
    response = TestClient(create_app()).get("/health")
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_api_docs_can_load_swagger_but_nothing_else_relaxes() -> None:
    client = TestClient(create_app())
    for path in ("/docs", "/api/docs", "/redoc"):
        docs = client.get(path)
        assert docs.status_code == 200, path
        assert "https://cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    health = client.get("/health").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in health and "script-src 'self';" in health
