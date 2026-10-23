import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_gateway import FakeAnalyzer, analysis, make_service
from ugen_ai_gateway.api import access
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.dependencies import get_gateway_service, get_usage_store
from ugen_ai_gateway.main import create_app
from ugen_ai_gateway.services.usage import UsageRecord, UsageStore

CHAT = {"messages": [{"role": "user", "content": "please design a system"}]}


class Env:
    def __init__(self, monkeypatch: Any, **settings: Any) -> None:
        self.settings = Settings(**settings)
        monkeypatch.setattr(
            "ugen_ai_gateway.api.access.get_settings", lambda: self.settings
        )
        access.rate_limiter.clear()
        self.store = UsageStore(":memory:")
        self.service, self.local, self.cloud, _ = make_service(
            analyzer=FakeAnalyzer(analysis(complexity="complex"))
        )
        self.service._usage_store = self.store
        app = create_app()
        app.dependency_overrides[get_usage_store] = lambda: self.store
        app.dependency_overrides[get_gateway_service] = lambda: self.service
        self.client = TestClient(app)

    def create_key(self, admin: str | None = None, **body: Any) -> dict[str, Any]:
        headers = {"X-API-Key": admin} if admin else {}
        response = self.client.post(
            "/v1/ugen/keys", json={"name": "team", **body}, headers=headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    def chat(self, key: str | None = None, **body: Any) -> Any:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return self.client.post("/v1/ugen/chat", json={**CHAT, **body}, headers=headers)


def test_first_key_locks_the_open_gateway(monkeypatch) -> None:
    env = Env(monkeypatch)
    assert env.chat().status_code == 200  # open while no keys exist

    created = env.create_key()

    assert created["key"].startswith("ugk_")
    assert env.chat().status_code == 401
    assert env.chat(created["key"]).status_code == 200
    # Without an admin key, tenants cannot manage keys once one exists.
    assert env.client.get("/v1/ugen/keys").status_code == 403
    assert (
        env.client.get(
            "/v1/ugen/keys", headers={"X-API-Key": created["key"]}
        ).status_code
        == 403
    )


def test_admin_key_manages_keys_and_revocation_takes_effect(monkeypatch) -> None:
    env = Env(monkeypatch, admin_api_key="admin")
    assert env.client.post("/v1/ugen/keys", json={"name": "x"}).status_code == 401

    created = env.create_key("admin")
    listed = env.client.get("/v1/ugen/keys", headers={"X-API-Key": "admin"}).json()[
        "data"
    ]
    revoked = env.client.delete(
        f"/v1/ugen/keys/{created['id']}", headers={"X-API-Key": "admin"}
    )

    assert [key["id"] for key in listed] == [created["id"]]
    assert "key" not in listed[0] and "key_hash" not in listed[0]
    assert listed[0]["prefix"] == created["key"][:10]
    assert revoked.status_code == 204
    assert env.chat(created["key"]).status_code == 401
    assert (
        env.client.delete(
            f"/v1/ugen/keys/{created['id']}", headers={"X-API-Key": "admin"}
        ).status_code
        == 404
    )


def test_master_key_still_works_alongside_api_keys(monkeypatch) -> None:
    env = Env(monkeypatch, gateway_api_key="master")
    env.create_key("master")

    assert env.chat("master").status_code == 200
    assert env.chat("wrong").status_code == 401


def test_secrets_are_stored_only_as_hashes(monkeypatch) -> None:
    env = Env(monkeypatch)
    created = env.create_key()

    dump = "\n".join(env.store._connection.iterdump())
    assert created["key"] not in dump


def test_each_key_has_its_own_rate_limit(monkeypatch) -> None:
    env = Env(monkeypatch)
    limited = env.create_key(rate_limit_per_minute=1)
    env.settings.admin_api_key = "admin"
    other = env.create_key("admin")

    assert env.chat(limited["key"]).status_code == 200
    assert env.chat(limited["key"]).status_code == 429
    assert env.chat(other["key"]).status_code == 200


def test_local_only_keys_never_reach_the_cloud(monkeypatch) -> None:
    env = Env(monkeypatch)
    key = env.create_key(allow_cloud=False)["key"]

    auto = env.chat(key).json()
    forced = env.chat(key, provider_selection="cloud").json()

    for body in (auto, forced):
        assert body["provider"] == "local"
        assert body["routing"]["rule"] == "key.local_only"
    assert env.cloud.calls == []


def test_spent_budget_keeps_the_key_local(monkeypatch) -> None:
    env = Env(monkeypatch)
    created = env.create_key(monthly_budget_usd=1.0)
    key = created["key"]
    assert env.chat(key).json()["provider"] == "cloud"

    asyncio.run(
        env.store.record(
            UsageRecord(id="spent", api_key_id=created["id"], cost_usd=1.5)
        )
    )
    body = env.chat(key).json()

    assert body["provider"] == "local"
    assert body["routing"]["rule"] == "key.budget"
    whoami = env.client.get("/v1/ugen/whoami", headers={"X-API-Key": key}).json()
    assert whoami["month_cost_usd"] == pytest.approx(1.5)
    assert whoami["monthly_budget_usd"] == 1.0


def test_tenants_only_see_and_rate_their_own_requests(monkeypatch) -> None:
    env = Env(monkeypatch, admin_api_key="admin")
    first = env.create_key("admin")["key"]
    second = env.create_key("admin")["key"]
    own = env.chat(first).json()["id"]
    others = env.chat(second).json()["id"]

    def get(path: str, key: str) -> Any:
        return env.client.get(path, headers={"X-API-Key": key}).json()

    assert [row["id"] for row in get("/v1/ugen/requests", first)["data"]] == [own]
    assert get("/v1/ugen/stats", first)["completed"] == 1
    feedback = env.client.post(
        "/v1/ugen/feedback",
        json={"request_id": others, "rating": 1},
        headers={"X-API-Key": first},
    )
    assert feedback.status_code == 404


def test_keys_list_reports_monthly_usage(monkeypatch) -> None:
    env = Env(monkeypatch, admin_api_key="admin")
    created = env.create_key("admin")
    env.chat(created["key"])
    env.chat(created["key"])

    listed = env.client.get("/v1/ugen/keys", headers={"X-API-Key": "admin"}).json()

    assert listed["data"][0]["month_requests"] == 2


def test_admin_key_can_chat_and_sees_all_usage(monkeypatch) -> None:
    env = Env(monkeypatch, admin_api_key="admin")
    tenant = env.create_key("admin")["key"]
    env.chat(tenant)

    assert env.chat().status_code == 401
    assert env.chat("admin").status_code == 200
    stats = env.client.get("/v1/ugen/stats", headers={"X-API-Key": "admin"}).json()
    assert stats["completed"] == 2


def test_invalid_keys_are_throttled_per_address(monkeypatch) -> None:
    env = Env(monkeypatch)
    env.create_key()
    statuses = [env.chat(f"ugk_guess_{n}").status_code for n in range(22)]
    assert statuses[:20] == [401] * 20
    assert statuses[20:] == [429, 429]


def test_rate_limiter_drops_idle_buckets(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(access.time, "monotonic", lambda: clock[0])
    limiter = access.InMemoryRateLimiter()
    for n in range(50):
        limiter.hit(f"ip:{n}", limit=5, window_seconds=60)
    assert len(limiter) == 50
    clock[0] += limiter.SWEEP_SECONDS + 61
    limiter.hit("ip:new", limit=5, window_seconds=60)
    assert len(limiter) == 1
