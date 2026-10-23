"""Model connections managed from the dashboard and stored in the database.

One local and one cloud model, each an OpenAI-compatible endpoint. This
replaces the LOCAL_* / CLOUD_* settings, which only seed the first
configuration.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.routing.config import (
    Condition,
    Defaults,
    ModelConfig,
    ProviderConfig,
    RoutingConfig,
    Rule,
    Target,
    cloud_rule,
)
from ugen_ai_gateway.services.secret_box import SecretBox


class LocalModel(BaseModel):
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    alias: str = Field(default="", max_length=100)


class LocalSettings(LocalModel):
    # Optional smaller model that analyzes requests (faster routing); the
    # main model is used when empty.
    analysis_model: str = Field(default="", max_length=200)
    # Optional model for /v1/embeddings, which always run locally.
    embedding_model: str = Field(default="", max_length=200)


class CloudModel(LocalModel):
    # USD per million tokens, for the cost and savings figures.
    input_cost_per_million: float = Field(default=0, ge=0)
    output_cost_per_million: float = Field(default=0, ge=0)


class ExtraModel(CloudModel):
    """An additional model that routing rules can send requests to."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    name: str = Field(min_length=1, max_length=60)
    locality: Literal["local", "cloud"] = "cloud"

    def usable(self) -> bool:
        return bool(
            self.base_url and self.model and (self.api_key or self.locality == "local")
        )

    @property
    def provider(self) -> str:
        return f"extra-{self.id}"


Complexity = Literal["simple", "moderate", "complex"]
TaskType = Literal[
    "general", "coding", "reasoning", "summarization", "translation", "other"
]


class RuleCondition(BaseModel):
    """Every set field must match; empty lists match anything."""

    complexity: list[Complexity] = Field(default_factory=list)
    task_type: list[TaskType] = Field(default_factory=list)
    contains_personal_data: bool | None = None
    min_chars: int | None = Field(default=None, ge=0)
    max_chars: int | None = Field(default=None, ge=0)


class RouteRule(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    when: RuleCondition = Field(default_factory=RuleCondition)
    # "local", "cloud", or an extra model's id.
    target: str = Field(min_length=1, max_length=40)


class StoredModels(BaseModel):
    local: LocalSettings = Field(default_factory=LocalSettings)
    cloud_enabled: bool = False
    cloud: CloudModel = Field(default_factory=CloudModel)
    # hybrid: rules when a request stays local anyway; llm: the local model
    # judges every request.
    analysis_mode: Literal["hybrid", "llm"] = "hybrid"
    # The lowest difficulty the cloud model answers in Auto mode.
    cloud_from: Literal["moderate", "complex"] = "moderate"
    extra: list[ExtraModel] = Field(default_factory=list, max_length=20)
    rules: list[RouteRule] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def check_references(self) -> "StoredModels":
        ids = [model.id for model in self.extra]
        if len(ids) != len(set(ids)):
            raise ValueError("Extra model ids must be different")
        targets = {"local", "cloud", *ids}
        for rule in self.rules:
            if rule.target not in targets:
                raise ValueError(
                    f"Rule '{rule.name}' sends requests to an unknown model"
                )
        return self

    @model_validator(mode="before")
    @classmethod
    def from_preset_layout(cls, data: Any) -> Any:
        """Read configurations saved with per-provider presets: keep the
        provider that was active."""
        if isinstance(data, dict) and "cloud_active" in data:
            data = dict(data)
            clouds = data.pop("cloud") or {}
            data["cloud"] = clouds.get(data.pop("cloud_active"), {})
            if isinstance(data.get("local"), dict):
                data["local"] = {
                    k: v for k, v in data["local"].items() if k != "preset"
                }
        return data

    def active_cloud(self) -> CloudModel | None:
        cloud = self.cloud
        if self.cloud_enabled and cloud.base_url and cloud.api_key and cloud.model:
            return cloud
        return None


def sealed(stored: StoredModels, box: SecretBox) -> dict[str, Any]:
    """The configuration as stored: API keys encrypted."""
    data = stored.model_dump()
    data["local"]["api_key"] = box.seal(stored.local.api_key)
    data["cloud"]["api_key"] = box.seal(stored.cloud.api_key)
    for extra in data["extra"]:
        extra["api_key"] = box.seal(extra["api_key"])
    return data


def unsealed(data: dict[str, Any], box: SecretBox) -> StoredModels:
    stored = StoredModels.model_validate(data)
    stored.local.api_key = box.open(stored.local.api_key)
    stored.cloud.api_key = box.open(stored.cloud.api_key)
    for extra in stored.extra:
        extra.api_key = box.open(extra.api_key)
    return stored


def from_settings(settings: Settings) -> StoredModels:
    """The configuration the LOCAL_* / CLOUD_* settings describe."""
    return StoredModels(
        local=LocalSettings(
            base_url=settings.local_base_url,
            api_key="" if settings.local_api_key == "local" else settings.local_api_key,
            model=settings.local_model,
            alias=settings.local_model_alias,
        ),
        cloud=CloudModel(
            # The OpenAI SDK's default when CLOUD_BASE_URL was left blank.
            base_url=settings.cloud_base_url
            or ("https://api.openai.com/v1" if settings.cloud_api_key else ""),
            api_key=settings.cloud_api_key,
            model=settings.cloud_model,
            alias=settings.cloud_model_alias,
        ),
        cloud_enabled=bool(settings.cloud_api_key and settings.cloud_model),
        analysis_mode="llm" if settings.analysis_mode == "llm" else "hybrid",
        cloud_from=settings.cloud_from,
    )


def to_routing(stored: StoredModels) -> RoutingConfig:
    """Routing equivalent to the stored models: moderate and complex requests
    go to the active cloud model when there is one."""
    local = stored.local
    providers = [
        ProviderConfig(
            name="local",
            locality="local",
            base_url=local.base_url or None,
            api_key=local.api_key or "local",
            models=[
                ModelConfig(id=local.model, alias=local.alias or None),
                *(
                    [ModelConfig(id=local.analysis_model)]
                    if local.analysis_model and local.analysis_model != local.model
                    else []
                ),
            ],
        )
    ]
    rules: list[Rule] = []
    cloud_target: Target | None = None
    targets: dict[str, Target] = {"local": Target(provider="local")}
    for extra in stored.extra:
        if not extra.usable():
            continue
        providers.append(
            ProviderConfig(
                name=extra.provider,
                locality=extra.locality,
                base_url=extra.base_url,
                api_key=extra.api_key or "local",
                models=[
                    ModelConfig(
                        id=extra.model,
                        alias=extra.alias or None,
                        input_cost_per_million=extra.input_cost_per_million,
                        output_cost_per_million=extra.output_cost_per_million,
                    )
                ],
            )
        )
        targets[extra.id] = Target(provider=extra.provider)
    if cloud := stored.active_cloud():
        providers.append(
            ProviderConfig(
                name="cloud",
                locality="cloud",
                base_url=cloud.base_url or None,
                api_key=cloud.api_key,
                models=[
                    ModelConfig(
                        id=cloud.model,
                        alias=cloud.alias or None,
                        input_cost_per_million=cloud.input_cost_per_million,
                        output_cost_per_million=cloud.output_cost_per_million,
                    )
                ],
            )
        )
        cloud_target = Target(provider="cloud")
        targets["cloud"] = cloud_target
    # The administrator's rules first, in order; rules pointing at a model
    # that is off or incomplete are skipped.
    for rule in stored.rules:
        if rule.target in targets:
            when = rule.when
            rules.append(
                Rule(
                    name=rule.name,
                    when=Condition(
                        complexity=when.complexity or None,
                        task_type=list(when.task_type) or None,
                        contains_personal_data=when.contains_personal_data,
                        min_chars=when.min_chars,
                        max_chars=when.max_chars,
                    ),
                    route=targets[rule.target],
                )
            )
    if cloud_target is not None:
        rules.append(cloud_rule(stored.cloud_from, cloud_target))
    return RoutingConfig(
        providers=providers,
        defaults=Defaults(local=Target(provider="local"), cloud=cloud_target),
        analysis=Target(provider="local", model=local.analysis_model or None),
        rules=rules,
    )


def _key_hint(key: str) -> str:
    return f"…{key[-4:]}" if len(key) > 8 else ("set" if key else "")


def public_view(stored: StoredModels) -> dict[str, Any]:
    """The configuration without secrets: keys become a short hint."""

    def masked(model: BaseModel) -> dict[str, Any]:
        data = model.model_dump()
        key = data.pop("api_key")
        return {**data, "has_api_key": bool(key), "api_key_hint": _key_hint(key)}

    return {
        "local": masked(stored.local),
        "cloud_enabled": stored.cloud_enabled,
        "cloud": masked(stored.cloud),
        "analysis_mode": stored.analysis_mode,
        "cloud_from": stored.cloud_from,
        "extra": [masked(model) for model in stored.extra],
        "rules": [rule.model_dump() for rule in stored.rules],
    }
