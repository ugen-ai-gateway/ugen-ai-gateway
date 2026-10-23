import secrets
from typing import Any

from tests.test_accounts import AccountEnv, bearer
from ugen_ai_gateway.config import Settings


def conversation(cid: str, text: str = "hello", **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "title": f"About {text}",
        "updatedAt": 1,
        "turns": [{"id": "t1", "user": text}],
        **extra,
    }


def sync(env: AccountEnv, token: str, **body: Any) -> Any:
    return env.client.post(
        "/v1/ugen/conversations/sync", json=body, headers=bearer(token)
    )


def listing(env: AccountEnv, token: str, **params: Any) -> dict[str, Any]:
    response = env.client.get(
        "/v1/ugen/conversations", params=params, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_conversations_sync_across_sessions(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    laptop = env.token("alice")
    phone = env.login("alice").json()["token"]

    saved = sync(env, laptop, save=[conversation("c1", memory={"text": "likes tea"})])
    assert saved.status_code == 200, saved.text
    first = listing(env, phone)
    assert first["ids"] == ["c1"]
    assert first["conversations"][0]["memory"] == {"text": "likes tea"}
    assert first["unreadable"] == 0

    # Only changes after `since` come back, but every id is listed.
    since = first["server_time"]
    assert listing(env, phone, since=since)["conversations"] == []
    sync(env, phone, save=[conversation("c2", "phone")])
    later = listing(env, laptop, since=since)
    assert [c["id"] for c in later["conversations"]] == ["c2"]
    assert set(later["ids"]) == {"c1", "c2"}

    # Saving the same id replaces it; deleting removes it.
    sync(env, laptop, save=[conversation("c1", "edited")], delete=["c2"])
    final = listing(env, phone)
    assert final["ids"] == ["c1"]
    assert final["conversations"][0]["turns"][0]["user"] == "edited"


def test_conversations_are_private_and_encrypted(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    sync(env, alice, save=[conversation("c1", "my card 4111 1111 1111 1111")])

    assert listing(env, bob)["ids"] == []
    # Bob deleting the same id does not touch Alice's conversation.
    sync(env, bob, delete=["c1"])
    assert listing(env, alice)["ids"] == ["c1"]

    raw = env.store._connection.execute("SELECT data FROM conversations").fetchone()
    assert b"4111" not in raw[0] and b"About" not in raw[0]


def test_conversation_sync_needs_a_session(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    env.token("alice")
    assert env.client.get("/v1/ugen/conversations").status_code == 401
    # Only dashboard sessions have accounts; other credentials are refused.
    assert (
        env.client.get(
            "/v1/ugen/conversations", headers=bearer("ugk_not_a_session")
        ).status_code
        == 401
    )


def test_conversation_validation_and_limits(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    token = env.token("alice")
    assert sync(env, token, save=[conversation("bad id!")]).status_code == 422
    assert sync(env, token, delete=["../x"]).status_code == 422
    big = conversation("big", "x" * 5_100_000)
    assert sync(env, token, save=[big]).status_code == 413

    monkeypatch.setattr(
        "ugen_ai_gateway.api.conversation_routes.get_settings",
        lambda: Settings(conversation_storage_mb=0.001),
    )
    # Random text barely compresses.
    full = sync(env, token, save=[conversation("c1", secrets.token_hex(2000))])
    assert full.status_code == 507
    assert listing(env, token)["ids"] == []  # rolled back
    # Deleting still works when storage is full.
    assert sync(env, token, delete=["c1"]).status_code == 200


def test_deleting_an_account_deletes_its_conversations(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    env.token("admin")
    token = env.token("alice")
    sync(env, token, save=[conversation("c1")])
    response = env.client.request(
        "DELETE",
        "/v1/ugen/auth/me",
        json={"password": "correct horse"},
        headers=bearer(token),
    )
    assert response.status_code == 204
    count = env.store._connection.execute("SELECT COUNT(*) FROM conversations")
    assert count.fetchone()[0] == 0


def test_unreadable_conversations_are_skipped(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    token = env.token("alice")
    sync(env, token, save=[conversation("c1"), conversation("c2")])
    env.store._connection.execute(
        "UPDATE conversations SET data = ? WHERE id = 'c2'", (b"garbage",)
    )
    result = listing(env, token)
    assert [c["id"] for c in result["conversations"]] == ["c1"]
    assert result["unreadable"] == 1
