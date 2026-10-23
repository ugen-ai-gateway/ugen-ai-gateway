from typing import Any

import pytest

from tests.test_accounts import bearer
from tests.test_models_config import CLOUD, LOCAL, ModelsEnv, body
from ugen_ai_gateway import dependencies
from ugen_ai_gateway.analysis.custom_rules import (
    CustomRule,
    PrivacySettings,
    rules_finder,
)
from ugen_ai_gateway.analysis.pii import find_personal_data, set_custom_finder
from ugen_ai_gateway.api import privacy_routes


def rule(**fields: Any) -> dict[str, Any]:
    return {"name": "Project", "kind": "keyword", "pattern": "Falcon", **fields}


def test_keywords_match_whole_terms_case_insensitively_also_in_cjk() -> None:
    finder = rules_finder([CustomRule(name="Project", pattern="Falcon\nBlue Sky")])
    set_custom_finder(finder, types=["custom:project"])
    found = find_personal_data("falcon, Falconry, 藍天計畫 Blue Sky 與Falcon合作")
    assert [m.value for m in found] == ["falcon", "Blue Sky", "Falcon"]
    assert {m.label for m in found} == {"PROJECT"}


def test_regex_rules_and_validation() -> None:
    finder = rules_finder(
        [CustomRule(name="Ticket", kind="regex", pattern=r"OPS-\d{4}")]
    )
    assert [m.value for m in finder("see OPS-1234 and OPS-12")] == ["OPS-1234"]
    with pytest.raises(ValueError, match="Invalid regular expression"):
        CustomRule(name="Bad", kind="regex", pattern="(unclosed")
    with pytest.raises(ValueError, match="empty"):
        CustomRule(name="Empty", kind="regex", pattern="x*")
    with pytest.raises(ValueError, match="different"):
        PrivacySettings(rules=[rule(), rule(name="project")])
    # Settings saved by older versions may carry never_cloud; it is ignored.
    assert PrivacySettings(never_cloud=["email"]).never_cloud_types() == []


class PrivacyEnv(ModelsEnv):
    def __init__(self, monkeypatch: Any) -> None:
        super().__init__(monkeypatch)
        self.put(body(cloud_enabled=True, cloud=CLOUD, local=LOCAL))
        self.service = dependencies.get_gateway_service()
        self.cloud = _Recorder()
        self.local = _Recorder()
        self.service._providers = {"local": self.local, "cloud": self.cloud}
        self.service._analyzer._provider = self.local  # type: ignore[attr-defined]

    def save(self, **privacy: Any) -> Any:
        response = self.client.put(
            "/v1/ugen/privacy", json=privacy, headers=bearer(self.admin)
        )
        # Saving rebuilds the gateway; keep the recording providers.
        service = dependencies.get_gateway_service()
        service._providers = {"local": self.local, "cloud": self.cloud}
        service._analyzer._provider = self.local  # type: ignore[attr-defined]
        return response


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.calls.append(kwargs)
        if "request analysis component" in str(kwargs["messages"][0]["content"]):
            text = '{"contains_personal_data": false, "personal_data_types": [], "personal_data_values": [], "complexity": "complex", "task_type": "general", "reason": "x"}'
        else:
            text = "Falcon plan: " + str(kwargs["messages"][-1]["content"])
        message = SimpleNamespace(content=text)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            model="m",
            usage=None,
        )

    async def health_check(self) -> None:
        pass


def test_custom_terms_never_reach_the_cloud_and_come_back_in_the_reply(
    monkeypatch,
) -> None:
    env = PrivacyEnv(monkeypatch)
    assert env.save(rules=[rule()]).status_code == 200
    reply = env.chat(
        env.admin,
        messages=[
            {"role": "user", "content": "Compare the Falcon rollout architecture"}
        ],
    )
    sent = str(env.cloud.calls[-1]["messages"])
    assert "Falcon" not in sent and "[PROJECT_1]" in sent
    assert "Falcon rollout architecture" in reply.json()["content"]


def test_keep_local_rules_stay_on_the_local_model(
    monkeypatch,
) -> None:
    env = PrivacyEnv(monkeypatch)
    env.save(rules=[rule(keep_local=True)])
    falcon = env.chat(
        env.admin,
        messages=[
            {"role": "user", "content": "Compare the Falcon rollout architecture"}
        ],
    )
    assert falcon.json()["provider"] == "local"
    assert falcon.json()["routing"]["rule"] == "privacy.never_cloud"
    email = env.chat(
        env.admin,
        messages=[
            {"role": "user", "content": "Compare the mail architecture for a@b.co"}
        ],
    )
    # Built-in personal data is redacted, not kept local.
    assert email.json()["provider"] == "cloud"
    assert "a@b.co" not in str(env.cloud.calls[-1]["messages"])
    plain = env.chat(
        env.admin,
        messages=[{"role": "user", "content": "Compare the rollout architecture"}],
    )
    assert plain.json()["provider"] == "cloud"


def test_try_it_previews_without_saving(monkeypatch) -> None:
    env = PrivacyEnv(monkeypatch)
    preview = env.client.post(
        "/v1/ugen/privacy/test",
        json={
            "text": "Falcon ships to a@b.co",
            "settings": {"rules": [rule(keep_local=True)]},
        },
        headers=bearer(env.admin),
    ).json()
    assert preview["redacted"] == "[PROJECT_1] ships to [EMAIL_1]"
    assert preview["keeps_local"] == ["custom:project"]
    # Nothing was saved, so the term is not detected afterwards.
    assert find_personal_data("Falcon") == []


def test_try_it_never_changes_what_other_requests_detect(monkeypatch) -> None:
    env = PrivacyEnv(monkeypatch)
    env.save(rules=[rule()])
    seen_during_preview: list[list[str]] = []
    real_rules_finder = privacy_routes.rules_finder

    def spying_rules_finder(rules: Any) -> Any:
        preview_finder = real_rules_finder(rules)

        def find(text: str) -> Any:
            # A request running at this moment, on another thread, uses the
            # saved rules: "Falcon" must still be detected.
            seen_during_preview.append([m.value for m in find_personal_data("Falcon")])
            return preview_finder(text)

        return find

    monkeypatch.setattr(privacy_routes, "rules_finder", spying_rules_finder)
    preview = env.client.post(
        "/v1/ugen/privacy/test",
        json={
            "text": "Osprey ships",
            "settings": {"rules": [rule(name="Bird", pattern="Osprey")]},
        },
        headers=bearer(env.admin),
    ).json()

    assert preview["redacted"] == "[BIRD_1] ships"
    assert seen_during_preview and all(
        seen == ["Falcon"] for seen in seen_during_preview
    )
    assert [m.value for m in find_personal_data("Falcon")] == ["Falcon"]


def test_only_administrators_manage_privacy(monkeypatch) -> None:
    env = PrivacyEnv(monkeypatch)
    member = env.token("bob")
    assert env.client.get("/v1/ugen/privacy", headers=bearer(member)).status_code == 403
    view = env.client.get("/v1/ugen/privacy", headers=bearer(env.admin)).json()
    assert view == {"rules": []}
