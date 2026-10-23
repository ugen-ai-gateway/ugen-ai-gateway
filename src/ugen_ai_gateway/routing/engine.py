from dataclasses import dataclass
from typing import Literal

from ugen_ai_gateway.analysis.schemas import RequestAnalysis
from ugen_ai_gateway.routing.config import (
    Condition,
    Locality,
    ModelConfig,
    ProviderConfig,
    RoutingConfig,
    Target,
)
from ugen_ai_gateway.services.errors import ModelNotFound, ProviderUnavailable

Selection = Literal["auto", "local", "cloud"]


@dataclass(frozen=True)
class Route:
    provider: ProviderConfig
    model: ModelConfig
    reason: str
    rule: str | None = None

    @property
    def locality(self) -> Locality:
        return self.provider.locality


def _matches(condition: Condition, analysis: RequestAnalysis, chars: int) -> bool:
    if (
        condition.complexity is not None
        and analysis.complexity not in condition.complexity
    ):
        return False
    if (
        condition.task_type is not None
        and analysis.task_type not in condition.task_type
    ):
        return False
    if (
        condition.contains_personal_data is not None
        and analysis.contains_personal_data != condition.contains_personal_data
    ):
        return False
    if condition.personal_data_types is not None and not set(
        condition.personal_data_types
    ).intersection(analysis.personal_data_types):
        return False
    if condition.min_chars is not None and chars < condition.min_chars:
        return False
    return condition.max_chars is None or chars <= condition.max_chars


class RuleEngine:
    """Pick the provider and model for a request.

    Order: an explicitly requested model, then an explicit local/cloud choice,
    then the first matching rule, then the local default. Finally the privacy
    rule is enforced: personal-data types listed in ``privacy.never_cloud``
    always keep the request local, whatever was chosen before.
    """

    def __init__(
        self, config: RoutingConfig, *, available: set[str] | None = None
    ) -> None:
        """``available`` names the providers that have a client; rules and
        defaults pointing anywhere else are skipped."""
        self.config = config
        self.available = (
            available
            if available is not None
            else {provider.name for provider in config.providers}
        )

    def default(self, locality: Locality) -> Route | None:
        target = (
            self.config.defaults.local
            if locality == "local"
            else self.config.defaults.cloud
        )
        if target is None or target.provider not in self.available:
            return None
        provider, model = self.config.resolve(target)
        return Route(provider, model, f"Default {locality} model.")

    def decide(
        self,
        analysis: RequestAnalysis,
        *,
        selection: Selection = "auto",
        model: str | None = None,
        chars: int = 0,
    ) -> Route:
        """Route a request. A ``model`` must be configured: an unknown name
        would run with no price, so its cost would escape budgets."""
        if model and self.config.find_model(model) is None:
            raise ModelNotFound(f"The model '{model}' does not exist")
        route = self._choose(analysis, selection, model, chars)
        blocked = set(self.config.privacy.never_cloud).intersection(
            analysis.personal_data_types
        )
        if route.locality == "cloud" and blocked:
            local = self.default("local")
            if local is None:
                raise ProviderUnavailable("Local provider is unavailable")
            return Route(
                local.provider,
                local.model,
                f"Contains {', '.join(sorted(blocked))}; policy keeps it local.",
                "privacy.never_cloud",
            )
        return route

    def _choose(
        self,
        analysis: RequestAnalysis,
        selection: Selection,
        model: str | None,
        chars: int,
    ) -> Route:
        found = self.config.find_model(model) if model else None
        if found and found[0].name in self.available:
            return Route(*found, f"Model {model} requested.")

        if selection != "auto":
            route = self.default(selection)
            if route is None:
                raise ProviderUnavailable(
                    f"{selection.capitalize()} provider is unavailable"
                )
            label = "Local" if selection == "local" else "Cloud"
            return Route(
                route.provider, route.model, f"{label} model selected manually."
            )

        for rule in self.config.rules:
            if rule.route.provider in self.available and _matches(
                rule.when, analysis, chars
            ):
                provider, resolved = self.config.resolve(rule.route)
                return Route(
                    provider, resolved, f"Matched rule '{rule.name}'.", rule.name
                )

        local = self.default("local")
        if local is None:
            raise ProviderUnavailable("Local provider is unavailable")
        return Route(
            local.provider,
            local.model,
            "Local model selected by the default routing policy.",
        )

    def models(self) -> list[str]:
        """Public ids (alias, else id) of every model on an available provider."""
        return [
            model.public_id
            for provider in self.config.providers
            if provider.name in self.available
            for model in provider.models
        ]

    def resolve(self, target: Target) -> Route:
        provider, model = self.config.resolve(target)
        return Route(provider, model, "")
