"""Single sign-on with any OpenID Connect provider (Google, Microsoft Entra,
Okta, Keycloak, Authentik, ...), using the authorization code flow with PKCE.

The ID token comes straight from the provider's token endpoint over TLS, so
its claims are trusted without checking the signature (OpenID Connect Core
3.1.3.7); the issuer, audience, nonce and expiry are still checked.
"""

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field, field_validator

from ugen_ai_gateway.services.secret_box import SecretBox

OIDC_SETTING = "oidc"
GOOGLE_ISSUER = "https://accounts.google.com"
CALLBACK_PATH = "/v1/ugen/auth/oidc/callback"
# How long a sign-in may take between leaving for the provider and returning.
PENDING_TTL_SECONDS = 600
LINK_TTL_SECONDS = 300
_MAX_PENDING = 1000
_DISCOVERY_TTL_SECONDS = 3600
_TIMEOUT = httpx.Timeout(10.0)


class OidcError(Exception):
    """A sign-in that cannot finish; the message is shown to the person."""


class OidcSettings(BaseModel):
    enabled: bool = False
    issuer: str = Field(default="", max_length=300)
    client_id: str = Field(default="", max_length=300)
    client_secret: str = Field(default="", max_length=2000)
    button_label: str = Field(default="Sign in with SSO", max_length=40)
    # Create an account the first time someone signs in; otherwise only
    # accounts already linked to the provider can sign in.
    auto_create: bool = True
    # Only these email domains may sign in (empty: any). Needs the email claim.
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    # Override the callback URL when the gateway cannot work it out itself.
    redirect_uri: str = Field(default="", max_length=500)

    @field_validator("issuer", "redirect_uri")
    @classmethod
    def check_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not re.match(r"^https?://[^\s/]+", value):
            raise ValueError("Use a full http(s):// URL")
        return value

    @field_validator("allowed_domains")
    @classmethod
    def check_domains(cls, value: list[str]) -> list[str]:
        domains = [domain.strip().lower().lstrip("@") for domain in value]
        return [domain for domain in domains if domain]

    @property
    def provider(self) -> str:
        """ "google" for Google accounts, otherwise "oidc"."""
        return "google" if self.issuer == GOOGLE_ISSUER else "oidc"

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.issuer and self.client_id)

    def sealed(self, box: SecretBox) -> dict[str, Any]:
        return self.model_dump() | {"client_secret": box.seal(self.client_secret)}

    @classmethod
    def unsealed(cls, data: dict[str, Any], box: SecretBox) -> "OidcSettings":
        settings = cls.model_validate(data)
        return settings.model_copy(
            update={"client_secret": box.open(settings.client_secret)}
        )

    def public_view(self) -> dict[str, Any]:
        secret = self.client_secret
        return self.model_dump() | {
            "client_secret": "",
            "client_secret_set": bool(secret),
        }


@dataclass
class _Pending:
    verifier: str
    nonce: str
    redirect_uri: str
    expires_at: float
    # Random value from the browser that started the sign-in, handed back with
    # the session so a session in a link from someone else is refused.
    browser: str = ""
    # Set when an account that is already signed in links this identity.
    link_user: str = ""


@dataclass
class Identity:
    subject: str  # "<issuer>|<sub>", unique across providers
    email: str | None
    name_hint: str  # suggested username


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError) as exc:
        raise OidcError("The provider returned an unreadable ID token") from exc
    if not isinstance(claims, dict):
        raise OidcError("The provider returned an unreadable ID token")
    return claims


class OidcClient:
    """Starts sign-ins and finishes them. Pending sign-ins live in memory, so
    a sign-in started before a restart must be started again."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._pending: dict[str, _Pending] = {}
        self._discovery: dict[str, tuple[float, dict[str, Any]]] = {}
        # Linking an identity to an existing account: a ticket (issued to the
        # signed-in account) starts it, a result code (redeemed by that same
        # account) finishes it. Both are single use and short-lived.
        self._link_tickets: dict[str, tuple[str, float]] = {}
        self._link_results: dict[str, tuple[str, Identity, float]] = {}

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT, follow_redirects=True
        )

    async def discover(self, issuer: str, *, fresh: bool = False) -> dict[str, Any]:
        cached = self._discovery.get(issuer)
        if cached and not fresh and cached[0] > time.time():
            return cached[1]
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with self._http() as http:
                response = await http.get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(
                f"Cannot read the provider's configuration at {url}"
            ) from exc
        if not isinstance(document, dict) or not document.get("authorization_endpoint"):
            raise OidcError("The issuer URL does not point to an OpenID provider")
        if str(document.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise OidcError(
                f"The provider calls itself {document.get('issuer')!r}, not {issuer!r}"
            )
        if not document.get("token_endpoint"):
            raise OidcError("The provider has no token endpoint")
        self._discovery[issuer] = (time.time() + _DISCOVERY_TTL_SECONDS, document)
        return document

    async def authorization_url(
        self,
        settings: OidcSettings,
        redirect_uri: str,
        browser: str = "",
        link_user: str = "",
    ) -> str:
        document = await self.discover(settings.issuer)
        now = time.time()
        # Forget abandoned sign-ins, and keep the table bounded.
        for state in [s for s, p in self._pending.items() if p.expires_at <= now]:
            del self._pending[state]
        while len(self._pending) >= _MAX_PENDING:
            del self._pending[next(iter(self._pending))]
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        self._pending[state] = _Pending(
            verifier, nonce, redirect_uri, now + PENDING_TTL_SECONDS, browser, link_user
        )
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = document["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    def take_redirect_uri(self, state: str) -> str | None:
        """Where the sign-in with this state said it would come back to."""
        pending = self._pending.get(state)
        return pending.redirect_uri if pending else None

    def browser_for(self, state: str) -> str:
        """The browser value the sign-in with this state started with."""
        pending = self._pending.get(state)
        return pending.browser if pending else ""

    def link_user_for(self, state: str) -> str:
        """The account this sign-in links an identity to, if it is a link."""
        pending = self._pending.get(state)
        return pending.link_user if pending else ""

    def issue_link_ticket(self, user_id: str) -> str:
        now = time.time()
        self._link_tickets = {t: v for t, v in self._link_tickets.items() if v[1] > now}
        ticket = secrets.token_urlsafe(24)
        self._link_tickets[ticket] = (user_id, now + LINK_TTL_SECONDS)
        return ticket

    def redeem_link_ticket(self, ticket: str) -> str | None:
        user_id, expires_at = self._link_tickets.pop(ticket, ("", 0.0))
        return user_id if expires_at > time.time() else None

    def store_link_result(self, user_id: str, identity: Identity) -> str:
        now = time.time()
        self._link_results = {c: v for c, v in self._link_results.items() if v[2] > now}
        code = secrets.token_urlsafe(24)
        self._link_results[code] = (user_id, identity, now + LINK_TTL_SECONDS)
        return code

    def redeem_link_result(self, code: str, user_id: str) -> Identity | None:
        """The identity behind ``code``, only for the account that started it."""
        owner, identity, expires_at = self._link_results.get(code, ("", None, 0.0))
        if identity is None or expires_at <= time.time() or owner != user_id:
            return None
        del self._link_results[code]
        return identity

    async def finish(self, settings: OidcSettings, code: str, state: str) -> Identity:
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at <= time.time():
            raise OidcError("This sign-in link expired; try again")
        document = await self.discover(settings.issuer)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "client_id": settings.client_id,
            "code_verifier": pending.verifier,
        }
        if settings.client_secret:
            form["client_secret"] = settings.client_secret
        try:
            async with self._http() as http:
                response = await http.post(
                    document["token_endpoint"],
                    data=form,
                    headers={"Accept": "application/json"},
                )
                tokens = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("Cannot reach the provider to finish signing in") from exc
        if response.status_code != 200 or not isinstance(tokens, dict):
            reason = (
                tokens.get("error_description") or tokens.get("error")
                if isinstance(tokens, dict)
                else None
            )
            raise OidcError(
                f"The provider refused the sign-in{f': {reason}' if reason else ''}"
            )
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("The provider did not return an ID token")
        claims = _claims(id_token)
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        issuer = str(claims.get("iss", "")).rstrip("/")
        # Google may name itself without the scheme in ID tokens.
        if settings.provider == "google" and issuer == "accounts.google.com":
            issuer = GOOGLE_ISSUER
        if issuer != settings.issuer.rstrip("/"):
            raise OidcError("The ID token comes from a different issuer")
        if settings.client_id not in audiences:
            raise OidcError("The ID token was issued for a different application")
        if claims.get("nonce") != pending.nonce:
            raise OidcError("The ID token does not belong to this sign-in")
        if (
            not isinstance(claims.get("exp"), int | float)
            or claims["exp"] < time.time() - 60
        ):
            raise OidcError("The ID token has expired")
        if not claims.get("sub"):
            raise OidcError("The ID token does not identify the person")
        email = claims.get("email")
        if isinstance(email, str) and claims.get("email_verified") is False:
            email = None
        if not isinstance(email, str) or "@" not in email:
            email = None
        if settings.allowed_domains:
            domain = email.rsplit("@", 1)[1].lower() if email else None
            if domain not in settings.allowed_domains:
                raise OidcError(
                    "Your account's email domain is not allowed to sign in here"
                )
        hint = next(
            (
                str(value)
                for value in (
                    claims.get("preferred_username"),
                    email.split("@")[0] if email else None,
                    claims.get("name"),
                )
                if value
            ),
            "user",
        )
        return Identity(f"{settings.issuer}|{claims['sub']}", email, hint)


def username_from(hint: str) -> str:
    """A valid username (3-32 of letters, digits, dots, dashes, underscores)."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", hint.split("@")[0]).strip(".-_")[:28]
    return name if len(name) >= 3 else f"user-{name}".rstrip("-")[:28] or "user"
