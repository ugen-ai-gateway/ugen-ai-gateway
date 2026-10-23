import asyncio
import sqlite3
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_gateway import FakeAnalyzer, FakeProvider, analysis, make_service
from tests.test_routing import config
from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.api import access
from ugen_ai_gateway.api.schemas import ChatRequest
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service, get_usage_store
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.gateway import GatewayService
from ugen_ai_gateway.services.usage import UsageRecord, UsageStore, token_cost


def store() -> UsageStore:
    return UsageStore(":memory:")


def test_token_cost() -> None:
    assert token_cost(1_000_000, 500_000, 0.3, 2.5) == pytest.approx(1.55)


def test_stats_totals_savings_daily_and_feedback() -> None:
    usage = store()
    now = time.time()
    records = [
        UsageRecord(
            id="a",
            provider="local",
            rule=None,
            completion_tokens=100,
            generation_ms=1000,
            baseline_cost_usd=0.5,
            personal_data_types=["email"],
            redacted_values=2,
        ),
        UsageRecord(
            id="b",
            provider="cloud",
            rule="long",
            completion_tokens=300,
            generation_ms=1000,
            cost_usd=0.2,
            baseline_cost_usd=0.2,
            fallback_from=None,
            analysis_source="rules",
        ),
        UsageRecord(id="c", status="error", error="ProviderOutage"),
        UsageRecord(id="old", provider="cloud", created_at=now - 30 * 86400),
    ]
    for record in records:
        asyncio.run(usage.record(record))
    assert asyncio.run(usage.set_feedback("a", 1)) is True
    assert asyncio.run(usage.set_feedback("b", -1)) is True
    assert asyncio.run(usage.set_feedback("b", 1)) is True  # changed vote
    assert asyncio.run(usage.set_feedback("missing", 1)) is False

    stats = asyncio.run(usage.stats(days=7))

    assert stats["requests"] == 3
    assert (stats["completed"], stats["failed"]) == (2, 1)
    assert (stats["local"], stats["cloud"]) == (1, 1)
    assert stats["tokens_per_second"] == 200.0
    assert stats["cost_usd"] == pytest.approx(0.2)
    assert stats["savings_usd"] == pytest.approx(0.5)
    assert (stats["with_personal_data"], stats["redacted_values"]) == (1, 2)
    assert stats["analysis_rules"] == 1
    assert stats["feedback"] == {"up": 2, "down": 0}
    assert sum(day["requests"] for day in stats["daily"]) == 3
    assert {row["rule"]: row["requests"] for row in stats["rules"]} == {
        "default": 1,
        "long": 1,
    }


def test_daily_buckets_follow_the_client_time_zone() -> None:
    usage = store()
    # 2024-01-01 23:30 UTC is already 2024-01-02 in UTC+8.
    timestamp = 1704151800.0
    asyncio.run(usage.record(UsageRecord(id="x", created_at=timestamp)))

    def day(offset: int) -> str:
        rows = usage._connection.execute(
            "SELECT date(created_at + ?, 'unixepoch') FROM requests", (offset * 60,)
        ).fetchone()
        return rows[0]

    assert day(0) == "2024-01-01"
    assert day(480) == "2024-01-02"


def test_recent_pages_newest_first() -> None:
    usage = store()
    for index in range(5):
        asyncio.run(usage.record(UsageRecord(id=str(index), created_at=100.0 + index)))

    first = asyncio.run(usage.recent(limit=2))
    second = asyncio.run(usage.recent(limit=2, before=first[-1]["created_at"]))

    assert [item["id"] for item in first] == ["4", "3"]
    assert [item["id"] for item in second] == ["2", "1"]


def test_gateway_records_cost_baseline_and_never_message_text() -> None:
    usage = store()
    completion_usage = type(
        "U",
        (),
        {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000},
    )()
    cloud = FakeProvider()
    cloud.completion.usage = completion_usage
    providers = {"ollama": FakeProvider(), "gemini": cloud}
    service = GatewayService(
        settings=Settings(analysis_mode="llm"),
        local_provider=providers["ollama"],  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=FakeAnalyzer(
            analysis(contains_personal_data=True, complexity="complex")
        ),  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        routing=config(rules=[]),
        providers=providers,  # type: ignore[arg-type]
        usage_store=usage,
    )
    secret = "請寄給 alex@example.com 這份機密"

    response = asyncio.run(
        service.chat(
            ChatRequest(
                provider_selection="cloud",
                messages=[{"role": "user", "content": secret}],
            )
        )
    )

    row = asyncio.run(usage.recent(limit=1))[0]
    assert row["id"] == response.id
    assert (row["provider"], row["provider_name"], row["status"]) == (
        "cloud",
        "gemini",
        "ok",
    )
    assert row["cost_usd"] == pytest.approx(2000 * 2.5 / 1_000_000)
    assert row["baseline_cost_usd"] == row["cost_usd"]
    assert row["personal_data_types"] == ["email"]
    dump = "\n".join(usage._connection.iterdump())
    assert "alex@example.com" not in dump and "機密" not in dump


def test_failed_requests_are_recorded_with_the_error_type() -> None:
    usage = store()
    service, _, _, _ = make_service(
        local_provider=FakeProvider(error=RuntimeError("x"))
    )
    service._usage_store = usage

    with pytest.raises(RuntimeError):
        asyncio.run(
            service.chat(ChatRequest(messages=[{"role": "user", "content": "hi"}]))
        )

    row = asyncio.run(usage.recent(limit=1))[0]
    assert (row["status"], row["error"]) == ("error", "ProviderRequestError")


def test_local_answers_save_against_the_cloud_baseline() -> None:
    usage = store()
    local = FakeProvider()
    local.completion.usage = type(
        "U", (), {"prompt_tokens": 0, "completion_tokens": 1_000_000, "total_tokens": 0}
    )()
    service = GatewayService(
        settings=Settings(analysis_mode="llm"),
        local_provider=local,  # type: ignore[arg-type]
        cloud_provider=None,
        analyzer=FakeAnalyzer(),  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        routing=config(rules=[]),
        providers={"ollama": local, "gemini": FakeProvider()},  # type: ignore[dict-item]
        usage_store=usage,
    )

    asyncio.run(service.chat(ChatRequest(messages=[{"role": "user", "content": "hi"}])))

    stats = asyncio.run(usage.stats(days=1))
    assert stats["cost_usd"] == 0
    assert stats["savings_usd"] == pytest.approx(2.5)


@pytest.fixture
def api(monkeypatch) -> Any:
    monkeypatch.setattr(
        "ugen_ai_gateway.api.access.get_settings", lambda: Settings(gateway_api_key="")
    )
    access.rate_limiter.clear()
    usage = store()
    service, _, _, _ = make_service()
    service._usage_store = usage
    app = create_app()
    app.dependency_overrides[get_gateway_service] = lambda: service
    app.dependency_overrides[get_usage_store] = lambda: usage
    return TestClient(app), app


def test_stats_requests_and_feedback_endpoints(api: Any) -> None:
    client, _ = api
    chat = client.post(
        "/v1/ugen/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    ).json()

    stats = client.get("/v1/ugen/stats", params={"days": 7}).json()
    recent = client.get("/v1/ugen/requests", params={"limit": 5}).json()["data"]
    vote = client.post(
        "/v1/ugen/feedback", json={"request_id": chat["id"], "rating": 1}
    )
    unknown = client.post(
        "/v1/ugen/feedback", json={"request_id": "nope", "rating": -1}
    )
    invalid = client.post(
        "/v1/ugen/feedback", json={"request_id": chat["id"], "rating": 5}
    )

    assert stats["completed"] == 1
    assert recent[0]["id"] == chat["id"]
    assert vote.status_code == 204
    assert unknown.status_code == 404
    assert invalid.status_code == 422
    assert client.get("/v1/ugen/requests").json()["data"][0]["rating"] == 1


def test_usage_endpoints_report_when_tracking_is_disabled(api: Any) -> None:
    client, app = api
    app.dependency_overrides[get_usage_store] = lambda: None

    assert client.get("/v1/ugen/stats").status_code == 404


def test_openai_ids_match_the_recorded_request(api: Any) -> None:
    client, _ = api

    body = client.post(
        "/v1/chat/completions",
        json={"model": "ugen-auto", "messages": [{"role": "user", "content": "hi"}]},
    ).json()
    recorded = client.get("/v1/ugen/requests").json()["data"][0]["id"]

    assert body["id"] == f"chatcmpl-ugen-{recorded}"


def test_store_file_is_created(tmp_path) -> None:
    path = tmp_path / "nested" / "ugen.db"
    UsageStore(path).close()

    tables = (
        sqlite3.connect(path)
        .execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        .fetchall()
    )
    assert {"requests", "feedback", "audit"} <= {name for (name,) in tables}


def test_requests_closed_mid_stream_are_recorded_as_cancelled() -> None:
    from tests.test_gateway import text_chunk

    async def scenario() -> list[dict[str, Any]]:
        usage = store()
        local = FakeProvider()
        local.chunks = [text_chunk("x " * 40), text_chunk("y " * 40)]
        service, _, _, _ = make_service(local_provider=local, output_pii_action="off")
        service._usage_store = usage
        events = service.events(
            ChatRequest(messages=[{"role": "user", "content": "hi"}]), stream=True
        )
        async for event in events:
            if event.type == "delta":
                break
        await events.aclose()
        await asyncio.sleep(0.05)
        return await usage.recent(limit=5)

    rows = asyncio.run(scenario())

    assert [(row["status"], row["provider"]) for row in rows] == [
        ("cancelled", "local")
    ]


def test_completed_requests_stay_completed_after_the_stream_is_closed() -> None:
    usage = store()
    service, _, _, _ = make_service()
    service._usage_store = usage

    asyncio.run(service.chat(ChatRequest(messages=[{"role": "user", "content": "hi"}])))

    assert asyncio.run(usage.recent(limit=5))[0]["status"] == "ok"


def test_audit_chain_survives_several_processes(tmp_path) -> None:
    import multiprocessing

    path = str(tmp_path / "ugen.db")
    UsageStore(path).close()
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_audit_entries, args=(path,)) for _ in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()

    assert [process.exitcode for process in processes] == [0, 0, 0]
    result = asyncio.run(UsageStore(path).verify_audit())
    assert result == {"ok": True, "entries": 90, "first_invalid_seq": None}


def _append_audit_entries(path: str) -> None:
    store = UsageStore(path)
    for _ in range(30):
        asyncio.run(store.append_audit("request.ok", request_id=None, detail={}))


def test_audit_export_without_since_returns_the_newest_entries() -> None:
    store = UsageStore(":memory:")
    for number in range(5):
        asyncio.run(store.append_audit(f"event_{number}", request_id=None, detail={}))

    newest = asyncio.run(store.audit_entries(since=None, limit=2))
    assert [entry["event"] for entry in newest] == ["event_3", "event_4"]
    first = asyncio.run(store.audit_entries(since=0, limit=2))
    assert [entry["event"] for entry in first] == ["event_0", "event_1"]
