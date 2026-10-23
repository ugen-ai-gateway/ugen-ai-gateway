"""Model connections, managed by administrators from the dashboard."""

import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from ugen_ai_gateway.api.access import audit_admin, check_admin_access
from ugen_ai_gateway.config import get_settings
from ugen_ai_gateway.dependencies import (
    MODELS_SETTING,
    current_models,
    get_secret_box,
    get_usage_store,
    models_changed,
)
from ugen_ai_gateway.providers.openai_client import create_openai_client
from ugen_ai_gateway.services.model_config import (
    RouteRule,
    StoredModels,
    public_view,
    sealed,
)
from ugen_ai_gateway.services.usage import UsageStore

router = APIRouter(prefix="/v1/ugen/models")

UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]
_PROBE_TIMEOUT = 30.0


def _check_url(value: str) -> str:
    value = value.strip()
    if value and not value.startswith(("http://", "https://")):
        raise ValueError("The base URL must start with http:// or https://")
    return value


class _Connection(BaseModel):
    base_url: str = Field(default="", max_length=500)
    # None keeps the saved key; "" removes it.
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=200)
    alias: str = Field(default="", max_length=100)

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return _check_url(value)


class LocalUpdate(_Connection):
    analysis_model: str = Field(default="", max_length=200)
    embedding_model: str = Field(default="", max_length=200)


class CloudUpdate(_Connection):
    input_cost_per_million: float = Field(default=0, ge=0, le=10_000)
    output_cost_per_million: float = Field(default=0, ge=0, le=10_000)


class ExtraUpdate(CloudUpdate):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    name: str = Field(min_length=1, max_length=60)
    locality: Literal["local", "cloud"] = "cloud"


class ModelsUpdate(BaseModel):
    local: LocalUpdate = Field(default_factory=LocalUpdate)
    cloud_enabled: bool = False
    cloud: CloudUpdate = Field(default_factory=CloudUpdate)
    analysis_mode: Literal["hybrid", "llm"] = "hybrid"
    cloud_from: Literal["moderate", "complex"] = "moderate"
    extra: list[ExtraUpdate] = Field(default_factory=list, max_length=20)
    rules: list[RouteRule] = Field(default_factory=list, max_length=50)


class Probe(BaseModel):
    """A connection to try: ``target`` names whose saved key to use when
    ``api_key`` is not given."""

    target: str = Field(pattern=r"^(local|cloud|extra:[a-z0-9][a-z0-9-]{0,31})$")
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    model: str = Field(default="", max_length=200)

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        return _check_url(value)


def _require_store(store: UsageStore | None) -> UsageStore:
    if store is None:
        raise HTTPException(
            status_code=503, detail="Saving models needs a database; set USAGE_DB_PATH"
        )
    return store


def _strip(model: _Connection) -> dict[str, Any]:
    """The fields the request set, trimmed; the API key is handled separately."""
    data = model.model_dump(exclude={"api_key"}, exclude_unset=True)
    return {k: v.strip() if isinstance(v, str) else v for k, v in data.items()}


@router.get("")
async def get_models(store: UsageStoreDependency, request: Request) -> dict[str, Any]:
    await check_admin_access(request, store)
    models, source = current_models(store)
    return {
        "source": "routing_file" if get_settings().routing_config else source,
        "config": public_view(models),
    }


@router.put("")
async def save_models(
    body: ModelsUpdate, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    await check_admin_access(request, store)
    store = _require_store(store)
    if get_settings().routing_config:
        raise HTTPException(
            status_code=409,
            detail="Models come from the ROUTING_CONFIG file; edit that file instead",
        )
    saved, _ = current_models(store)

    local = saved.local.model_copy(update=_strip(body.local))
    if body.local.api_key is not None:
        local.api_key = body.local.api_key.strip()
    cloud = saved.cloud.model_copy(update=_strip(body.cloud))
    if body.cloud.api_key is not None:
        cloud.api_key = body.cloud.api_key.strip()

    saved_extra = {model.id: model for model in saved.extra}
    extra = []
    for update in body.extra:
        data = _strip(update)
        previous = saved_extra.get(update.id)
        key = (
            update.api_key.strip()
            if update.api_key is not None
            else (previous.api_key if previous else "")
        )
        extra.append({**data, "api_key": key})

    try:
        models = StoredModels(
            local=local,
            cloud_enabled=body.cloud_enabled,
            cloud=cloud,
            analysis_mode=body.analysis_mode,
            cloud_from=body.cloud_from,
            extra=extra,
            rules=body.rules,
        )
    except ValidationError as exc:
        message = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise HTTPException(status_code=422, detail=message) from exc
    for model in models.extra:
        if not model.usable():
            raise HTTPException(
                status_code=422,
                detail=f"'{model.name}' needs a base URL and a model"
                + (" and an API key" if model.locality == "cloud" else ""),
            )
    if models.cloud_enabled and not models.active_cloud():
        raise HTTPException(
            status_code=422,
            detail="The cloud needs a base URL, an API key and a model before it can be on",
        )

    await store.save_setting(MODELS_SETTING, sealed(models, get_secret_box()))
    models_changed()
    # What changed, never the keys.
    await audit_admin(
        request,
        store,
        "models_updated",
        local_base_url=models.local.base_url,
        local_model=models.local.model,
        cloud_enabled=models.cloud_enabled,
        cloud_base_url=models.cloud.base_url,
        cloud_model=models.cloud.model,
        analysis_mode=models.analysis_mode,
        cloud_from=models.cloud_from,
        extra=[
            {"name": m.name, "locality": m.locality, "model": m.model}
            for m in models.extra
        ],
        rules=[{"name": r.name, "target": r.target} for r in models.rules],
    )
    return {"source": "database", "config": public_view(models)}


def _probe_client(probe: Probe, store: UsageStore | None):
    saved, _ = current_models(store)
    if probe.target == "local":
        saved_url, saved_key, fallback = (
            saved.local.base_url,
            saved.local.api_key,
            "local",
        )
    elif probe.target == "cloud":
        saved_url, saved_key, fallback = saved.cloud.base_url, saved.cloud.api_key, ""
    else:
        match = next(
            (m for m in saved.extra if m.id == probe.target.removeprefix("extra:")),
            None,
        )
        saved_url = match.base_url if match else ""
        saved_key = match.api_key if match else ""
        fallback = "local" if match and match.locality == "local" else ""
    # The saved key only goes to the saved address, so a changed URL cannot
    # be used to send it elsewhere; a new address needs the key typed in.
    if _same_url(probe.base_url, saved_url):
        key = probe.api_key.strip() if probe.api_key is not None else saved_key
    else:
        key = (probe.api_key or "").strip()
    if not key and not fallback:
        detail = (
            "The saved key is only sent to the saved base URL; enter the key "
            "to try this address"
            if saved_key and not _same_url(probe.base_url, saved_url)
            else "Enter the API key first"
        )
        raise HTTPException(status_code=422, detail=detail)
    if not probe.base_url:
        raise HTTPException(status_code=422, detail="Enter the base URL first")
    return create_openai_client(
        api_key=key or fallback,
        base_url=probe.base_url,
        timeout=_PROBE_TIMEOUT,
        max_retries=0,
    )


def _same_url(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.strip().rstrip("/") == b.strip().rstrip("/")


def _short(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message if len(message) <= 300 else message[:300] + "…"


@router.post("/test")
async def test_connection(
    probe: Probe, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """Send a tiny chat request with these settings, without saving them."""
    await check_admin_access(request, store)
    if not probe.model:
        raise HTTPException(status_code=422, detail="Choose a model first")
    client = _probe_client(probe, store)
    started = time.perf_counter()
    try:
        await client.chat.completions.create(
            model=probe.model,
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=16,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the administrator
        return {"ok": False, "error": _short(exc)}
    finally:
        await client.close()
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}


@router.post("/list")
async def list_models(
    probe: Probe, store: UsageStoreDependency, request: Request
) -> dict[str, Any]:
    """The models the provider offers, to pick from."""
    await check_admin_access(request, store)
    client = _probe_client(probe, store)
    try:
        ids = sorted({model.id async for model in client.models.list()})
    except Exception as exc:  # noqa: BLE001 - reported to the administrator
        return {"models": [], "error": _short(exc)}
    finally:
        await client.close()
    # Gemini lists models as "models/<id>"; requests use the bare id.
    return {"models": [model_id.removeprefix("models/") for model_id in ids]}
