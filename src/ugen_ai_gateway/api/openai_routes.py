"""OpenAI-compatible endpoints, so existing OpenAI SDKs and tools can use the
gateway by changing only their base URL."""

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ugen_ai_gateway.api.access import authenticate
from ugen_ai_gateway.api.errors import http_error_for
from ugen_ai_gateway.api.prompt_routes import with_prompts
from ugen_ai_gateway.api.schemas import MAX_CHAT_MESSAGES, ChatRequest, ChatResponse
from ugen_ai_gateway.dependencies import get_gateway_service, get_usage_store
from ugen_ai_gateway.routing.engine import RuleEngine
from ugen_ai_gateway.services.gateway import GatewayEvent, GatewayService
from ugen_ai_gateway.services.usage import UsageStore

logger = logging.getLogger(__name__)

router = APIRouter()

GatewayDependency = Annotated[GatewayService, Depends(get_gateway_service)]

AUTO_MODEL = "ugen-auto"
LOCAL_MODEL = "ugen-local"
CLOUD_MODEL = "ugen-cloud"
_ALIASES: dict[str, Literal["auto", "local", "cloud"]] = {
    AUTO_MODEL: "auto",
    "auto": "auto",
    LOCAL_MODEL: "local",
    CLOUD_MODEL: "cloud",
}
# Generation parameters forwarded to the provider when the client sets them.
_PASSTHROUGH_OPTIONS = (
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)


class OpenAIChatRequest(BaseModel):
    """The subset of the OpenAI Chat Completions request the gateway supports."""

    model_config = ConfigDict(extra="ignore")

    model: str = AUTO_MODEL
    messages: list[ChatCompletionMessageParam] = Field(
        min_length=1, max_length=MAX_CHAT_MESSAGES
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stop: str | list[str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    seed: int | None = None
    n: int | None = None
    tools: list[Any] | None = None


class _RequestRejected(Exception):
    def __init__(self, status_code: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def error_response(
    status_code: int, message: str, *, code: str | None = None
) -> JSONResponse:
    error_type = (
        "invalid_request_error"
        if status_code < 500
        else "server_error"
        if status_code != 503
        else "service_unavailable"
    )
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type, "code": code}},
    )


def resolve_model(
    name: str, engine: RuleEngine
) -> tuple[Literal["auto", "local", "cloud"], str | None]:
    """Map the requested model id to a provider selection and a model.

    A configured model name pins that model (privacy rules still apply).
    """
    if name in _ALIASES:
        return _ALIASES[name], None
    found = engine.config.find_model(name)
    if found and found[0].name in engine.available:
        return "auto", name
    raise _RequestRejected(404, f"The model '{name}' does not exist", "model_not_found")


def to_gateway_request(
    body: OpenAIChatRequest, engine: RuleEngine
) -> tuple[ChatRequest, dict[str, Any]]:
    if body.n not in (None, 1):
        raise _RequestRejected(400, "Only n=1 is supported", "unsupported_parameter")
    if body.tools:
        raise _RequestRejected(
            400, "Tool calling is not supported yet", "unsupported_parameter"
        )
    selection, model = resolve_model(body.model, engine)
    payload: dict[str, Any] = {
        "provider_selection": selection,
        "model": model,
        "messages": body.messages,
    }
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    try:
        request = ChatRequest.model_validate(payload)
    except ValidationError as exc:
        message = exc.errors()[0].get("msg", "Invalid request")
        raise _RequestRejected(400, message) from exc
    options = {
        name: value
        for name in _PASSTHROUGH_OPTIONS
        if (value := getattr(body, name)) is not None
    }
    return request, options


def _usage(response: ChatResponse) -> dict[str, int] | None:
    usage = response.usage.generation if response.usage else None
    return usage.model_dump() if usage else None


def _ugen_details(response: ChatResponse) -> dict[str, Any]:
    return {
        "routing": response.routing.model_dump(mode="json"),
        "performance": (
            response.performance.model_dump(mode="json")
            if response.performance
            else None
        ),
    }


def _header_value(value: Any) -> str:
    """HTTP header values must be Latin-1; percent-encode anything else, such
    as a Chinese rule name, instead of failing the whole response."""
    text = str(value)
    return text if text.isascii() else quote(text, safe=" !#$&'()*+,-./:;=?@[]^_{|}~")


def _routing_headers(routing: dict[str, Any], model: str) -> dict[str, str]:
    headers = {
        "X-UGen-Provider": routing.get("provider", ""),
        "X-UGen-Model": model,
        "X-UGen-Route-Reason": routing.get("reason", ""),
        "X-UGen-Redacted-Values": routing.get("redacted_values", 0),
    }
    if routing.get("fallback_from"):
        headers["X-UGen-Fallback-From"] = routing["fallback_from"]
    return {name: _header_value(value) for name, value in headers.items()}


@router.get("/v1/models")
async def list_models(
    gateway: GatewayDependency,
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
    http_request: Request,
) -> dict[str, Any]:
    await authenticate(http_request, store)
    ids = [AUTO_MODEL, LOCAL_MODEL]
    if gateway.engine.default("cloud") is not None:
        ids.append(CLOUD_MODEL)
    ids.extend(model for model in gateway.engine.models() if model not in ids)
    if gateway.embedding_model:
        ids.append(EMBEDDING_MODEL)
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": "ugen"}
            for model_id in ids
        ],
    }


@router.post("/v1/chat/completions", response_model=None)
async def create_chat_completion(
    body: OpenAIChatRequest,
    gateway: GatewayDependency,
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
    http_request: Request,
) -> JSONResponse | StreamingResponse:
    principal = await authenticate(http_request, store)
    try:
        request, options = to_gateway_request(body, gateway.engine)
    except _RequestRejected as exc:
        return error_response(exc.status_code, str(exc), code=exc.code)
    request = await with_prompts(request, store, principal)

    created = int(time.time())

    if not body.stream:
        try:
            response = await gateway.chat(request, options=options, principal=principal)
        except Exception as exc:  # noqa: BLE001 - every failure maps to an API error
            return error_response(*http_error_for(exc))
        return JSONResponse(
            {
                "id": f"chatcmpl-ugen-{response.id}",
                "object": "chat.completion",
                "created": created,
                "model": response.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": response.content},
                        "finish_reason": response.finish_reason or "stop",
                    }
                ],
                "usage": _usage(response),
                "ugen": _ugen_details(response),
            },
            headers=_routing_headers(
                response.routing.model_dump(mode="json"), response.model
            ),
        )

    # Wait for the first generated text before answering, so failures up to
    # that point (including a fallback that also fails) still get a proper HTTP
    # status, and the headers name the provider that actually answers.
    events = gateway.events(request, stream=True, options=options, principal=principal)
    routing: dict[str, Any] = {}
    model = ""
    completion_id = "chatcmpl-ugen"
    first: GatewayEvent | None = None
    try:
        async for event in events:
            if event.type == "analysis_started":
                completion_id = f"chatcmpl-ugen-{event.data['request_id']}"
            elif event.type == "routed":
                routing = dict(event.data)
            elif event.type == "fallback":
                routing["provider"] = event.data["to"]
                routing["fallback_from"] = event.data["from"]
            elif event.type == "generation_started":
                model = str(event.data.get("model", ""))
            elif event.type == "delta" or event.response is not None:
                first = event
                break
    except Exception as exc:  # noqa: BLE001 - every failure maps to an API error
        await events.aclose()
        return error_response(*http_error_for(exc))

    include_usage = bool((body.stream_options or {}).get("include_usage"))
    return StreamingResponse(
        _sse(_replay(first, events), completion_id, created, model, include_usage),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **_routing_headers(routing, model),
        },
    )


async def _replay(
    first: GatewayEvent | None, events: AsyncIterator[GatewayEvent]
) -> AsyncIterator[GatewayEvent]:
    try:
        if first is not None:
            yield first
        async for event in events:
            yield event
    finally:
        await events.aclose()  # type: ignore[attr-defined]


async def _sse(
    events: AsyncIterator[GatewayEvent],
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
) -> AsyncIterator[str]:
    def chunk(
        delta: dict[str, Any],
        finish_reason: str | None = None,
        **extra: Any,
    ) -> str:
        body = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            **extra,
        }
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    try:
        async for event in events:
            if event.type == "delta":
                yield chunk({"content": event.data["content"]})
            elif event.response is not None:
                response = event.response
                yield chunk(
                    {},
                    response.finish_reason or "stop",
                    ugen=_ugen_details(response),
                )
                if include_usage:
                    usage = _usage(response)
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [],
                                "usage": usage,
                            }
                        )
                        + "\n\n"
                    )
    except Exception as exc:  # noqa: BLE001 - every failure maps to an API error
        status_code, message = http_error_for(exc)
        error = {"message": message, "type": "server_error", "code": status_code}
        yield f"data: {json.dumps({'error': error})}\n\n"
    finally:
        await events.aclose()  # type: ignore[attr-defined]
    yield "data: [DONE]\n\n"


EMBEDDING_MODEL = "ugen-embed"


class OpenAIEmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str = EMBEDDING_MODEL
    input: str | list[str] | list[int] | list[list[int]]
    encoding_format: Literal["float", "base64"] | None = None
    dimensions: int | None = Field(default=None, ge=1)


@router.post("/v1/embeddings", response_model=None)
async def create_embeddings(
    body: OpenAIEmbeddingRequest,
    gateway: GatewayDependency,
    store: Annotated[UsageStore | None, Depends(get_usage_store)],
    http_request: Request,
) -> JSONResponse:
    """OpenAI-compatible embeddings, always computed by the local model."""
    principal = await authenticate(http_request, store)
    if body.model not in (EMBEDDING_MODEL, gateway.embedding_model):
        return error_response(
            404,
            f"The model '{body.model}' does not exist; use '{EMBEDDING_MODEL}'",
            code="model_not_found",
        )
    texts = [body.input] if isinstance(body.input, str) else body.input
    if sum(len(text) for text in texts if isinstance(text, str)) > 200_000:
        return error_response(413, "Input exceeds 200,000 characters", code=None)
    options = body.model_dump(
        include={"encoding_format", "dimensions"}, exclude_none=True
    )
    try:
        result = await gateway.embed(body.input, options=options, principal=principal)
    except Exception as exc:  # noqa: BLE001 - every failure maps to an API error
        return error_response(*http_error_for(exc))
    payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    return JSONResponse(payload)
