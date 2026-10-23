import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ugen_ai_gateway.api.auth_routes import router as auth_router
from ugen_ai_gateway.api.conversation_routes import router as conversation_router
from ugen_ai_gateway.api.model_routes import router as model_router
from ugen_ai_gateway.api.openai_routes import router as openai_router
from ugen_ai_gateway.api.privacy_routes import router as privacy_router
from ugen_ai_gateway.api.prompt_routes import router as prompt_router
from ugen_ai_gateway.api.routes import router
from ugen_ai_gateway.api.sharing_routes import router as sharing_router
from ugen_ai_gateway.api.user_routes import router as user_router
from ugen_ai_gateway.config import get_settings
from ugen_ai_gateway.dependencies import get_gateway_service

API_PREFIX = "/api"
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def report_configuration(app: FastAPI) -> AsyncIterator[None]:
    """Name a broken model configuration at startup instead of on the first
    request; the server still starts so /health and /ready can report it."""
    try:
        get_gateway_service()
    except Exception:
        logger.exception("The model configuration is invalid; requests will fail")
    yield


class StripApiPrefix:
    """Serve the API under /api as well, the path the dashboard calls, so the
    same build works behind the Vite dev proxy and from this server."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope["path"]
            if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
                # Keep the prefix so absolute URLs (the sign-on callback) use it.
                scope = {
                    **scope,
                    "path": path[len(API_PREFIX) :] or "/",
                    "ugen.api_prefix": API_PREFIX,
                }
        await self.app(scope, receive, send)


SECURITY_HEADERS = [
    (
        b"content-security-policy",
        (
            b"default-src 'self'; script-src 'self'; "
            b"style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
            b"img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
            b"base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        ),
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]
# The interactive API docs (/docs, /redoc) load Swagger UI and ReDoc from
# cdn.jsdelivr.net with an inline start-up script; only those pages allow it.
DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")
DOCS_CSP = (
    b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    b"font-src 'self' https://fonts.gstatic.com data:; "
    b"img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
    b"worker-src 'self' blob:; connect-src 'self'; object-src 'none'; "
    b"base-uri 'self'; frame-ancestors 'none'"
)


class SecurityHeaders:
    """Limit what a page can load or be embedded in, so injected markup in a
    reply could not run scripts or exfiltrate the session token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"].removeprefix(API_PREFIX)
        headers = SECURITY_HEADERS
        if path in DOCS_PATHS:
            headers = [
                (b"content-security-policy", DOCS_CSP),
                *(h for h in SECURITY_HEADERS if h[0] != b"content-security-policy"),
            ]

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name for name, _ in message.get("headers", [])}
                message["headers"] = [
                    *message.get("headers", []),
                    *(header for header in headers if header[0] not in present),
                ]
            await send(message)

        await self.app(scope, receive, with_headers)


def _static_dir() -> Path | None:
    configured = get_settings().static_dir
    path = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    )
    return path if (path / "index.html").is_file() else None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=report_configuration)
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(model_router)
    app.include_router(user_router)
    app.include_router(privacy_router)
    app.include_router(prompt_router)
    app.include_router(conversation_router)
    app.include_router(sharing_router)
    app.include_router(openai_router)
    if static := _static_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="dashboard")
    app.add_middleware(StripApiPrefix)
    app.add_middleware(SecurityHeaders)
    return app


app = create_app()


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=7632)
