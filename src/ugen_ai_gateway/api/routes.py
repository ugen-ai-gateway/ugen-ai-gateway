"""Gateway-specific endpoints used by the dashboard: health, config, and chat
with routing details and stage-by-stage progress."""

import csv
import io
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ugen_ai_gateway.api.access import audit_admin, authenticate, check_admin_access
from ugen_ai_gateway.api.errors import http_error_for
from ugen_ai_gateway.api.prompt_routes import with_prompts
from ugen_ai_gateway.api.schemas import ChatRequest, ChatResponse
from ugen_ai_gateway.dependencies import get_gateway_service, get_usage_store
from ugen_ai_gateway.services.gateway import GatewayService
from ugen_ai_gateway.services.usage import UsageStore

logger = logging.getLogger(__name__)

router = APIRouter()

GatewayDependency = Annotated[
    GatewayService,
    Depends(get_gateway_service),
]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request, response: Response) -> dict[str, str]:
    """Whether requests can be served, with the reason when they cannot."""
    factory = request.app.dependency_overrides.get(
        get_gateway_service, get_gateway_service
    )
    try:
        gateway = factory()
    except Exception:
        logger.exception("The gateway configuration is invalid")
        response.status_code = 503
        return {
            "status": "not_ready",
            "reason": "The configuration in .env is invalid; see the server logs",
        }
    if problem := await gateway.readiness_problem():
        response.status_code = 503
        return {"status": "not_ready", "reason": problem}
    return {"status": "ready"}


@router.get("/v1/ugen/config")
async def gateway_config(
    gateway: GatewayDependency,
    http_request: Request,
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
) -> dict[str, Any]:
    """Non-secret configuration for the dashboard, for signed-in callers."""
    await authenticate(http_request, store)
    engine = gateway.engine
    local = engine.default("local")
    cloud = engine.default("cloud")
    return {
        "configured": gateway.configured,
        "local_model": local.model.public_id if local else "",
        "cloud_model": cloud.model.public_id if cloud else "",
        "cloud_enabled": cloud is not None,
        # Redaction is always on; kept for clients that read these fields.
        "redact_local": True,
        "restore_redacted": True,
        "providers": [
            {
                "name": provider.name,
                "locality": provider.locality,
                "available": provider.name in engine.available,
                "models": [model.public_id for model in provider.models],
            }
            for provider in engine.config.providers
        ],
        "rules": [rule.name for rule in engine.config.rules],
        "never_cloud": engine.config.privacy.never_cloud,
    }


@router.get("/v1/ugen/providers")
async def provider_status(
    gateway: GatewayDependency,
    http_request: Request,
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
) -> dict[str, str]:
    """Circuit-breaker state per provider: closed, open, or half_open."""
    await authenticate(http_request, store)
    return gateway.provider_status()


UsageStoreDependency = Annotated[UsageStore | None, Depends(get_usage_store)]


def _require_store(store: UsageStore | None) -> UsageStore:
    if store is None:
        raise HTTPException(status_code=404, detail="Usage tracking is disabled")
    return store


@router.get("/v1/ugen/stats")
async def usage_stats(
    store: UsageStoreDependency,
    http_request: Request,
    days: Annotated[int, Query(ge=1, le=366)] = 7,
    tz_offset_minutes: Annotated[int, Query(ge=-840, le=840)] = 0,
) -> dict[str, Any]:
    """Totals, savings and daily counts. Daily buckets use the given UTC offset.

    An API key sees only its own requests; the master key sees everything."""
    principal = await authenticate(http_request, store)
    return await _require_store(store).stats(
        days=days, tz_offset_minutes=tz_offset_minutes, **principal.usage_scope
    )


@router.get("/v1/ugen/requests")
async def recent_requests(
    store: UsageStoreDependency,
    http_request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: float | None = None,
) -> dict[str, Any]:
    """Recent request metadata, newest first; pass ``before`` to page back."""
    principal = await authenticate(http_request, store)
    items = await _require_store(store).recent(
        limit=limit, before=before, **principal.usage_scope
    )
    return {"data": items}


class FeedbackRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=64)
    rating: Literal[-1, 1]


@router.post("/v1/ugen/feedback", status_code=204)
async def submit_feedback(
    body: FeedbackRequest, store: UsageStoreDependency, http_request: Request
) -> Response:
    principal = await authenticate(http_request, store)
    if not await _require_store(store).set_feedback(
        body.request_id, body.rating, **principal.usage_scope
    ):
        raise HTTPException(status_code=404, detail="Unknown request id")
    return Response(status_code=204)


@router.get("/v1/ugen/audit", response_model=None)
async def audit_log(
    store: UsageStoreDependency,
    http_request: Request,
    since: float | None = None,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
    format: Literal["json", "csv"] = "json",
) -> dict[str, Any] | Response:
    """Export the hash-chained audit log (metadata only) as JSON or CSV."""
    await check_admin_access(http_request, store)
    entries = await _require_store(store).audit_entries(since=since, limit=limit)
    if format == "json":
        return {"data": entries}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = [
        "seq",
        "created_at",
        "request_id",
        "event",
        "detail",
        "prev_hash",
        "hash",
    ]
    writer.writerow(columns)
    for entry in entries:
        writer.writerow(
            [
                json.dumps(entry[column], ensure_ascii=False, sort_keys=True)
                if column == "detail"
                else entry[column]
                for column in columns
            ]
        )
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ugen-audit.csv"'},
    )


@router.get("/v1/ugen/audit/verify")
async def verify_audit_log(
    store: UsageStoreDependency, http_request: Request
) -> dict[str, Any]:
    """Recompute the hash chain; ``ok`` is false from the first altered entry."""
    await check_admin_access(http_request, store)
    return await _require_store(store).verify_audit()


class KeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    allow_cloud: bool = True
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=100_000)


@router.post("/v1/ugen/keys", status_code=201)
async def create_api_key(
    body: KeyRequest, store: UsageStoreDependency, http_request: Request
) -> dict[str, Any]:
    """Create an API key. The returned ``key`` is shown only this once."""
    await check_admin_access(http_request, store)
    view, secret = await _require_store(store).create_key(**body.model_dump())
    await audit_admin(
        http_request,
        store,
        "api_key_created",
        key_id=view["id"],
        allow_cloud=view["allow_cloud"],
        monthly_budget_usd=view["monthly_budget_usd"],
        rate_limit_per_minute=view["rate_limit_per_minute"],
    )
    return {**view, "key": secret}


@router.get("/v1/ugen/keys")
async def list_api_keys(
    store: UsageStoreDependency, http_request: Request
) -> dict[str, Any]:
    """All keys with this month's request count and cost; secrets are never shown."""
    await check_admin_access(http_request, store)
    return {"data": await _require_store(store).list_keys()}


@router.delete("/v1/ugen/keys/{key_id}", status_code=204)
async def revoke_api_key(
    key_id: str, store: UsageStoreDependency, http_request: Request
) -> Response:
    await check_admin_access(http_request, store)
    if not await _require_store(store).revoke_key(key_id):
        raise HTTPException(status_code=404, detail="Unknown or already revoked key")
    await audit_admin(http_request, store, "api_key_revoked", key_id=key_id)
    return Response(status_code=204)


@router.get("/v1/ugen/whoami")
async def whoami(store: UsageStoreDependency, http_request: Request) -> dict[str, Any]:
    """What the presented credential is allowed to do."""
    principal = await authenticate(http_request, store)
    spend = (
        await store.month_spend(principal.api_key_id)
        if store is not None and principal.api_key_id
        else None
    )
    return {
        "api_key_id": principal.api_key_id,
        "allow_cloud": principal.allow_cloud,
        "monthly_budget_usd": principal.monthly_budget_usd,
        "month_cost_usd": spend,
        "rate_limit_per_minute": principal.rate_limit_per_minute,
    }


class MemoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=32_000)


class MemoryRequest(BaseModel):
    previous: str = Field(default="", max_length=8_000)
    messages: list[MemoryMessage] = Field(min_length=1, max_length=64)


@router.post("/v1/ugen/memory")
async def update_memory(
    body: MemoryRequest,
    gateway: GatewayDependency,
    store: UsageStoreDependency,
    http_request: Request,
) -> dict[str, str]:
    """Fold older messages into a conversation's memory with the local model.

    The dashboard keeps the memory with the conversation and sends it as a
    system message, where it is checked and redacted like any other text."""
    await authenticate(http_request, store)
    if sum(len(message.content) for message in body.messages) > 60_000:
        raise HTTPException(
            status_code=413, detail="Too much text to summarize at once"
        )
    try:
        memory = await gateway.summarize_memory(
            body.previous, [message.model_dump() for message in body.messages]
        )
    except Exception as exc:
        status_code, detail = http_error_for(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {"memory": memory}


@router.post("/v1/ugen/chat", response_model=ChatResponse)
async def create_chat(
    request: ChatRequest,
    gateway: GatewayDependency,
    store: UsageStoreDependency,
    http_request: Request,
) -> ChatResponse:
    principal = await authenticate(http_request, store)
    request = await with_prompts(request, store, principal)
    try:
        return await gateway.chat(request, principal=principal)
    except Exception as exc:
        status_code, detail = http_error_for(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.post("/v1/ugen/chat/stream")
async def create_chat_stream(
    request: ChatRequest,
    gateway: GatewayDependency,
    store: UsageStoreDependency,
    http_request: Request,
) -> StreamingResponse:
    principal = await authenticate(http_request, store)
    request = await with_prompts(request, store, principal)

    async def events():
        try:
            async for event in gateway.events(
                request, stream=True, principal=principal
            ):
                yield json.dumps(event.to_json(), ensure_ascii=False) + "\n"
        except Exception as exc:
            logger.exception("Streaming chat completion failed")
            _, detail = http_error_for(exc)
            yield (
                json.dumps({"type": "error", "detail": detail}, ensure_ascii=False)
                + "\n"
            )

    return StreamingResponse(
        events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
