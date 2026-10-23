from types import SimpleNamespace
from typing import Any

from tests.test_accounts import AccountEnv, bearer
from ugen_ai_gateway import dependencies
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.services.model_config import (
    StoredModels,
    from_settings,
    to_routing,
)

LOCAL = {"base_url": "http://localhost:11434/v1", "model": "qwen3:8b"}
CLOUD = {
    "base_url": "https://api.example.com/v1",
    "api_key": "sk-test-1234567890",
    "model": "big",
}


def body(**overrides: Any) -> dict[str, Any]:
    return {"local": LOCAL, "cloud_enabled": False, "cloud": {}, **overrides}


class ModelsEnv(AccountEnv):
    def __init__(self, monkeypatch: Any, **settings: Any) -> None:
        super().__init__(monkeypatch, **settings)
        for module in ("model_routes", "dependencies"):
            target = (
                f"ugen_ai_gateway.api.{module}.get_settings"
                if module == "model_routes"
                else "ugen_ai_gateway.dependencies.get_settings"
            )
            monkeypatch.setattr(target, lambda: self.settings)
        monkeypatch.setattr(dependencies, "get_usage_store", lambda: self.store)
        self.client.app.dependency_overrides.pop(dependencies.get_gateway_service)  # type: ignore[attr-defined]
        dependencies.models_changed()
        self.admin = self.token("alice")

    def put(self, payload: dict[str, Any], token: str | None = None) -> Any:
        return self.client.put(
            "/v1/ugen/models", json=payload, headers=bearer(token or self.admin)
        )

    def get(self, token: str | None = None) -> Any:
        return self.client.get("/v1/ugen/models", headers=bearer(token or self.admin))


def test_only_administrators_see_or_change_models(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    member = env.token("bob")
    assert env.get(member).status_code == 403
    assert env.put(body(), member).status_code == 403
    assert env.client.get("/v1/ugen/models").status_code == 403


def test_saved_models_drive_the_gateway_without_a_restart(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    assert env.get().json()["source"] == "env"

    saved = env.put(
        body(
            cloud_enabled=True,
            cloud={**CLOUD, "alias": "Smart"},
            local={**LOCAL, "alias": "House"},
        )
    )
    assert saved.status_code == 200, saved.text

    config = env.client.get("/v1/ugen/config", headers=bearer(env.admin)).json()
    assert config["local_model"] == "House"
    assert config["cloud_model"] == "Smart"
    assert config["cloud_enabled"] is True
    route = dependencies.get_gateway_service().engine.default("cloud")
    assert route.provider.base_url == "https://api.example.com/v1"
    assert route.model.id == "big"


def test_api_keys_are_never_returned_and_kept_when_omitted(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    env.put(body(cloud_enabled=True, cloud={**CLOUD, "api_key": "AIza-secret-abcd"}))

    view = env.get().json()
    assert "AIza-secret-abcd" not in str(view)
    assert view["config"]["cloud"]["has_api_key"] is True
    assert view["config"]["cloud"]["api_key_hint"] == "…abcd"

    # Stored encrypted, never as plain text.
    raw = env.store.load_setting("models")["cloud"]["api_key"]
    assert raw.startswith("enc:v1:") and "AIza" not in raw

    def saved() -> Any:
        return dependencies.current_models(env.store)[0]

    # Saving again without a key keeps it; "" removes it.
    env.put(
        body(
            cloud_enabled=True, cloud={"base_url": CLOUD["base_url"], "model": "other"}
        )
    )
    assert saved().cloud.api_key == "AIza-secret-abcd"
    assert saved().cloud.model == "other"
    env.put(body(cloud={"api_key": ""}))
    assert saved().cloud.api_key == ""


def test_turning_the_cloud_off_keeps_its_settings(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    env.put(body(cloud_enabled=True, cloud=CLOUD))
    env.put(body(cloud_enabled=False))

    assert dependencies.get_gateway_service().engine.default("cloud") is None
    assert dependencies.current_models(env.store)[0].cloud.model == "big"


def test_cloud_cannot_be_turned_on_without_url_key_and_model(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    for missing in ("base_url", "api_key", "model"):
        cloud = {k: v for k, v in CLOUD.items() if k != missing}
        response = env.put(body(cloud_enabled=True, cloud=cloud))
        assert response.status_code == 422, missing
    assert (
        env.put(body(local={**LOCAL, "base_url": "localhost:11434"})).status_code == 422
    )


def test_configuration_saved_with_presets_keeps_the_active_provider() -> None:
    old = {
        "local": {"preset": "ollama", **LOCAL},
        "cloud_enabled": True,
        "cloud_active": "anthropic",
        "cloud": {
            "openai": {"model": "gpt"},
            "anthropic": {**CLOUD, "model": "claude"},
        },
    }
    stored = StoredModels.model_validate(old)
    assert stored.cloud.model == "claude"
    assert stored.active_cloud() is not None


def test_routing_file_takes_precedence(monkeypatch, tmp_path) -> None:
    env = ModelsEnv(monkeypatch, routing_config=str(tmp_path / "routing.yaml"))
    assert env.get().json()["source"] == "routing_file"
    assert env.put(body()).status_code == 409


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.models = SimpleNamespace(list=self.list)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("401 invalid api key")
        return SimpleNamespace(choices=[])

    async def _items(self) -> Any:
        for model_id in ("models/gemini-b", "models/gemini-a"):
            yield SimpleNamespace(id=model_id)

    def list(self) -> Any:
        return self._items()

    async def close(self) -> None:
        pass


def test_test_and_list_use_the_saved_key_unless_one_is_given(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    env.put(body(cloud={**CLOUD, "api_key": "saved-key-9999"}))
    used: list[str] = []
    fake = FakeClient()

    def factory(**kwargs: Any) -> FakeClient:
        used.append(kwargs["api_key"])
        return fake

    monkeypatch.setattr(
        "ugen_ai_gateway.api.model_routes.create_openai_client", factory
    )
    probe = {
        "target": "cloud",
        "base_url": CLOUD["base_url"] + "/",
        "model": "gemini-a",
    }
    tested = env.client.post(
        "/v1/ugen/models/test", json=probe, headers=bearer(env.admin)
    )
    assert tested.json()["ok"] is True
    listed = env.client.post(
        "/v1/ugen/models/list",
        json={**probe, "api_key": "typed-key"},
        headers=bearer(env.admin),
    )
    assert listed.json()["models"] == ["gemini-a", "gemini-b"]
    assert used == ["saved-key-9999", "typed-key"]

    # The saved key never goes to a different address.
    elsewhere = env.client.post(
        "/v1/ugen/models/test",
        json={**probe, "base_url": "https://evil.example/v1"},
        headers=bearer(env.admin),
    )
    assert elsewhere.status_code == 422
    assert used == ["saved-key-9999", "typed-key"]

    monkeypatch.setattr(
        "ugen_ai_gateway.api.model_routes.create_openai_client",
        lambda **_: FakeClient(fail=True),
    )
    failed = env.client.post(
        "/v1/ugen/models/test", json=probe, headers=bearer(env.admin)
    )
    assert failed.json() == {"ok": False, "error": "401 invalid api key"}


def test_env_settings_seed_the_first_configuration() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        local_base_url="http://localhost:11434/v1",
        local_model="qwen",
        cloud_api_key="AIza-key",
        cloud_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        cloud_model="gemini-flash",
    )
    seeded = from_settings(settings)
    assert seeded.local.model == "qwen"
    assert seeded.cloud_enabled and seeded.cloud.model == "gemini-flash"
    routing = to_routing(seeded)
    assert [p.locality for p in routing.providers] == ["local", "cloud"]


def test_no_cloud_when_disabled() -> None:
    stored = StoredModels.model_validate({"local": LOCAL, "cloud": CLOUD})
    assert [p.name for p in to_routing(stored).providers] == ["local"]


def test_unconfigured_gateway_points_to_the_models_page(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    response = env.chat(env.admin)
    assert response.status_code == 503
    assert "Models page" in response.json()["detail"]


def test_a_separate_analysis_model_analyzes_requests() -> None:
    stored = StoredModels.model_validate(
        {"local": {**LOCAL, "analysis_model": "qwen3:1.7b"}}
    )
    routing = to_routing(stored)
    assert routing.analysis.model == "qwen3:1.7b"
    assert routing.resolve(routing.defaults.local)[1].id == "qwen3:8b"


CODER = {
    "id": "coder",
    "name": "Code model",
    "locality": "cloud",
    "base_url": "https://code.example.com/v1",
    "api_key": "sk-coder-key-0001",
    "model": "code-xl",
}


def test_rules_send_matching_requests_to_extra_models_first() -> None:
    stored = StoredModels.model_validate(
        {
            "local": LOCAL,
            "cloud_enabled": True,
            "cloud": CLOUD,
            "extra": [CODER],
            "rules": [
                {"name": "code", "when": {"task_type": ["coding"]}, "target": "coder"},
                {"name": "short", "when": {"max_chars": 40}, "target": "local"},
            ],
        }
    )
    routing = to_routing(stored)
    assert [rule.name for rule in routing.rules] == [
        "code",
        "short",
        "moderate-or-complex-to-cloud",
    ]
    provider = routing.provider("extra-coder")
    assert provider is not None and provider.locality == "cloud"
    assert provider.models[0].id == "code-xl"


def test_rules_to_a_disabled_cloud_are_skipped() -> None:
    stored = StoredModels.model_validate(
        {
            "local": LOCAL,
            "rules": [
                {"name": "big", "when": {"complexity": ["complex"]}, "target": "cloud"}
            ],
        }
    )
    assert to_routing(stored).rules == []


def test_rules_must_point_at_a_known_model() -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown model"):
        StoredModels.model_validate(
            {"local": LOCAL, "rules": [{"name": "x", "target": "nope"}]}
        )


def test_extra_models_save_keep_their_keys_and_route(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    rules = [{"name": "code", "when": {"complexity": ["complex"]}, "target": "coder"}]
    saved = env.put(body(extra=[CODER], rules=rules))
    assert saved.status_code == 200, saved.text
    view = env.get().json()["config"]
    assert view["extra"][0]["api_key_hint"] == "…0001" and "sk-coder" not in str(view)
    assert view["rules"][0]["target"] == "coder"

    # Saving again without the key keeps it.
    env.put(
        body(extra=[{k: v for k, v in CODER.items() if k != "api_key"}], rules=rules)
    )
    assert (
        dependencies.current_models(env.store)[0].extra[0].api_key
        == "sk-coder-key-0001"
    )
    assert "extra-coder" in dependencies.get_gateway_service().engine.available


def test_incomplete_extra_models_and_bad_rules_are_refused(monkeypatch) -> None:
    env = ModelsEnv(monkeypatch)
    missing_key = {k: v for k, v in CODER.items() if k != "api_key"}
    response = env.put(body(extra=[missing_key]))
    assert response.status_code == 422 and "API key" in response.json()["detail"]
    bad = env.put(body(rules=[{"name": "x", "target": "ghost"}]))
    assert bad.status_code == 422 and "unknown model" in bad.json()["detail"]


def test_cloud_from_complex_keeps_moderate_requests_local(monkeypatch) -> None:
    from ugen_ai_gateway.analysis.schemas import RequestAnalysis
    from ugen_ai_gateway.routing.engine import RuleEngine

    def route(cloud_from: str, complexity: str) -> str:
        stored = StoredModels.model_validate(
            {
                "local": LOCAL,
                "cloud_enabled": True,
                "cloud": CLOUD,
                "cloud_from": cloud_from,
            }
        )
        analysis = RequestAnalysis(
            contains_personal_data=False,
            complexity=complexity,  # type: ignore[arg-type]
            task_type="general",
            reason="test",
        )
        return RuleEngine(to_routing(stored)).decide(analysis).locality

    assert route("moderate", "moderate") == "cloud"
    assert route("complex", "moderate") == "local"
    assert route("complex", "complex") == "cloud"

    # Saved from the Models page and shown back.
    env = ModelsEnv(monkeypatch)
    saved = env.put(body(cloud_enabled=True, cloud=CLOUD, cloud_from="complex"))
    assert saved.status_code == 200, saved.text
    assert env.get().json()["config"]["cloud_from"] == "complex"
