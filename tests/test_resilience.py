import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from tests.test_gateway import (
    FakeAnalyzer,
    FakeProvider,
    analysis,
    collect,
    make_service,
    text_chunk,
)
from ugen_ai_gateway.api.schemas import ChatRequest
from ugen_ai_gateway.services import resilience
from ugen_ai_gateway.services.errors import ProviderOutage, ProviderUnavailable
from ugen_ai_gateway.services.resilience import CircuitBreaker, is_outage

REQUEST = httpx.Request("POST", "https://provider.test/v1/chat/completions")


def connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=REQUEST)


def status_error(code: int) -> openai.APIStatusError:
    return openai.APIStatusError(
        "failed", response=httpx.Response(code, request=REQUEST), body=None
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (connection_error(), True),
        (openai.APITimeoutError(request=REQUEST), True),
        (TimeoutError(), True),
        (status_error(503), True),
        (status_error(429), True),
        (status_error(400), False),
        (status_error(401), False),
        (RuntimeError("bad"), False),
    ],
)
def test_is_outage(error: BaseException, expected: bool) -> None:
    assert is_outage(error) is expected


def test_circuit_breaker_opens_then_probes_after_cooldown(monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(resilience.time, "monotonic", lambda: now[0])
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10)

    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()

    now[0] = 10.0
    assert breaker.state == "half_open" and breaker.allow()
    breaker.record_failure()
    assert breaker.state == "open"

    now[0] = 25.0
    breaker.record_success()
    assert breaker.state == "closed"


def complex_request(
    selection: str = "auto", content: str = "solve this"
) -> ChatRequest:
    return ChatRequest(
        provider_selection=selection, messages=[{"role": "user", "content": content}]
    )


def test_cloud_outage_falls_back_to_local() -> None:
    analyzer = FakeAnalyzer(analysis(contains_personal_data=True, complexity="complex"))
    local = FakeProvider(
        completion=SimpleNamespace(
            model="local-m",
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hi [EMAIL_1]"))],
        )
    )
    service, _, cloud, _ = make_service(
        analyzer=analyzer,
        local_provider=local,
        cloud_provider=FakeProvider(error=connection_error()),
    )

    response = asyncio.run(service.chat(complex_request(content="alex@example.com")))

    assert response.provider == "local"
    assert response.routing.fallback_from == "cloud"
    assert "fell back" in response.routing.reason
    assert response.content == "Hi alex@example.com"
    assert local.calls[0]["messages"][-1]["content"] == "[EMAIL_1]"
    assert len(cloud.calls) == 1


def test_fallback_is_reported_as_an_event() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    local = FakeProvider()
    local.chunks = [text_chunk("ok")]
    service, _, _, _ = make_service(
        analyzer=analyzer,
        local_provider=local,
        cloud_provider=FakeProvider(error=status_error(503)),
    )

    events = asyncio.run(collect(service, complex_request()))

    types = [event.type for event in events]
    assert types.index("fallback") < types.index("delta")
    fallback = events[types.index("fallback")]
    assert fallback.data["from"] == "cloud" and fallback.data["to"] == "local"
    assert types.count("generation_started") == 2


def test_explicit_cloud_selection_does_not_fall_back() -> None:
    service, local, _, _ = make_service(
        cloud_provider=FakeProvider(error=connection_error())
    )

    with pytest.raises(ProviderOutage):
        asyncio.run(service.chat(complex_request("cloud")))
    assert local.calls == []


def test_rejected_requests_do_not_fall_back() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, _, _ = make_service(
        analyzer=analyzer, cloud_provider=FakeProvider(error=status_error(400))
    )

    with pytest.raises(RuntimeError, match="Model provider request failed"):
        asyncio.run(service.chat(complex_request()))
    assert local.calls == []


def test_fallback_to_local_can_be_disabled() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, _, _ = make_service(
        analyzer=analyzer,
        cloud_provider=FakeProvider(error=connection_error()),
        fallback_to_local=False,
    )

    with pytest.raises(ProviderOutage):
        asyncio.run(service.chat(complex_request()))
    assert local.calls == []


@pytest.mark.parametrize(
    ("enabled", "has_personal_data", "falls_back"),
    [(False, False, False), (True, True, False), (True, False, True)],
)
def test_local_to_cloud_fallback_is_opt_in_and_never_carries_personal_data(
    enabled: bool, has_personal_data: bool, falls_back: bool
) -> None:
    analyzer = FakeAnalyzer(analysis(contains_personal_data=has_personal_data))
    service, _, cloud, _ = make_service(
        analyzer=analyzer,
        local_provider=FakeProvider(error=connection_error()),
        fallback_to_cloud=enabled,
    )
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    if falls_back:
        assert asyncio.run(service.chat(request)).provider == "cloud"
    else:
        with pytest.raises(ProviderOutage):
            asyncio.run(service.chat(request))
        assert cloud.calls == []


def test_local_to_cloud_fallback_needs_model_analysis() -> None:
    service, _, cloud, _ = make_service(
        local_provider=FakeProvider(error=connection_error()),
        fallback_to_cloud=True,
        analysis_mode="hybrid",
    )

    with pytest.raises(ProviderOutage):
        asyncio.run(
            service.chat(ChatRequest(messages=[{"role": "user", "content": "hi"}]))
        )
    assert cloud.calls == []


def test_open_circuit_skips_the_provider_entirely() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, cloud, _ = make_service(
        analyzer=analyzer,
        cloud_provider=FakeProvider(error=connection_error()),
        circuit_failure_threshold=2,
    )

    for _ in range(3):
        asyncio.run(service.chat(complex_request()))

    assert len(cloud.calls) == 2
    assert len(local.calls) == 3
    assert service.provider_status() == {"local": "closed", "cloud": "open"}


def test_open_circuit_without_fallback_reports_unavailable() -> None:
    service, _, cloud, _ = make_service(
        cloud_provider=FakeProvider(error=connection_error()),
        circuit_failure_threshold=1,
    )
    with pytest.raises(ProviderOutage):
        asyncio.run(service.chat(complex_request("cloud")))

    with pytest.raises(ProviderUnavailable, match="temporarily unavailable"):
        asyncio.run(service.chat(complex_request("cloud")))
    assert len(cloud.calls) == 1


class FailsMidStream:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[Any] = []

    async def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        yield text_chunk(self.text)
        raise connection_error()


def test_no_fallback_after_text_reached_the_client() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    long_text = "This part of the answer was already shown to the user. "
    service, local, _, _ = make_service(
        analyzer=analyzer,
        cloud_provider=FailsMidStream(long_text),  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderOutage):
        asyncio.run(collect(service, complex_request()))
    assert local.calls == []


def test_text_held_back_by_the_output_guard_is_dropped_on_fallback() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    local = FakeProvider()
    local.chunks = [text_chunk("Local answer.")]
    service, _, _, _ = make_service(
        analyzer=analyzer,
        local_provider=local,
        cloud_provider=FailsMidStream("partial "),  # type: ignore[arg-type]
        output_pii_action="redact",
    )

    events = asyncio.run(collect(service, complex_request()))

    deltas = "".join(e.data["content"] for e in events if e.type == "delta")
    assert deltas == "Local answer."
    assert events[-1].response.content == "Local answer."
