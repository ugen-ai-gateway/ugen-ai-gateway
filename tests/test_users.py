from typing import Any

from tests.test_accounts import PASSWORD, AccountEnv, bearer
from ugen_ai_gateway import cli


class UsersEnv(AccountEnv):
    def __init__(self, monkeypatch: Any, **settings: Any) -> None:
        super().__init__(monkeypatch, **settings)
        self.admin = self.token("alice")
        self.member = self.token("bob")
        self.bob_id = self.client.get(
            "/v1/ugen/auth/me", headers=bearer(self.member)
        ).json()["id"]
        self.alice_id = self.client.get(
            "/v1/ugen/auth/me", headers=bearer(self.admin)
        ).json()["id"]

    def patch(
        self, user_id: str, body: dict[str, Any], token: str | None = None
    ) -> Any:
        return self.client.patch(
            f"/v1/ugen/users/{user_id}", json=body, headers=bearer(token or self.admin)
        )

    def audit_events(self) -> list[str]:
        entries = self.client.get("/v1/ugen/audit", headers=bearer(self.admin)).json()[
            "data"
        ]
        return [entry["event"] for entry in entries]


def test_change_password_signs_out_other_sessions(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    other = env.login("bob").json()["token"]
    wrong = env.client.put(
        "/v1/ugen/auth/me/password",
        json={"current_password": "nope", "new_password": "new password 1"},
        headers=bearer(env.member),
    )
    assert wrong.status_code == 403
    changed = env.client.put(
        "/v1/ugen/auth/me/password",
        json={"current_password": PASSWORD, "new_password": "new password 1"},
        headers=bearer(env.member),
    )
    assert changed.status_code == 204
    assert (
        env.client.get("/v1/ugen/auth/me", headers=bearer(env.member)).status_code
        == 200
    )
    assert env.client.get("/v1/ugen/auth/me", headers=bearer(other)).status_code == 401
    assert env.login("bob").status_code == 401
    assert env.login("bob", "new password 1").status_code == 200


def test_admins_list_users_with_usage_and_members_cannot(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    assert env.chat(env.member).status_code == 200
    assert (
        env.client.get("/v1/ugen/users", headers=bearer(env.member)).status_code == 403
    )
    users = env.client.get("/v1/ugen/users", headers=bearer(env.admin)).json()["data"]
    bob = next(user for user in users if user["username"] == "bob")
    assert bob["month_requests"] == 1 and bob["is_admin"] is False
    assert "password_hash" not in str(users)


def test_disabled_accounts_cannot_sign_in_and_lose_their_sessions(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    assert env.patch(env.bob_id, {"disabled": True}).status_code == 200
    assert env.chat(env.member).status_code == 401
    response = env.login("bob")
    assert response.status_code == 403 and "disabled" in response.json()["detail"]
    env.patch(env.bob_id, {"disabled": False})
    assert env.login("bob").status_code == 200


def test_the_last_administrator_cannot_be_removed(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    # Admins change their own account on the Account page, not here.
    assert env.patch(env.alice_id, {"is_admin": False}).status_code == 400
    assert env.patch(env.bob_id, {"is_admin": True}).status_code == 200
    assert env.patch(env.alice_id, {"is_admin": False}, env.member).status_code == 200
    # Now bob is the only admin.
    assert env.patch(env.bob_id, {"disabled": True}, env.member).status_code == 400


def test_reset_password_returns_a_one_time_password(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    reset = env.client.post(
        f"/v1/ugen/users/{env.bob_id}/reset-password", headers=bearer(env.admin)
    )
    password = reset.json()["password"]
    assert len(password) >= 12
    assert env.chat(env.member).status_code == 401
    assert env.login("bob", password).status_code == 200


def test_admin_deletes_another_account(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    assert (
        env.client.delete(
            f"/v1/ugen/users/{env.bob_id}", headers=bearer(env.admin)
        ).status_code
        == 204
    )
    assert env.login("bob").status_code == 401
    assert (
        env.client.delete(
            f"/v1/ugen/users/{env.alice_id}", headers=bearer(env.admin)
        ).status_code
        == 400
    )


def test_a_users_cloud_budget_keeps_them_local_once_spent(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    env.patch(env.bob_id, {"monthly_budget_usd": 0})
    response = env.chat(env.member)
    assert response.json()["provider"] == "local"
    assert response.json()["routing"]["rule"] == "user.budget"
    env.patch(env.bob_id, {"monthly_budget_usd": None})
    assert env.chat(env.member).json()["provider"] == "cloud"


def test_registration_toggle_is_saved_from_the_dashboard(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    saved = env.client.put(
        "/v1/ugen/settings",
        json={"allow_registration": False},
        headers=bearer(env.admin),
    )
    assert saved.status_code == 200
    assert env.client.get("/v1/ugen/auth/status").json()["registration_open"] is False
    assert env.register("carol").status_code == 403
    assert (
        env.client.put(
            "/v1/ugen/settings",
            json={"allow_registration": True},
            headers=bearer(env.member),
        ).status_code
        == 403
    )


def test_admin_actions_are_audited(monkeypatch) -> None:
    env = UsersEnv(monkeypatch)
    env.patch(env.bob_id, {"monthly_budget_usd": 5})
    env.client.post("/v1/ugen/keys", json={"name": "team"}, headers=bearer(env.admin))
    env.client.put(
        "/v1/ugen/settings",
        json={"allow_registration": False},
        headers=bearer(env.admin),
    )
    events = env.audit_events()
    for event in (
        "account_created",
        "account_updated",
        "api_key_created",
        "settings_updated",
    ):
        assert event in events
    verified = env.client.get("/v1/ugen/audit/verify", headers=bearer(env.admin)).json()
    assert verified["ok"] is True


def test_command_line_resets_a_forgotten_password(
    monkeypatch, tmp_path, capsys
) -> None:
    import asyncio

    from ugen_ai_gateway.config import Settings
    from ugen_ai_gateway.services.usage import UsageStore

    path = tmp_path / "cli.db"
    store = UsageStore(path)
    asyncio.run(store.create_user("alice", PASSWORD))
    store.close()
    monkeypatch.setattr(
        cli, "get_settings", lambda: Settings(_env_file=None, usage_db_path=str(path))
    )  # type: ignore[call-arg]

    cli.main(["reset-password", "alice", "--password", "brand new pass"])
    assert "New password for alice" in capsys.readouterr().out
    assert (
        asyncio.run(UsageStore(path).verify_login("alice", "brand new pass"))
        is not None
    )
    cli.main(["list-users"])
    assert "alice" in capsys.readouterr().out
