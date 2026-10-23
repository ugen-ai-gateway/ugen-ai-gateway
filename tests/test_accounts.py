import asyncio
import base64
import sqlite3
from typing import Any

import pytest

from tests.test_keys import Env
from ugen_ai_gateway.services.usage import UsageStore, hash_password, verify_password

PASSWORD = "correct horse"


class AccountEnv(Env):
    def __init__(self, monkeypatch: Any, **settings: Any) -> None:
        super().__init__(monkeypatch, **settings)
        monkeypatch.setattr(
            "ugen_ai_gateway.api.auth_routes.get_settings", lambda: self.settings
        )

    def register(self, username: str, password: str = PASSWORD) -> Any:
        return self.client.post(
            "/v1/ugen/auth/register", json={"username": username, "password": password}
        )

    def login(self, username: str, password: str = PASSWORD) -> Any:
        return self.client.post(
            "/v1/ugen/auth/login", json={"username": username, "password": password}
        )

    def token(self, username: str) -> str:
        response = self.register(username)
        assert response.status_code == 201, response.text
        return response.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_first_account_is_admin_and_locks_the_gateway(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    assert env.client.get("/v1/ugen/auth/status").json() == {
        "accounts_enabled": True,
        "has_users": False,
        "registration_open": True,
        "sso": None,
    }
    assert env.chat().status_code == 200  # open until the first account exists

    first = env.register("alice")
    assert first.status_code == 201
    body = first.json()
    assert body["token"].startswith("ugs_")
    assert body["user"]["username"] == "alice"
    assert body["user"]["is_admin"] is True
    assert "password_hash" not in body["user"]

    assert env.chat().status_code == 401
    assert env.chat(body["token"]).status_code == 200
    second = env.register("bob").json()
    assert second["user"]["is_admin"] is False


def test_login_logout_and_wrong_password(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    env.token("alice")

    assert env.login("alice", "wrong password").status_code == 401
    assert env.login("nobody").status_code == 401
    assert env.login("ALICE").status_code == 200  # usernames ignore case

    token = env.login("alice").json()["token"]
    me = env.client.get("/v1/ugen/auth/me", headers=bearer(token))
    assert me.status_code == 200 and me.json()["username"] == "alice"

    assert (
        env.client.post("/v1/ugen/auth/logout", headers=bearer(token)).status_code
        == 204
    )
    assert env.client.get("/v1/ugen/auth/me", headers=bearer(token)).status_code == 401
    assert env.chat(token).status_code == 401


def test_usernames_are_unique_and_validated(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    env.token("alice")
    assert env.register("Alice").status_code == 409
    assert env.register("a").status_code == 422
    assert env.register("has space").status_code == 422
    assert env.register("carol", "short").status_code == 422


def test_registration_can_be_closed_after_the_first_account(monkeypatch) -> None:
    env = AccountEnv(monkeypatch, allow_registration=False)
    assert env.client.get("/v1/ugen/auth/status").json()["registration_open"] is True
    assert env.register("alice").status_code == 201
    assert env.client.get("/v1/ugen/auth/status").json()["registration_open"] is False
    assert env.register("bob").status_code == 403


def test_members_see_only_their_own_usage(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    admin, member = env.token("alice"), env.token("bob")
    assert env.chat(admin).status_code == 200
    assert env.chat(member).status_code == 200
    assert env.chat(member).status_code == 200

    def completed(token: str) -> int:
        return env.client.get("/v1/ugen/stats", headers=bearer(token)).json()[
            "completed"
        ]

    assert completed(member) == 2
    assert completed(admin) == 3

    admin_request = env.client.get("/v1/ugen/requests", headers=bearer(admin)).json()
    alice_id = [r for r in admin_request["data"] if r["user_id"]][-1]["id"]
    feedback = {"request_id": alice_id, "rating": 1}
    other = env.client.post("/v1/ugen/feedback", json=feedback, headers=bearer(member))
    assert other.status_code == 404


def test_only_admin_accounts_manage_api_keys(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    admin, member = env.token("alice"), env.token("bob")

    assert env.client.get("/v1/ugen/keys", headers=bearer(member)).status_code == 403
    assert env.client.get("/v1/ugen/keys").status_code == 403
    created = env.client.post(
        "/v1/ugen/keys", json={"name": "team"}, headers=bearer(admin)
    )
    assert created.status_code == 201
    assert env.chat(created.json()["key"]).status_code == 200


def test_delete_account_needs_the_password_and_removes_its_data(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    admin, member = env.token("alice"), env.token("bob")
    assert env.chat(member).status_code == 200

    wrong = env.client.request(
        "DELETE", "/v1/ugen/auth/me", json={"password": "nope"}, headers=bearer(member)
    )
    assert wrong.status_code == 403
    deleted = env.client.request(
        "DELETE",
        "/v1/ugen/auth/me",
        json={"password": PASSWORD},
        headers=bearer(member),
    )
    assert deleted.status_code == 204

    assert env.chat(member).status_code == 401
    assert env.login("bob").status_code == 401
    assert (
        env.client.get("/v1/ugen/stats", headers=bearer(admin)).json()["requests"] == 0
    )
    assert env.register("bob").status_code == 201  # the name is free again


def test_last_admin_leaving_promotes_the_oldest_member(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    admin = env.token("alice")
    env.token("bob")
    env.token("carol")

    env.client.request(
        "DELETE", "/v1/ugen/auth/me", json={"password": PASSWORD}, headers=bearer(admin)
    )

    assert env.login("bob").json()["user"]["is_admin"] is True
    assert env.login("carol").json()["user"]["is_admin"] is False
    # Deleting every account does not reopen the gateway.
    assert env.chat().status_code == 401


def test_sign_in_attempts_are_rate_limited(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    env.token("alice")
    codes = [env.login("alice", "wrong password").status_code for _ in range(12)]
    assert codes[:9] == [401] * 9
    assert codes[-1] == 429


def test_sessions_expire() -> None:
    async def scenario() -> None:
        store = UsageStore(":memory:")
        user = await store.create_user("alice", PASSWORD)
        token, _ = await store.create_session(user["id"], ttl_seconds=-1)
        assert await store.find_session(token) is None

    asyncio.run(scenario())


def test_passwords_are_salted_hashes() -> None:
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second and PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong", first)
    assert not verify_password(PASSWORD, "garbage")


@pytest.mark.parametrize("path", ["/api/health", "/health"])
def test_api_is_also_served_under_the_api_prefix(monkeypatch, path: str) -> None:
    env = AccountEnv(monkeypatch)
    assert env.client.get(path).json() == {"status": "ok"}


PNG = (
    "data:image/png;base64,"
    + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
)


def test_avatar_can_be_set_and_removed(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    token = env.token("alice")

    saved = env.client.put(
        "/v1/ugen/auth/me/avatar", json={"image": PNG}, headers=bearer(token)
    )
    assert saved.status_code == 200 and saved.json()["avatar"] == PNG
    assert (
        env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()["avatar"]
        == PNG
    )
    assert env.login("alice").json()["user"]["avatar"] == PNG

    removed = env.client.put(
        "/v1/ugen/auth/me/avatar", json={"image": None}, headers=bearer(token)
    )
    assert removed.json()["avatar"] is None
    assert (
        env.client.put("/v1/ugen/auth/me/avatar", json={"image": PNG}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "image",
    [
        "data:image/svg+xml;base64,"
        + base64.b64encode(b"<svg onload=alert(1)>").decode(),
        "data:image/png;base64," + base64.b64encode(b"<html>not a png</html>").decode(),
        "data:image/png;base64,***",
        "https://example.com/me.png",
        "data:image/png;base64,"
        + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 210_000).decode()[:299_000],
    ],
)
def test_avatar_rejects_anything_but_a_small_image(monkeypatch, image: str) -> None:
    env = AccountEnv(monkeypatch)
    token = env.token("alice")
    response = env.client.put(
        "/v1/ugen/auth/me/avatar", json={"image": image}, headers=bearer(token)
    )
    assert response.status_code == 422


def test_existing_database_gains_the_avatar_column(tmp_path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, "
        "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, last_login_at REAL)"
    )
    old.commit()
    old.close()

    async def scenario() -> None:
        store = UsageStore(path)
        user = await store.create_user("alice", PASSWORD)
        assert (await store.set_avatar(user["id"], PNG))["avatar"] == PNG  # type: ignore[index]

    asyncio.run(scenario())


def test_configuration_endpoints_need_sign_in(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    token = env.token("alice")
    for path in ("/v1/ugen/config", "/v1/ugen/providers", "/v1/models"):
        assert env.client.get(path).status_code == 401, path
        assert env.client.get(path, headers=bearer(token)).status_code == 200, path
