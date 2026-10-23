class GatewayConfigurationError(ValueError):
    """Raised when a required provider or model setting is missing."""


class InvalidChatRequest(ValueError):
    """Raised when a chat request cannot be processed safely."""


class RequestTooLarge(InvalidChatRequest):
    """Raised when a chat request exceeds configured input limits."""


class PrivacyRedactionError(InvalidChatRequest):
    """Raised when detected personal data cannot be safely redacted."""


class ModelNotFound(InvalidChatRequest):
    """Raised when a request names a model that is not configured."""


class PolicyViolation(InvalidChatRequest):
    """Raised when a guard blocks a request (for example prompt injection)."""


class ProviderUnavailable(RuntimeError):
    """Raised when the selected provider is not configured or unavailable."""


class ProviderTimeout(TimeoutError):
    """Raised when an upstream model request times out."""


class ProviderRequestError(RuntimeError):
    """Raised when an upstream model request fails or returns invalid data."""


class ProviderOutage(ProviderRequestError):
    """Raised when a provider is unreachable, overloaded, or failing (5xx/429)."""
