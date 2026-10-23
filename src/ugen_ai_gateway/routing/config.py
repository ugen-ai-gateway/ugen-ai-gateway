"""Routing configuration: providers, models, privacy rules and routing rules.

Loaded from the YAML file named by ROUTING_CONFIG, or built from the
LOCAL_* / CLOUD_* settings when no file is given.
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from ugen_ai_gateway.config import Settings

Locality = Literal["local", "cloud"]
Complexity = Literal["simple", "moderate", "complex"]


class ModelConfig(BaseModel):
    id: str = Field(min_length=1)
    # Public name shown to clients instead of ``id``; also accepted in requests.
    alias: str | None = Field(default=None, min_length=1)
    # USD per million tokens; used for cost and savings reporting.
    input_cost_per_million: float = Field(default=0.0, ge=0)
    output_cost_per_million: float = Field(default=0.0, ge=0)

    @property
    def public_id(self) -> str:
        return self.alias or self.id

    def answers_to(self, name: str) -> bool:
        return name in (self.id, self.alias)


class ProviderConfig(BaseModel):
    name: str = Field(min_length=1)
    locality: Locality
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    models: list[ModelConfig] = Field(min_length=1)

    def resolved_api_key(self) -> str:
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return self.api_key or ("local" if self.locality == "local" else "")

    def model(self, model_id: str) -> ModelConfig | None:
        return next(
            (model for model in self.models if model.answers_to(model_id)), None
        )


class Target(BaseModel):
    provider: str
    model: str | None = None  # None: the provider's first model


class Condition(BaseModel):
    """All listed fields must match; an empty condition always matches."""

    complexity: list[Complexity] | None = None
    task_type: list[str] | None = None
    contains_personal_data: bool | None = None
    # Matches when any of these types was detected.
    personal_data_types: list[str] | None = None
    min_chars: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=0)


class Rule(BaseModel):
    name: str
    when: Condition = Field(default_factory=Condition)
    route: Target


class Defaults(BaseModel):
    local: Target
    cloud: Target | None = None


class PrivacyConfig(BaseModel):
    # Requests containing any of these types are always generated locally.
    never_cloud: list[str] = Field(default_factory=list)


class RoutingConfig(BaseModel):
    providers: list[ProviderConfig] = Field(min_length=1)
    defaults: Defaults
    analysis: Target
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_references(self) -> "RoutingConfig":
        names = [provider.name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("Provider names must be unique")

        targets: list[tuple[str, Target]] = [
            ("defaults.local", self.defaults.local),
            ("analysis", self.analysis),
            *((f"rules[{rule.name}]", rule.route) for rule in self.rules),
        ]
        if self.defaults.cloud is not None:
            targets.append(("defaults.cloud", self.defaults.cloud))
        for where, target in targets:
            provider = self.provider(target.provider)
            if provider is None:
                raise ValueError(f"{where}: unknown provider '{target.provider}'")
            if target.model is not None and provider.model(target.model) is None:
                raise ValueError(
                    f"{where}: provider '{provider.name}' has no model '{target.model}'"
                )

        for where, target, locality in (
            ("defaults.local", self.defaults.local, "local"),
            ("defaults.cloud", self.defaults.cloud, "cloud"),
            # The analysis model sees unredacted text, so it must stay local.
            ("analysis", self.analysis, "local"),
        ):
            if (
                target is not None
                and self.provider(target.provider).locality != locality
            ):  # type: ignore[union-attr]
                raise ValueError(f"{where} must use a {locality} provider")
        return self

    def provider(self, name: str) -> ProviderConfig | None:
        return next((p for p in self.providers if p.name == name), None)

    def resolve(self, target: Target) -> tuple[ProviderConfig, ModelConfig]:
        provider = self.provider(target.provider)
        assert provider is not None  # checked by the validator
        model = provider.model(target.model) if target.model else provider.models[0]
        assert model is not None
        return provider, model

    def find_model(self, model_id: str) -> tuple[ProviderConfig, ModelConfig] | None:
        for provider in self.providers:
            if model := provider.model(model_id):
                return provider, model
        return None


def load_routing_config(path: str | Path) -> RoutingConfig:
    with open(path, encoding="utf-8") as file:
        return RoutingConfig.model_validate(yaml.safe_load(file) or {})


def cloud_rule(cloud_from: str, cloud: Target) -> Rule:
    """The default rule: requests at least this difficult go to the cloud."""
    if cloud_from == "complex":
        return Rule(
            name="complex-to-cloud",
            when=Condition(complexity=["complex"]),
            route=cloud,
        )
    return Rule(
        name="moderate-or-complex-to-cloud",
        when=Condition(complexity=["moderate", "complex"]),
        route=cloud,
    )


def config_from_settings(settings: Settings, *, cloud_enabled: bool) -> RoutingConfig:
    """The configuration equivalent to the LOCAL_* / CLOUD_* settings."""
    providers = [
        ProviderConfig(
            name="local",
            locality="local",
            base_url=settings.local_base_url or None,
            api_key=settings.local_api_key,
            models=[
                ModelConfig(
                    id=settings.local_model or "unconfigured",
                    alias=settings.local_model_alias or None,
                )
            ],
        )
    ]
    rules: list[Rule] = []
    cloud: Target | None = None
    if cloud_enabled and settings.cloud_model:
        providers.append(
            ProviderConfig(
                name="cloud",
                locality="cloud",
                base_url=settings.cloud_base_url or None,
                api_key=settings.cloud_api_key,
                models=[
                    ModelConfig(
                        id=settings.cloud_model,
                        alias=settings.cloud_model_alias or None,
                    )
                ],
            )
        )
        cloud = Target(provider="cloud")
        rules.append(cloud_rule(settings.cloud_from, cloud))
    return RoutingConfig(
        providers=providers,
        defaults=Defaults(local=Target(provider="local"), cloud=cloud),
        analysis=Target(provider="local"),
        rules=rules,
    )
