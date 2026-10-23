from typing import Literal

from pydantic import BaseModel

from ugen_ai_gateway.analysis.schemas import RequestAnalysis

ProviderName = Literal["local", "cloud"]


class RoutingDecision(BaseModel):
    """Model routing decision and its rationale."""

    provider: ProviderName
    reason: str
    requires_redaction: bool = False


class RoutingPolicy:
    """Apply privacy-first rules to model routing."""

    def __init__(self, *, cloud_enabled: bool = True) -> None:
        self._cloud_enabled = cloud_enabled

    @property
    def cloud_enabled(self) -> bool:
        return self._cloud_enabled

    def decide(self, analysis: RequestAnalysis) -> RoutingDecision:
        if analysis.complexity in {"moderate", "complex"} and self._cloud_enabled:
            return RoutingDecision(
                provider="cloud",
                reason="Moderate or complex task; cloud model is enabled.",
            )

        return RoutingDecision(
            provider="local",
            reason="Local model selected by the default routing policy.",
        )
