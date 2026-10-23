"""Account administration and gateway-wide settings, for administrators."""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from ugen_ai_gateway.api.access import audit_admin, check_admin_access, identify
from ugen_ai_gateway.api.auth_routes import (
    GATEWAY_SETTING,
    callback_url,
    get_oidc_client,
    oidc_settings,
    registration_allowed,
)
from ugen_ai_gateway.dependencies import get_secret_box, get_usage_store
from ugen_ai_gateway.services.oidc import OIDC_SETTING, OidcError, OidcSettings
from ugen_ai_gateway.services.usage import LastAdministrator, UsageStore

router = APIRouter(prefix="/v1/ugen")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]


def _require_store(store: UsageStore | None) -> UsageStore:
    if store is None:
        raise HTTPException(
            status_code=503, detail="Accounts need a database; set USAGE_DB_PATH"
        )
    return store


async def _admin(request: Request, store: UsageStore | None) -> UsageStore:
    await check_admin_access(request, store)
    return _require_store(store)


async def _not_self(request: Request, store: UsageStore, user_id: str) -> None:
    principal = await identify(request, store)
    if principal is not None and principal.user_id == user_id:
        raise HTTPException(
            status_code=400, detail="Use the Account page to change your own account"
        )


@router.get("/users")
async def list_users(store: UsageStoreDependency, request: Request) -> dict[str, Any]:
    """Every account with this month's usage, highest cloud cost first."""
    store = await _admin(request, store)
    return {"data": await store.list_users()}


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    disabled: bool | None = None
    # Monthly cloud budget in USD; null removes the limit.
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=1_000_000)


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, body: UserUpdate, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    store = await _admin(request, store)
    changes = body.model_dump(exclude_unset=True)
    if "is_admin" in changes or "disabled" in changes:
        await _not_self(request, store, user_id)
    try:
        user = await store.update_user(user_id, changes)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown account") from exc
    except LastAdministrator as exc:
        raise HTTPException(
            status_code=409,
            detail="The gateway needs at least one active administrator",
        ) from exc
    await audit_admin(request, store, "account_updated", user_id=user_id, **changes)
    return user


@router.post("/users/{user_id}/reset-password")
async def reset_password(
    user_id: str, store: UsageStoreDependency, request: Request
) -> dict[str, str]:
    """Set a random password and sign the account out everywhere. The new
    password is shown once, for the administrator to pass on."""
    store = await _admin(request, store)
    await _not_self(request, store, user_id)
    if await store.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="Unknown account")
    password = secrets.token_urlsafe(12)
    await store.change_password(user_id, password)
    await audit_admin(request, store, "password_reset", user_id=user_id)
    return {"password": password}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str, store: UsageStoreDependency, request: Request
) -> Response:
    store = await _admin(request, store)
    await _not_self(request, store, user_id)
    if not await store.delete_user(user_id):
        raise HTTPException(status_code=404, detail="Unknown account")
    await audit_admin(request, store, "account_deleted", user_id=user_id)
    return Response(status_code=204)


class GatewaySettings(BaseModel):
    allow_registration: bool


@router.get("/settings")
async def get_gateway_settings(
    store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    store = await _admin(request, store)
    return {"allow_registration": registration_allowed(store)}


@router.put("/settings")
async def save_gateway_settings(
    body: GatewaySettings, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    store = await _admin(request, store)
    saved = store.load_setting(GATEWAY_SETTING) or {}
    await store.save_setting(GATEWAY_SETTING, {**saved, **body.model_dump()})
    await audit_admin(request, store, "settings_updated", **body.model_dump())
    return body.model_dump()


def _sso_view(request: Request, settings: OidcSettings) -> dict[str, Any]:
    return settings.public_view() | {
        "callback_url": callback_url(request, settings),
        "provider": settings.provider,
    }


@router.get("/sso")
async def get_sso(store: UsageStoreDependency, request: Request) -> dict[str, Any]:
    store = await _admin(request, store)
    return _sso_view(request, oidc_settings(store))


class SsoUpdate(BaseModel):
    enabled: bool | None = None
    issuer: str | None = None
    client_id: str | None = None
    # Empty keeps the stored secret; use clear_client_secret to remove it.
    client_secret: str | None = None
    clear_client_secret: bool = False
    button_label: str | None = None
    auto_create: bool | None = None
    allowed_domains: list[str] | None = None
    redirect_uri: str | None = None


def _merged(store: UsageStore, body: SsoUpdate) -> OidcSettings:
    current = oidc_settings(store)
    changes = body.model_dump(exclude_unset=True, exclude={"clear_client_secret"})
    if not changes.get("client_secret"):
        changes["client_secret"] = (
            "" if body.clear_client_secret else current.client_secret
        )
    try:
        settings = OidcSettings.model_validate(current.model_dump() | changes)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()[0]["msg"]) from exc
    if settings.enabled and not (settings.issuer and settings.client_id):
        raise HTTPException(
            status_code=422, detail="Single sign-on needs an issuer URL and a client ID"
        )
    return settings


@router.put("/sso")
async def save_sso(
    body: SsoUpdate, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    store = await _admin(request, store)
    settings = _merged(store, body)
    await store.save_setting(OIDC_SETTING, settings.sealed(get_secret_box()))
    await audit_admin(
        request,
        store,
        "sso_updated",
        enabled=settings.enabled,
        issuer=settings.issuer,
        auto_create=settings.auto_create,
    )
    return _sso_view(request, settings)


@router.post("/sso/test")
async def test_sso(
    body: SsoUpdate, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Read the provider's discovery document for the issuer on the form."""
    store = await _admin(request, store)
    settings = _merged(store, body.model_copy(update={"enabled": False}))
    if not settings.issuer:
        raise HTTPException(status_code=422, detail="Enter the issuer URL")
    try:
        document = await get_oidc_client().discover(settings.issuer, fresh=True)
    except OidcError as exc:
        return {"ok": False, "message": str(exc)}
    return {
        "ok": True,
        "message": "Found the provider",
        "authorization_endpoint": document["authorization_endpoint"],
    }
