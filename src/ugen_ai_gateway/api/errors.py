import logging

from ugen_ai_gateway.services.errors import (
    GatewayConfigurationError,
    InvalidChatRequest,
    ModelNotFound,
    PolicyViolation,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
    RequestTooLarge,
)

logger = logging.getLogger(__name__)


def http_error_for(exc: Exception) -> tuple[int, str]:
    """Map a gateway exception to an HTTP status and a client-safe message."""
    if isinstance(exc, PolicyViolation):
        return 400, str(exc)
    if isinstance(exc, ModelNotFound):
        return 404, str(exc)
    if isinstance(exc, RequestTooLarge):
        return 413, str(exc)
    if isinstance(exc, InvalidChatRequest):
        return 422, str(exc)
    if isinstance(exc, GatewayConfigurationError | ProviderUnavailable):
        return 503, str(exc)
    if isinstance(exc, ProviderTimeout | TimeoutError):
        return 504, "Model provider timed out"
    if isinstance(exc, ProviderRequestError):
        return 502, "Model provider request failed"
    if isinstance(exc, ValueError):
        return 503, str(exc)
    logger.exception("Unhandled chat completion error", exc_info=exc)
    return 502, "Model provider request failed"
