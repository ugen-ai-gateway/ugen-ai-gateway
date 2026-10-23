from typing import Any

from tests.test_accounts import AccountEnv, bearer
from tests.test_conversations import conversation, listing, sync


def answered(cid: str, text: str = "hello", answer: str = "hi there") -> dict[str, Any]:
    item = conversation(cid, text, memory={"text": "private summary"})
    item["turns"][0]["result"] = {
        "content": answer,
        "model": "local-model",
        "provider": "local",
        "routing": {"complexity": "simple", "personal_data_types": ["email"]},
    }
    return item


def test_share_links_show_a_snapshot(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    created = env.client.post(
        "/v1/ugen/shares", json={"conversation": answered("c1")}, headers=bearer(alice)
    )
    assert created.status_code == 200, created.text
    share_id = created.json()["id"]
    assert created.json()["visibility"] == "accounts"

    # Accounts-only: signed-in people can open it, others cannot.
    assert env.client.get(f"/v1/ugen/shares/{share_id}").status_code == 401
    view = env.client.get(f"/v1/ugen/shares/{share_id}", headers=bearer(bob)).json()
    assert view["owner"] == "alice" and view["is_owner"] is False
    assert view["turns"] == [
        {
            "id": "t1",
            "user": "hello",
            "author": None,
            "answer": "hi there",
            "model": "local-model",
            "provider": "local",
            "stopped": False,
        }
    ]
    # Memory and routing details are not part of a share.
    assert "private summary" not in str(view) and "personal_data_types" not in str(view)

    # Refreshing keeps the link and can make it public.
    again = env.client.post(
        "/v1/ugen/shares",
        json={"conversation": answered("c1", answer="updated"), "visibility": "public"},
        headers=bearer(alice),
    )
    assert again.json()["id"] == share_id
    public = env.client.get(f"/v1/ugen/shares/{share_id}").json()
    assert public["turns"][0]["answer"] == "updated"

    mine = env.client.get("/v1/ugen/shares", headers=bearer(alice)).json()["data"]
    assert [share["conversation_id"] for share in mine] == ["c1"]
    # Only the owner can remove it.
    assert (
        env.client.delete(
            f"/v1/ugen/shares/{share_id}", headers=bearer(bob)
        ).status_code
        == 404
    )
    assert (
        env.client.delete(
            f"/v1/ugen/shares/{share_id}", headers=bearer(alice)
        ).status_code
        == 204
    )
    assert env.client.get(f"/v1/ugen/shares/{share_id}").status_code == 404


def _shared_chat(env: AccountEnv, owner: str, members: list[str]) -> str:
    created = env.client.post(
        "/v1/ugen/shared-chats",
        json={"conversation": answered("c1"), "members": members},
        headers=bearer(owner),
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _chat(env: AccountEnv, token: str, chat_id: str) -> dict[str, Any]:
    return next(c for c in listing(env, token)["conversations"] if c["id"] == chat_id)


def test_shared_chats_merge_members_turns(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    carol = env.token("carol")
    chat_id = _shared_chat(env, alice, ["bob"])

    seen = _chat(env, bob, chat_id)
    assert seen["shared"] == {
        "owner": "alice",
        "members": ["alice", "bob"],
        "role": "member",
    }
    assert chat_id not in listing(env, carol)["ids"]

    # Both add a turn at the same time, each from the copy they loaded.
    alice_copy = _chat(env, alice, chat_id)
    for token, copy, turn in ((alice, alice_copy, "a2"), (bob, seen, "b2")):
        added = {**copy, "turns": [*copy["turns"], {"id": turn, "user": turn}]}
        assert sync(env, token, save=[added]).status_code == 200
    merged = _chat(env, alice, chat_id)
    assert [t["id"] for t in merged["turns"]] == ["t1", "a2", "b2"]
    assert "shared" not in str(
        env.store._connection.execute("SELECT data FROM shared_chats").fetchone()[0]
    )

    # A stale copy without an answer does not erase one; removals stick.
    stale = {**seen, "turns": [{"id": "t1", "user": "hello"}]}
    sync(env, bob, save=[stale], removed_turns={chat_id: ["a2"]})
    after = _chat(env, alice, chat_id)
    assert [t["id"] for t in after["turns"]] == ["t1", "b2"]
    assert after["turns"][0]["result"]["content"] == "hi there"

    # Outsiders cannot write to it.
    assert sync(env, carol, save=[{**seen}]).status_code == 403


def test_shared_chat_members_and_leaving(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    carol = env.token("carol")
    chat_id = _shared_chat(env, alice, ["bob"])

    assert (
        env.client.patch(
            f"/v1/ugen/shared-chats/{chat_id}",
            json={"add": ["carol"]},
            headers=bearer(bob),
        ).status_code
        == 403
    )
    added = env.client.patch(
        f"/v1/ugen/shared-chats/{chat_id}",
        json={"add": ["carol"]},
        headers=bearer(alice),
    )
    assert added.status_code == 200
    assert chat_id in listing(env, carol)["ids"]
    unknown = env.client.patch(
        f"/v1/ugen/shared-chats/{chat_id}",
        json={"add": ["nobody"]},
        headers=bearer(alice),
    )
    assert unknown.status_code == 404

    # A member leaving (through sync, like deleting it from the sidebar).
    sync(env, bob, delete=[chat_id])
    assert chat_id not in listing(env, bob)["ids"]
    assert _chat(env, alice, chat_id)["shared"]["members"] == ["alice", "carol"]

    # The owner deleting it removes it for everyone.
    assert env.client.delete(
        f"/v1/ugen/shared-chats/{chat_id}", headers=bearer(alice)
    ).json() == {"status": "deleted"}
    assert chat_id not in listing(env, carol)["ids"]
