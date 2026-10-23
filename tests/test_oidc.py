import base64
import json
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from tests.test_accounts import PASSWORD, AccountEnv, bearer
from ugen_ai_gateway.services.oidc import OidcClient, username_from

ISSUER = "https://id.example.com"
BROWSER = "browser-value-1234567890"
CALLBACK = "https://testserver/v1/ugen/auth/oidc/callback"


def _jwt(claims: dict[str, Any]) -> str:
    def encode(part: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'RS256'})}.{encode(claims)}.signature"


class FakeProvider:
    """An OpenID provider that signs in whoever ``person`` describes."""

    def __init__(self) -> None:
        self.person: dict[str, Any] = {"sub": "123", "email": "dana@corp.example"}
        self.claims: dict[str, Any] = {}
        self.nonce = ""
        self.token_form: dict[str, list[str]] = {}
        self.issuer = ISSUER
        self.token_issuer = ISSUER

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{self.issuer}/authorize",
                    "token_endpoint": f"{self.issuer}/token",
                },
            )
        if request.url.path == "/token":
            self.token_form = parse_qs(request.content.decode())
            if self.token_form.get("code") != ["good-code"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {
                "iss": self.token_issuer,
                "aud": "ugen",
                "nonce": self.nonce,
                "exp": time.time() + 300,
                **self.person,
                **self.claims,
            }
            return httpx.Response(200, json={"id_token": _jwt(claims)})
        return httpx.Response(404)


class SsoEnv(AccountEnv):
    def __init__(self, monkeypatch: Any, **settings: Any) -> None:
        super().__init__(monkeypatch, **settings)
        self.provider = FakeProvider()
        client = OidcClient(httpx.MockTransport(self.provider.handle))
        monkeypatch.setattr(
            "ugen_ai_gateway.api.auth_routes.get_oidc_client", lambda: client
        )
        monkeypatch.setattr(
            "ugen_ai_gateway.api.user_routes.get_oidc_client", lambda: client
        )

    def configure(self, admin: str, **settings: Any) -> Any:
        body = {
            "enabled": True,
            "issuer": ISSUER,
            "client_id": "ugen",
            "client_secret": "shh",
        }
        return self.client.put(
            "/v1/ugen/sso", json=body | settings, headers=bearer(admin)
        )

    def sign_in(self, code: str = "good-code") -> str:
        """Run the browser side of a sign-in; return the dashboard fragment."""
        start = self.client.get(
            "/v1/ugen/auth/oidc/start",
            params={"browser": BROWSER},
            follow_redirects=False,
        )
        assert start.status_code == 302
        target = urlparse(start.headers["location"])
        if target.netloc not in ("id.example.com", "accounts.google.com"):
            return target.fragment
        query = parse_qs(target.query)
        self.provider.nonce = query["nonce"][0]
        done = self.client.get(
            "/v1/ugen/auth/oidc/callback",
            params={"code": code, "state": query["state"][0]},
            follow_redirects=False,
        )
        assert done.status_code == 302
        fragment = urlparse(done.headers["location"]).fragment
        result = parse_qs(fragment)
        if "sso" not in result:
            return fragment
        # The session comes back with the starting browser's value.
        assert result["browser"] == [BROWSER]
        return f"sso={result['sso'][0]}"


def test_sso_creates_an_account_and_signs_it_in(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    assert env.client.get("/v1/ugen/auth/status").json()["sso"] is None

    saved = env.configure(admin, button_label="Sign in with Corp")
    assert saved.status_code == 200, saved.text
    assert saved.json()["client_secret"] == ""
    assert saved.json()["client_secret_set"] is True
    assert saved.json()["callback_url"] == CALLBACK
    assert "shh" not in json.dumps(env.store.load_setting("oidc"))
    assert env.client.get("/v1/ugen/auth/status").json()["sso"] == {
        "label": "Sign in with Corp",
        "provider": "oidc",
    }

    start = env.client.get(
        "/v1/ugen/auth/oidc/start", params={"browser": BROWSER}, follow_redirects=False
    )
    query = parse_qs(urlparse(start.headers["location"]).query)
    assert query["redirect_uri"] == [CALLBACK]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["openid email profile"]

    fragment = env.sign_in()
    assert fragment.startswith("sso=ugs_")
    token = fragment.removeprefix("sso=")
    assert env.provider.token_form["client_secret"] == ["shh"]
    assert env.provider.token_form["code_verifier"]
    me = env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()
    assert me["username"] == "dana"
    assert me["sso"] is True and me["has_password"] is False
    assert me["is_admin"] is False
    assert me["email"] == "dana@corp.example"

    # Signing in again reuses the account.
    again = env.sign_in().removeprefix("sso=")
    assert (
        env.client.get("/v1/ugen/auth/me", headers=bearer(again)).json()["id"]
        == me["id"]
    )
    # No password matches an SSO account.
    assert env.login("dana", "!").status_code in (401, 422)


def test_sso_username_collisions_get_a_suffix(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("dana")
    env.configure(admin)
    token = env.sign_in().removeprefix("sso=")
    assert (
        env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()["username"]
        == "dana-2"
    )


def test_sso_rejections(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin, allowed_domains=["@Other.example"], auto_create=True)
    assert "sso_error=" in env.sign_in()
    assert "domain" in unquote(env.sign_in())

    env.configure(admin, allowed_domains=[])
    assert "expired" in unquote(env.sign_in(code="bad-code")) or "refused" in unquote(
        env.sign_in(code="bad-code")
    )

    env.provider.claims = {"aud": "someone-else"}
    assert "different application" in unquote(env.sign_in())
    env.provider.claims = {"nonce": "replayed"}
    assert "does not belong" in unquote(env.sign_in())
    env.provider.claims = {"exp": time.time() - 3600}
    assert "expired" in unquote(env.sign_in())
    env.provider.claims = {}

    # A state that was never issued (or already used) is refused.
    stale = env.client.get(
        "/v1/ugen/auth/oidc/callback",
        params={"code": "good-code", "state": "made-up"},
        follow_redirects=False,
    )
    assert "expired" in unquote(stale.headers["location"])

    # Without auto-create, unknown people cannot sign in.
    env.configure(admin, auto_create=False)
    assert "No account" in unquote(env.sign_in())


def test_disabled_sso_account_cannot_sign_in(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin)
    token = env.sign_in().removeprefix("sso=")
    user_id = env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()["id"]
    assert (
        env.client.patch(
            f"/v1/ugen/users/{user_id}", json={"disabled": True}, headers=bearer(admin)
        ).status_code
        == 200
    )
    assert "disabled" in unquote(env.sign_in())


def test_sso_settings_validation_and_secret_handling(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    member = env.token("bob")
    assert env.client.get("/v1/ugen/sso", headers=bearer(member)).status_code == 403
    assert (
        env.client.put(
            "/v1/ugen/sso", json={"enabled": True}, headers=bearer(admin)
        ).status_code
        == 422
    )
    assert env.configure(admin, issuer="not a url").status_code == 422

    env.configure(admin)
    # Saving without a secret keeps the stored one.
    env.client.put("/v1/ugen/sso", json={"button_label": "Corp"}, headers=bearer(admin))
    view = env.client.get("/v1/ugen/sso", headers=bearer(admin)).json()
    assert view["client_secret_set"] is True and view["button_label"] == "Corp"
    env.client.put(
        "/v1/ugen/sso", json={"clear_client_secret": True}, headers=bearer(admin)
    )
    assert (
        env.client.get("/v1/ugen/sso", headers=bearer(admin)).json()[
            "client_secret_set"
        ]
        is False
    )

    test = env.client.post("/v1/ugen/sso/test", json={}, headers=bearer(admin)).json()
    assert test["ok"] is True
    bad = env.client.post(
        "/v1/ugen/sso/test",
        json={"issuer": "https://elsewhere.example"},
        headers=bearer(admin),
    ).json()
    assert bad["ok"] is False


def test_callback_url_keeps_the_api_prefix(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    view = env.client.get("/api/v1/ugen/sso", headers=bearer(admin)).json()
    assert view["callback_url"] == "https://testserver/api/v1/ugen/auth/oidc/callback"
    env.configure(admin)
    start = env.client.get(
        "/api/v1/ugen/auth/oidc/start",
        params={"browser": BROWSER},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(start.headers["location"]).query)
    env.provider.nonce = query["nonce"][0]
    done = env.client.get(
        "/api/v1/ugen/auth/oidc/callback",
        params={"code": "good-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    # Back to the dashboard root, not under /api.
    assert done.headers["location"].startswith("https://testserver/#sso=ugs_")


def test_sso_account_password_and_deletion(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin)
    token = env.sign_in().removeprefix("sso=")
    # An SSO account can set a first password without a current one.
    set_password = env.client.put(
        "/v1/ugen/auth/me/password",
        json={"new_password": PASSWORD},
        headers=bearer(token),
    )
    assert set_password.status_code == 204
    assert env.login("dana").status_code == 200

    token = env.sign_in().removeprefix("sso=")
    assert (
        env.client.request(
            "DELETE",
            "/v1/ugen/auth/me",
            json={"password": "nope"},
            headers=bearer(token),
        ).status_code
        == 403
    )

    # A password-less account confirms deletion with its username.
    env.provider.person = {"sub": "456", "preferred_username": "erin"}
    erin = env.sign_in().removeprefix("sso=")
    assert (
        env.client.request(
            "DELETE",
            "/v1/ugen/auth/me",
            json={"username": "nope"},
            headers=bearer(erin),
        ).status_code
        == 403
    )
    assert (
        env.client.request(
            "DELETE",
            "/v1/ugen/auth/me",
            json={"username": "ERIN"},
            headers=bearer(erin),
        ).status_code
        == 204
    )


def test_first_account_can_come_from_sso(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    from ugen_ai_gateway.dependencies import get_secret_box
    from ugen_ai_gateway.services.oidc import OidcSettings

    settings = OidcSettings(enabled=True, issuer=ISSUER, client_id="ugen")
    env.store._save_setting("oidc", settings.sealed(get_secret_box()))
    token = env.sign_in().removeprefix("sso=")
    assert env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()["is_admin"]


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("dana.lee@corp.example", "dana.lee"),
        ("Dana Lee", "Dana-Lee"),
        ("李小明", "user"),
        ("ab", "user-ab"),
        ("x" * 40, "x" * 28),
    ],
)
def test_username_from(hint: str, expected: str) -> None:
    assert username_from(hint) == expected


def test_google_preset(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.provider.issuer = "https://accounts.google.com"
    # Google may write its issuer without the scheme in ID tokens.
    env.provider.token_issuer = "accounts.google.com"
    saved = env.configure(
        admin, issuer="https://accounts.google.com", button_label="Sign in with Google"
    )
    assert saved.json()["provider"] == "google"
    assert env.client.get("/v1/ugen/auth/status").json()["sso"] == {
        "label": "Sign in with Google",
        "provider": "google",
    }
    token = env.sign_in().removeprefix("sso=")
    assert env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()["sso"]

    # Other providers must match exactly.
    env.provider.issuer = env.provider.token_issuer = ISSUER
    env.configure(admin, issuer=ISSUER)
    env.provider.token_issuer = "id.example.com"
    assert "different issuer" in unquote(env.sign_in())


def test_callback_url_uses_https_behind_a_proxy(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")

    def callback(**headers: str) -> str:
        return env.client.get(
            "/api/v1/ugen/sso", headers=bearer(admin) | headers
        ).json()["callback_url"]

    path = "/api/v1/ugen/auth/oidc/callback"
    # A tunnel such as cloudflared sends plain HTTP with the public host.
    assert callback(host="ugen.example.com") == f"https://ugen.example.com{path}"
    assert (
        callback(host="ugen.example.com", **{"x-forwarded-proto": "https"})
        == f"https://ugen.example.com{path}"
    )
    # Local and private addresses keep http.
    assert callback(host="localhost:7632") == f"http://localhost:7632{path}"
    assert callback(host="192.168.1.20:7632") == f"http://192.168.1.20:7632{path}"
    assert (
        callback(host="localhost:7632", **{"x-forwarded-proto": "https"})
        == f"https://localhost:7632{path}"
    )


def test_sign_on_must_start_from_the_dashboard(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin)
    # Without the browser value (for example a link made by someone else).
    start = env.client.get("/v1/ugen/auth/oidc/start", follow_redirects=False)
    assert "sso_error=" in start.headers["location"]


def _link(env: "SsoEnv", token: str) -> Any:
    """Run a link from the Account page; return the completion response."""
    ticket = env.client.post("/v1/ugen/auth/oidc/link", headers=bearer(token))
    assert ticket.status_code == 200, ticket.text
    start = env.client.get(
        "/v1/ugen/auth/oidc/start",
        params={"browser": BROWSER, "link": ticket.json()["ticket"]},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(start.headers["location"]).query)
    env.provider.nonce = query["nonce"][0]
    done = env.client.get(
        "/v1/ugen/auth/oidc/callback",
        params={"code": "good-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    result = parse_qs(urlparse(done.headers["location"]).fragment)
    assert result["browser"] == [BROWSER] and "sso" not in result
    return env.client.post(
        "/v1/ugen/auth/oidc/link/complete",
        json={"code": result["sso_link"][0]},
        headers=bearer(token),
    )


def test_link_sso_to_an_existing_account(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin, auto_create=False)
    linked = _link(env, admin)
    assert linked.status_code == 200, linked.text
    assert linked.json()["sso"] is True and linked.json()["has_password"] is True

    # Signing in with the provider now opens the existing account.
    token = env.sign_in().removeprefix("sso=")
    me = env.client.get("/v1/ugen/auth/me", headers=bearer(token)).json()
    assert me["username"] == "alice" and me["is_admin"] is True

    # Unlinking works because the account has a password.
    unlinked = env.client.delete("/v1/ugen/auth/me/sso", headers=bearer(admin))
    assert unlinked.status_code == 200 and unlinked.json()["sso"] is False
    assert "No account" in unquote(env.sign_in())


def test_link_refuses_an_identity_used_by_another_account(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin)
    env.sign_in()  # creates "dana" for the provider identity
    taken = _link(env, admin)
    assert taken.status_code == 409 and "'dana'" in taken.json()["detail"]


def test_link_result_only_works_for_the_account_that_started_it(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    bob = env.token("bob")
    env.configure(admin)
    ticket = env.client.post("/v1/ugen/auth/oidc/link", headers=bearer(bob))
    start = env.client.get(
        "/v1/ugen/auth/oidc/start",
        params={"browser": BROWSER, "link": ticket.json()["ticket"]},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(start.headers["location"]).query)
    env.provider.nonce = query["nonce"][0]
    done = env.client.get(
        "/v1/ugen/auth/oidc/callback",
        params={"code": "good-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    code = parse_qs(urlparse(done.headers["location"]).fragment)["sso_link"][0]
    # Alice cannot redeem Bob's link, and tickets are single use.
    stolen = env.client.post(
        "/v1/ugen/auth/oidc/link/complete", json={"code": code}, headers=bearer(admin)
    )
    assert stolen.status_code == 400
    reused = env.client.get(
        "/v1/ugen/auth/oidc/start",
        params={"browser": BROWSER, "link": ticket.json()["ticket"]},
        follow_redirects=False,
    )
    assert "expired" in unquote(reused.headers["location"])


def test_password_less_accounts_cannot_unlink(monkeypatch) -> None:
    env = SsoEnv(monkeypatch)
    admin = env.token("alice")
    env.configure(admin)
    token = env.sign_in().removeprefix("sso=")
    refused = env.client.delete("/v1/ugen/auth/me/sso", headers=bearer(token))
    assert refused.status_code == 409
