import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.analysis.redaction import PLACEHOLDER_INSTRUCTION
from ugen_ai_gateway.analysis.schemas import RequestAnalysis
from ugen_ai_gateway.api.schemas import ChatRequest
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.services.errors import ModelNotFound
from ugen_ai_gateway.services.gateway import ANALYSIS_BATCH_SEPARATOR, GatewayService


class FakeProvider:
    def __init__(self, completion: Any = None, error: Exception | None = None) -> None:
        self.completion = completion or SimpleNamespace(
            model="resolved-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content="hello"))],
        )
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.chunks: list[Any] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.completion

    async def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


def text_chunk(text: str) -> Any:
    return SimpleNamespace(
        model="streamed-model",
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
    )


def usage_chunk(completion_tokens: int) -> Any:
    return SimpleNamespace(
        model="streamed-model",
        usage=SimpleNamespace(
            prompt_tokens=3,
            completion_tokens=completion_tokens,
            total_tokens=3 + completion_tokens,
        ),
        choices=[],
    )


class FakeAnalyzer:
    def __init__(self, analysis: RequestAnalysis | None = None) -> None:
        self.analysis = analysis or RequestAnalysis(
            contains_personal_data=False,
            personal_data_types=[],
            complexity="simple",
            task_type="general",
            reason="Simple general request.",
        )
        self.inputs: list[str] = []

    async def analyze(self, user_input: str) -> RequestAnalysis:
        self.inputs.append(user_input)
        return self.analysis


def make_service(
    *,
    local_model: str = "local-default",
    cloud_model: str = "cloud-default",
    cloud_api_key: str = "cloud-key",
    local_provider: FakeProvider | None = None,
    cloud_provider: FakeProvider | None = None,
    analyzer: FakeAnalyzer | None = None,
    cloud_enabled: bool = True,
    **settings: Any,
) -> tuple[GatewayService, FakeProvider, FakeProvider, FakeAnalyzer]:
    local = local_provider or FakeProvider()
    cloud = cloud_provider or FakeProvider()
    request_analyzer = analyzer or FakeAnalyzer()
    service = GatewayService(
        settings=Settings(
            local_model=local_model,
            cloud_model=cloud_model,
            cloud_api_key=cloud_api_key,
            **{"analysis_mode": "llm", "analysis_cache_size": 0, **settings},
        ),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=cloud if cloud_api_key else None,  # type: ignore[arg-type]
        analyzer=request_analyzer,  # type: ignore[arg-type]
        policy=RoutingPolicy(cloud_enabled=cloud_enabled),
    )
    return service, local, cloud, request_analyzer


def analysis(
    *, contains_personal_data: bool = False, complexity: str = "simple"
) -> RequestAnalysis:
    return RequestAnalysis(
        contains_personal_data=contains_personal_data,
        personal_data_types=["email"] if contains_personal_data else [],
        complexity=complexity,  # type: ignore[arg-type]
        task_type="general",
        reason="Test analysis.",
    )


def test_chat_uses_local_provider_and_default_model() -> None:
    service, local, cloud, analyzer = make_service()
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    response = asyncio.run(service.chat(request))

    assert response.model == "resolved-model"
    assert response.content == "hello"
    assert response.provider == "local"
    assert response.routing.complexity == "simple"
    assert response.routing.task_type == "general"
    assert response.routing.contains_personal_data is False
    assert response.routing.provider == "local"
    assert (
        response.routing.reason == "Local model selected by the default routing policy."
    )
    assert analyzer.inputs == ["hi"]
    assert local.calls[0]["model"] == "local-default"
    assert cloud.calls == []


def test_chat_uses_cloud_for_complex_request() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, cloud, _ = make_service(analyzer=analyzer)
    request = ChatRequest(messages=[{"role": "user", "content": "solve this"}])

    response = asyncio.run(service.chat(request))

    assert response.provider == "cloud"
    assert response.routing.complexity == "complex"
    assert response.routing.provider == "cloud"
    assert cloud.calls[0]["model"] == "cloud-default"
    assert local.calls == []


def test_chat_redacts_personal_data_and_routes_complex_request_to_cloud() -> None:
    analyzer = FakeAnalyzer(analysis(contains_personal_data=True, complexity="complex"))
    service, local, cloud, _ = make_service(analyzer=analyzer)
    request = ChatRequest(
        messages=[{"role": "user", "content": "Email alex@example.com"}]
    )

    response = asyncio.run(service.chat(request))

    assert response.provider == "cloud"
    assert response.routing.redacted_values == 1
    assert cloud.calls[0]["messages"] == [
        {"role": "system", "content": PLACEHOLDER_INSTRUCTION},
        {"role": "user", "content": "Email [EMAIL_1]"},
    ]
    assert local.calls == []
    assert request.messages[0].get("content") == "Email alex@example.com"


def test_chat_redacts_personal_data_before_sending_to_local_provider() -> None:
    service, local, cloud, _ = make_service()
    request = ChatRequest(messages=[{"role": "user", "content": "Call 0912-345-678"}])

    asyncio.run(service.chat(request))

    assert local.calls[0]["messages"][1:] == [
        {"role": "user", "content": "Call [TAIWAN_MOBILE_1]"}
    ]
    assert cloud.calls == []


@pytest.mark.parametrize("contains_personal_data", [True, False])
@pytest.mark.parametrize("complexity", ["simple", "complex"])
def test_unredactable_personal_data_never_leaves_the_machine(
    contains_personal_data: bool, complexity: str
) -> None:
    analyzer = FakeAnalyzer(
        RequestAnalysis(
            contains_personal_data=contains_personal_data,
            personal_data_types=["name"],
            complexity=complexity,  # type: ignore[arg-type]
            task_type="general",
            reason="A name was detected.",
        )
    )
    service, local, cloud, _ = make_service(analyzer=analyzer)
    request = ChatRequest(messages=[{"role": "user", "content": "My name is Alex"}])

    response = asyncio.run(service.chat(request))

    assert response.provider == "local"
    assert local.calls and cloud.calls == []
    if complexity == "complex":
        assert response.routing.rule == "privacy.unredactable"


def test_with_ner_a_claim_the_model_cannot_point_to_does_not_keep_it_local() -> None:
    """NER looks for names and places in every message before anything is
    sent; a vague "contains a name" from the analysis model, with nothing it
    can point to, no longer keeps a technical request local."""
    from ugen_ai_gateway.analysis.pii import (
        PersonalDataMatch,
        clear_registered_finders,
        register_finder,
    )

    def names(text: str) -> list[PersonalDataMatch]:
        start = text.find("Alex Chen")
        if start < 0:
            return []
        return [
            PersonalDataMatch(
                "person_name", "PERSON_NAME", start, start + 9, "Alex Chen"
            )
        ]

    analyzer = FakeAnalyzer(
        RequestAnalysis(
            contains_personal_data=True,
            personal_data_types=["name"],
            complexity="complex",
            task_type="coding",
            reason="Might contain a name.",
        )
    )
    register_finder(names, types=["person_name"])
    try:
        service, _, cloud, _ = make_service(analyzer=analyzer)
        plain = asyncio.run(
            service.chat(
                ChatRequest(messages=[{"role": "user", "content": "Design a router"}])
            )
        )
        named = asyncio.run(
            service.chat(
                ChatRequest(
                    messages=[
                        {"role": "user", "content": "Design a router for Alex Chen"}
                    ]
                )
            )
        )
    finally:
        clear_registered_finders()

    assert plain.provider == "cloud" and plain.routing.rule != "privacy.unredactable"
    # Names NER finds are still replaced before the cloud sees them.
    assert named.provider == "cloud"
    assert "Alex Chen" not in str(cloud.calls[-1]["messages"])


def test_unredactable_personal_data_is_refused_without_a_local_model() -> None:
    from tests.test_routing import config

    analyzer = FakeAnalyzer(
        RequestAnalysis(
            contains_personal_data=True,
            personal_data_types=["name"],
            complexity="complex",
            task_type="general",
            reason="A name was detected.",
        )
    )
    cloud = FakeProvider()
    service = GatewayService(
        settings=Settings(analysis_mode="llm"),
        local_provider=FakeProvider(),  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=analyzer,  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        routing=config(),
        providers={"gemini": cloud},  # type: ignore[dict-item]
    )

    with pytest.raises(ValueError, match="could not be safely redacted"):
        asyncio.run(
            service.chat(ChatRequest(messages=[{"role": "user", "content": "x " * 80}]))
        )
    assert cloud.calls == []


def test_chat_keeps_complex_request_local_when_cloud_is_disabled() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, cloud, _ = make_service(analyzer=analyzer, cloud_enabled=False)
    request = ChatRequest(messages=[{"role": "user", "content": "solve this"}])

    response = asyncio.run(service.chat(request))

    assert response.provider == "local"
    assert local.calls[0]["model"] == "local-default"
    assert cloud.calls == []


def test_chat_uses_the_requested_configured_model() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, cloud, _ = make_service(analyzer=analyzer)
    request = ChatRequest(
        model="cloud-default",
        messages=[{"role": "user", "content": "solve this"}],
        temperature=0.7,
    )

    response = asyncio.run(service.chat(request))

    assert response.provider == "cloud"
    assert cloud.calls[0]["model"] == "cloud-default"
    assert cloud.calls[0]["temperature"] == 0.7
    assert local.calls == []


def test_chat_rejects_models_that_are_not_configured() -> None:
    service, local, cloud, _ = make_service()
    request = ChatRequest(
        model="o1-pro", messages=[{"role": "user", "content": "solve this"}]
    )

    with pytest.raises(ModelNotFound):
        asyncio.run(service.chat(request))
    assert cloud.calls == [] and local.calls == []


def test_chat_ignores_requested_provider_and_uses_policy() -> None:
    service, local, cloud, _ = make_service()
    request = ChatRequest(
        provider="cloud", messages=[{"role": "user", "content": "hi"}]
    )

    response = asyncio.run(service.chat(request))

    assert response.provider == "local"
    assert local.calls
    assert cloud.calls == []


def test_chat_rejects_missing_local_model() -> None:
    service, local, cloud, analyzer = make_service(local_model="")
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(ValueError, match="No local model is configured"):
        asyncio.run(service.chat(request))

    assert analyzer.inputs == []
    assert local.calls == []
    assert cloud.calls == []


def test_chat_requires_text_user_message() -> None:
    service, local, cloud, analyzer = make_service()
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}
                ],
            }
        ]
    )

    with pytest.raises(ValueError, match="A text user message is required"):
        asyncio.run(service.chat(request))

    assert analyzer.inputs == []
    assert local.calls == []
    assert cloud.calls == []


def test_chat_analyzes_user_text_from_strings_and_text_parts() -> None:
    service, _, _, analyzer = make_service()
    request = ChatRequest(
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": [{"type": "text", "text": "from a part"}]},
            {"role": "user", "content": "second"},
        ]
    )

    asyncio.run(service.chat(request))

    # The latest message (a short follow-up, so together with the message
    # before it) in one call; older ones together in another.
    assert analyzer.inputs == [
        "from a part\n\nsecond",
        ANALYSIS_BATCH_SEPARATOR.join(["system prompt", "first", "from a part"]),
    ]


class ValueAnalyzer(FakeAnalyzer):
    """Reports a name when the text mentions it, like the analysis model."""

    async def analyze(self, user_input: str) -> RequestAnalysis:
        self.inputs.append(user_input)
        found = "Alex Chen" in user_input
        return self.analysis.model_copy(
            update={
                "contains_personal_data": found,
                "personal_data_types": ["name"] if found else [],
                "personal_data_values": ["Alex Chen"] if found else [],
            }
        )


def test_long_conversations_need_at_most_two_analysis_calls() -> None:
    analyzer = ValueAnalyzer()
    service, local, _, _ = make_service(analyzer=analyzer, analysis_cache_size=64)
    messages: list[dict[str, str]] = []
    for number in range(8):
        text = "Please email Alex Chen" if number == 2 else f"question {number}"
        messages += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "ok"},
        ]
    messages.append({"role": "user", "content": "and now?"})

    response = asyncio.run(service.chat(ChatRequest(messages=messages)))

    assert len(analyzer.inputs) == 2
    # A name found by the combined call is still redacted.
    assert "Alex Chen" not in str(local.calls[0]["messages"])
    assert response.routing.contains_personal_data is True

    # Each older message was cached on its own: a follow-up costs one call.
    asyncio.run(
        service.chat(
            ChatRequest(
                messages=[
                    *messages,
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "one more"},
                ]
            )
        )
    )
    assert analyzer.inputs[2:] == ["and now?\n\none more"]


def test_personal_data_without_values_in_a_batch_keeps_requests_local() -> None:
    class AddressAnalyzer(FakeAnalyzer):
        # An address it can tell is there but cannot quote, only in the old message.
        async def analyze(self, user_input: str) -> RequestAnalysis:
            self.inputs.append(user_input)
            found = "corner" in user_input
            return analysis(complexity="complex").model_copy(
                update={
                    "contains_personal_data": found,
                    "personal_data_types": ["address"] if found else [],
                }
            )

    analyzer = AddressAnalyzer()
    service, _local, cloud, _ = make_service(analyzer=analyzer)
    request = ChatRequest(
        messages=[
            {"role": "user", "content": "I live on the corner near the station"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "design a system for me"},
        ]
    )

    response = asyncio.run(service.chat(request))

    assert response.provider == "local" and cloud.calls == []


def test_difficulty_comes_from_the_latest_message_only() -> None:
    service, _local, cloud, _ = make_service(analysis_mode="hybrid")
    request = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": "Compare microservice and monolith architecture",
            },
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "thanks, what time is it?"},
        ]
    )

    response = asyncio.run(service.chat(request))

    assert response.provider == "local"
    assert response.routing.complexity == "simple"
    assert cloud.calls == []


def test_earlier_messages_are_not_analyzed_again() -> None:
    service, _, _, analyzer = make_service(analysis_cache_size=16)
    first = [{"role": "user", "content": "first question"}]
    asyncio.run(service.chat(ChatRequest(messages=first)))
    later = [
        *first,
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "follow-up"},
    ]
    asyncio.run(service.chat(ChatRequest(messages=later)))

    assert analyzer.inputs == ["first question", "first question\n\nfollow-up"]


def test_personal_data_in_earlier_messages_is_still_redacted() -> None:
    service, local, _, _ = make_service()
    request = ChatRequest(
        messages=[
            {"role": "user", "content": "my email is alex@example.com"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "write a greeting"},
        ]
    )

    response = asyncio.run(service.chat(request))

    assert response.routing.redacted_values == 1
    assert "alex@example.com" not in str(local.calls[-1]["messages"])


def test_chat_redacts_text_parts_in_multimodal_messages() -> None:
    service, local, _, _ = make_service()
    request = ChatRequest(
        messages=[
            {"role": "user", "content": "Review this image"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Email alex@example.com"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/x.png"},
                    },
                ],
            },
        ]
    )

    asyncio.run(service.chat(request))

    assert local.calls[0]["messages"][2]["content"][0]["text"] == "Email [EMAIL_1]"


def test_chat_rejects_explicit_cloud_selection_without_cloud_provider() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, local, cloud, _ = make_service(analyzer=analyzer, cloud_api_key="")
    request = ChatRequest(
        provider_selection="cloud",
        messages=[{"role": "user", "content": "solve this"}],
    )

    with pytest.raises(RuntimeError, match="Cloud provider is unavailable"):
        asyncio.run(service.chat(request))

    assert local.calls == []
    assert cloud.calls == []


def test_chat_keeps_complex_request_local_without_cloud_model() -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, _, cloud, _ = make_service(analyzer=analyzer, cloud_model="")
    request = ChatRequest(messages=[{"role": "user", "content": "solve this"}])

    assert asyncio.run(service.chat(request)).provider == "local"
    assert cloud.calls == []

    explicit = ChatRequest(
        provider_selection="cloud", messages=[{"role": "user", "content": "x"}]
    )
    with pytest.raises(RuntimeError, match="Cloud provider is unavailable"):
        asyncio.run(service.chat(explicit))


@pytest.mark.parametrize(
    ("completion", "exception", "message"),
    [
        (SimpleNamespace(model="m", choices=[]), RuntimeError, "no choices"),
        (
            SimpleNamespace(
                model="m",
                choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            ),
            TypeError,
            "no text content",
        ),
    ],
)
def test_chat_rejects_invalid_provider_response(
    completion: Any, exception: type[Exception], message: str
) -> None:
    service, _, _, _ = make_service(local_provider=FakeProvider(completion=completion))
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(exception, match=message):
        asyncio.run(service.chat(request))


def test_chat_propagates_provider_failure() -> None:
    service, _, _, _ = make_service(
        local_provider=FakeProvider(error=RuntimeError("offline"))
    )
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(RuntimeError, match="offline"):
        asyncio.run(service.chat(request))


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "other", "messages": [{"role": "user", "content": "hi"}]},
        {"messages": []},
        {
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 2.1,
        },
    ],
)
def test_chat_request_rejects_invalid_payload(payload: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ChatRequest.model_validate(payload)


async def collect(service: GatewayService, request: ChatRequest) -> list[Any]:
    return [event async for event in service.events(request, stream=True)]


def test_events_stream_text_deltas_then_result() -> None:
    local = FakeProvider()
    local.chunks = [text_chunk("Hel"), text_chunk("lo"), usage_chunk(2)]
    service, _, cloud, _ = make_service(local_provider=local, output_pii_action="off")
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    events = asyncio.run(collect(service, request))

    assert [event.type for event in events] == [
        "analysis_started",
        "analysis_completed",
        "routed",
        "generation_started",
        "delta",
        "delta",
        "result",
    ]
    assert [event.data["content"] for event in events if event.type == "delta"] == [
        "Hel",
        "lo",
    ]
    result = events[-1].response
    assert result.content == "Hello"
    assert result.model == "streamed-model"
    assert result.usage.generation.completion_tokens == 2
    assert events[-1].to_json()["data"]["content"] == "Hello"
    assert cloud.calls == []


def test_events_stream_wraps_provider_failure() -> None:
    service, _, _, _ = make_service(
        local_provider=FakeProvider(error=RuntimeError("offline"))
    )
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    with pytest.raises(RuntimeError, match="Model provider request failed: offline"):
        asyncio.run(collect(service, request))


def reply(content: str) -> Any:
    return SimpleNamespace(
        model="m", choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_chat_restores_placeholders_in_the_reply() -> None:
    local = FakeProvider(completion=reply("I will email [EMAIL_1] today."))
    service, _, _, _ = make_service(local_provider=local)
    request = ChatRequest(
        messages=[{"role": "user", "content": "Write to alex@example.com"}]
    )

    response = asyncio.run(service.chat(request))

    assert response.content == "I will email alex@example.com today."
    assert "alex@example.com" not in str(local.calls[0]["messages"])


def test_requests_to_the_local_model_are_redacted_too() -> None:
    service, local, _, _ = make_service()
    request = ChatRequest(messages=[{"role": "user", "content": "Call 0912-345-678"}])

    response = asyncio.run(service.chat(request))

    assert "0912-345-678" not in str(local.calls[0]["messages"])
    assert response.routing.redacted_values == 1


def test_same_value_gets_the_same_placeholder_across_turns() -> None:
    service, local, _, _ = make_service()
    request = ChatRequest(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Email alex@example.com"},
            {"role": "assistant", "content": "Sent to alex@example.com."},
            {"role": "user", "content": "Also cc sam@example.org and alex@example.com"},
        ]
    )

    asyncio.run(service.chat(request))

    sent = local.calls[0]["messages"]
    assert sent[0]["content"] == f"Be brief.\n\n{PLACEHOLDER_INSTRUCTION}"
    assert sent[1]["content"] == "Email [EMAIL_1]"
    assert sent[2]["content"] == "Sent to [EMAIL_1]."
    assert sent[3]["content"] == "Also cc [EMAIL_2] and [EMAIL_1]"


def test_stream_restores_placeholders_split_across_chunks() -> None:
    local = FakeProvider()
    local.chunks = [text_chunk("Mail [EM"), text_chunk("AIL_"), text_chunk("1] now")]
    service, _, _, _ = make_service(local_provider=local, output_pii_action="off")
    request = ChatRequest(messages=[{"role": "user", "content": "alex@example.com"}])

    events = asyncio.run(collect(service, request))

    deltas = [event.data["content"] for event in events if event.type == "delta"]
    assert "".join(deltas) == "Mail alex@example.com now"
    assert all("[" not in delta for delta in deltas)
    assert events[-1].response.content == "Mail alex@example.com now"


def test_hybrid_analysis_skips_the_model_when_the_request_stays_local() -> None:
    service, local, _, analyzer = make_service(analysis_mode="hybrid")
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    response = asyncio.run(service.chat(request))

    assert analyzer.inputs == []
    assert response.routing.analysis_source == "rules"
    assert response.provider == "local"
    assert local.calls


@pytest.mark.parametrize(
    ("content", "selection"),
    [
        ("Please design a system for billing", "auto"),
        ("hi", "cloud"),
    ],
)
def test_hybrid_analysis_uses_the_model_whenever_cloud_is_possible(
    content: str, selection: str
) -> None:
    analyzer = FakeAnalyzer(analysis(complexity="complex"))
    service, _, cloud, _ = make_service(analyzer=analyzer, analysis_mode="hybrid")
    request = ChatRequest(
        provider_selection=selection, messages=[{"role": "user", "content": content}]
    )

    response = asyncio.run(service.chat(request))

    assert analyzer.inputs == [content]
    assert response.routing.analysis_source == "llm"
    assert cloud.calls


def test_hybrid_analysis_skips_the_model_when_local_is_forced() -> None:
    service, _, _, analyzer = make_service(analysis_mode="hybrid")
    request = ChatRequest(
        provider_selection="local",
        messages=[{"role": "user", "content": "Please design a system"}],
    )

    response = asyncio.run(service.chat(request))

    assert analyzer.inputs == []
    assert response.routing.complexity == "complex"


def test_rules_mode_never_calls_the_analysis_model() -> None:
    service, _, cloud, analyzer = make_service(analysis_mode="rules")
    request = ChatRequest(
        messages=[{"role": "user", "content": "Compare these designs, mail a@b.com"}]
    )

    response = asyncio.run(service.chat(request))

    assert analyzer.inputs == []
    assert response.routing.personal_data_types == ["email"]
    assert response.provider == "cloud"
    assert cloud.calls[0]["messages"][-1]["content"].endswith("[EMAIL_1]")


def test_analysis_results_are_cached_for_identical_input() -> None:
    service, _, _, analyzer = make_service(analysis_cache_size=8)
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    first = asyncio.run(service.chat(request))
    second = asyncio.run(service.chat(request))

    assert analyzer.inputs == ["hi"]
    assert first.routing.analysis_source == "llm"
    assert second.routing.analysis_source == "cache"


def test_analysis_events_report_the_source() -> None:
    service, _, _, _ = make_service(analysis_mode="hybrid")
    request = ChatRequest(messages=[{"role": "user", "content": "hi"}])

    events = asyncio.run(collect(service, request))

    completed = next(event for event in events if event.type == "analysis_completed")
    assert completed.data["source"] == "rules"


def test_personal_data_inside_chinese_text_is_redacted_and_restored() -> None:
    analyzer = FakeAnalyzer(
        RequestAnalysis(
            contains_personal_data=True,
            personal_data_types=["name", "taiwan_mobile"],
            personal_data_values=["王小明"],
            complexity="complex",
            task_type="general",
            reason="test",
        )
    )
    cloud = FakeProvider(
        completion=reply("已通知[PERSONAL_DATA_1]（[TAIWAN_MOBILE_1]）")
    )
    service, _, _, _ = make_service(analyzer=analyzer, cloud_provider=cloud)
    request = ChatRequest(
        messages=[{"role": "user", "content": "請通知王小明，電話0912345678"}]
    )

    response = asyncio.run(service.chat(request))

    assert cloud.calls[0]["messages"][-1]["content"] == (
        "請通知[PERSONAL_DATA_1]，電話[TAIWAN_MOBILE_1]"
    )
    assert response.content == "已通知王小明（0912345678）"


def test_memory_is_summarized_by_the_local_model_only() -> None:
    service, local, cloud, _ = make_service()
    memory = asyncio.run(
        service.summarize_memory(
            "- user is Alex",
            [
                {"role": "user", "content": "I prefer Python"},
                {"role": "assistant", "content": "ok"},
            ],
        )
    )

    assert memory == "hello"  # FakeProvider's reply
    assert cloud.calls == []
    sent = local.calls[-1]["messages"]
    assert "long-term memory" in sent[0]["content"]
    assert (
        "- user is Alex" in sent[1]["content"]
        and "USER: I prefer Python" in sent[1]["content"]
    )


def test_output_keeps_names_and_places_the_model_mentions() -> None:
    """Named-entity recognition checks what is sent, not the answer: answers
    name people and places all the time, and NER mistakes there (a verb read
    as a name) used to blank out words."""
    import re

    from ugen_ai_gateway.analysis.pii import (
        PersonalDataMatch,
        clear_registered_finders,
        register_finder,
    )

    scans: list[str] = []

    def names(text: str) -> list[PersonalDataMatch]:
        scans.append(text)
        return [
            PersonalDataMatch(
                "person_name", "PERSON_NAME", m.start(), m.end(), m.group()
            )
            for m in re.finditer("Bob Lee", text)
        ]

    register_finder(names, types=["person_name"])
    try:
        words = "Ask Bob Lee about it, or call 0912-345-678 today. "
        local = FakeProvider()
        local.chunks = [text_chunk(word + " ") for word in words.split(" ")]
        service, _, _, _ = make_service(
            local_provider=local, output_pii_action="redact"
        )
        request = ChatRequest(messages=[{"role": "user", "content": "plan my day"}])
        events = asyncio.run(collect(service, request))
    finally:
        clear_registered_finders()

    text = events[-1].response.content
    assert "Bob Lee" in text and "[PERSON_NAME]" not in text
    # Structured data the user never gave is still redacted.
    assert "0912-345-678" not in text and "[TAIWAN_MOBILE]" in text
    assert not any("Bob Lee" in scanned for scanned in scans)


def test_untraceable_findings_are_not_cached_for_shared_messages() -> None:
    """A batch that reports personal data without values marks every message
    in it; that must not stick to a message other requests share, such as
    the gateway's system prompt."""

    class NameSpotter(FakeAnalyzer):
        async def analyze(self, user_input: str) -> RequestAnalysis:
            self.inputs.append(user_input)
            found = "Lin" in user_input
            return RequestAnalysis(
                contains_personal_data=found,
                personal_data_types=["name"] if found else [],
                complexity="complex",
                task_type="general",
                reason="Test analysis.",
            )

    service, _, _, _ = make_service(
        analyzer=NameSpotter(), analysis_mode="llm", analysis_cache_size=16
    )
    shared = {"role": "system", "content": "House rules: be brief."}

    def ask(*messages: dict) -> Any:
        request = ChatRequest(
            messages=[*messages, {"role": "user", "content": "design a system"}]
        )
        return asyncio.run(service.chat(request))

    ask(shared, {"role": "system", "content": "Call me Lin."})
    other = ask(shared)

    assert other.provider == "cloud"
    assert other.routing.personal_data_types == []


def test_system_messages_given_as_text_parts_are_merged_too() -> None:
    service, local, _, _ = make_service()
    request = ChatRequest(
        messages=[
            {"role": "system", "content": "House rules."},
            {"role": "system", "content": [{"type": "text", "text": "You are a bot."}]},
            {"role": "user", "content": "hi"},
        ]
    )

    asyncio.run(service.chat(request))

    sent = local.calls[0]["messages"]
    assert sent[0] == {"role": "system", "content": "House rules.\n\nYou are a bot."}
    assert [message["role"] for message in sent] == ["system", "user"]
