import logging
from functools import lru_cache

from ugen_ai_gateway.analysis.analyzer import RequestAnalyzer
from ugen_ai_gateway.analysis.custom_rules import PrivacySettings, rules_finder
from ugen_ai_gateway.analysis.ner import ENTITY_TYPES, build_presidio_finder
from ugen_ai_gateway.analysis.pii import register_finder, set_custom_finder
from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.config import Settings, get_settings
from ugen_ai_gateway.providers.openai_client import create_openai_client
from ugen_ai_gateway.providers.openai_compatible import OpenAICompatibleProvider
from ugen_ai_gateway.routing.config import RoutingConfig, load_routing_config
from ugen_ai_gateway.services.errors import GatewayConfigurationError
from ugen_ai_gateway.services.gateway import GatewayService
from ugen_ai_gateway.services.model_config import (
    StoredModels,
    from_settings,
    to_routing,
    unsealed,
)
from ugen_ai_gateway.services.secret_box import SecretBox
from ugen_ai_gateway.services.usage import UsageStore

logger = logging.getLogger(__name__)


@lru_cache
def get_local_provider() -> OpenAICompatibleProvider:
    settings = get_settings()

    client = create_openai_client(
        api_key=settings.local_api_key,
        base_url=settings.local_base_url or None,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )

    return OpenAICompatibleProvider(client, name="local", locality="local")


@lru_cache
def get_cloud_provider() -> OpenAICompatibleProvider:
    settings = get_settings()

    if not settings.cloud_api_key:
        raise ValueError("CLOUD_API_KEY is not configured")

    client = create_openai_client(
        api_key=settings.cloud_api_key,
        base_url=settings.cloud_base_url or None,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )

    return OpenAICompatibleProvider(client, name="cloud", locality="cloud")


MODELS_SETTING = "models"
PRIVACY_SETTING = "privacy"
# Bumped whenever the model configuration is saved, so the next request
# builds a gateway with the new connections. One process per database.
_models_version = 0
_service: tuple[int, GatewayService] | None = None


def models_changed() -> None:
    global _models_version
    _models_version += 1


def current_models(store: UsageStore | None) -> tuple[StoredModels, str]:
    """The model configuration and where it comes from: "database" once saved
    from the dashboard, "env" (LOCAL_* / CLOUD_*) before that."""
    settings = get_settings()
    saved = store.load_setting(MODELS_SETTING) if store is not None else None
    if saved is not None:
        return unsealed(saved, get_secret_box()), "database"
    return from_settings(settings), "env"


@lru_cache
def _register_ner() -> None:
    settings = get_settings()
    if settings.pii_ner == "presidio":
        register_finder(
            build_presidio_finder(
                language=settings.pii_ner_language,
                spacy_model=settings.pii_ner_spacy_model,
                score_threshold=settings.pii_ner_score_threshold,
            ),
            types=[data_type for data_type, _ in ENTITY_TYPES.values()],
        )


def get_gateway_service() -> GatewayService:
    global _service
    version = _models_version
    if _service is None or _service[0] != version:
        _service = (version, _build_gateway_service())
    return _service[1]


def current_privacy(store: UsageStore | None) -> PrivacySettings:
    saved = store.load_setting(PRIVACY_SETTING) if store is not None else None
    return PrivacySettings.model_validate(saved or {})


def install_rules(privacy: PrivacySettings) -> None:
    """Make the custom terms part of personal-data detection."""
    set_custom_finder(
        rules_finder(privacy.rules) if privacy.rules else None,
        types=[rule.type for rule in privacy.rules],
    )


def _apply_privacy(routing: RoutingConfig, privacy: PrivacySettings) -> RoutingConfig:
    """Install the custom terms and add their keep-local types to the routing."""
    install_rules(privacy)
    never_cloud = list(
        dict.fromkeys([*routing.privacy.never_cloud, *privacy.never_cloud_types()])
    )
    return routing.model_copy(
        update={
            "privacy": routing.privacy.model_copy(update={"never_cloud": never_cloud})
        }
    )


def _build_gateway_service() -> GatewayService:
    """ROUTING_CONFIG (a YAML file) wins; otherwise the models saved from the
    dashboard, or the LOCAL_* / CLOUD_* settings before anything was saved.
    The Privacy page's terms and rules apply either way."""
    settings = get_settings()
    _register_ner()
    store = get_usage_store()
    privacy = current_privacy(store)

    if settings.routing_config:
        return _service_from_routing_config(
            settings,
            _apply_privacy(load_routing_config(settings.routing_config), privacy),
            usage_store=store,
        )

    models, _ = current_models(store)
    if models.local.model:
        return _service_from_routing_config(
            settings.model_copy(
                update={
                    "analysis_mode": models.analysis_mode,
                    "embedding_model": models.local.embedding_model,
                }
            ),
            _apply_privacy(to_routing(models), privacy),
            usage_store=store,
        )
    install_rules(privacy)

    # Nothing configured yet: requests fail until a local model is set.
    local_provider = get_local_provider()
    return GatewayService(
        settings=settings,
        local_provider=local_provider,
        cloud_provider=None,
        analyzer=RequestAnalyzer(provider=local_provider, model=settings.local_model),
        policy=RoutingPolicy(cloud_enabled=False),
        usage_store=store,
    )


@lru_cache
def get_secret_box() -> SecretBox:
    settings = get_settings()
    return SecretBox.for_database(settings.usage_db_path, settings.secret_key)


@lru_cache
def get_usage_store() -> UsageStore | None:
    settings = get_settings()
    return UsageStore(settings.usage_db_path) if settings.usage_db_path else None


def _service_from_routing_config(
    settings: Settings,
    routing: RoutingConfig,
    *,
    usage_store: UsageStore | None = None,
) -> GatewayService:
    providers: dict[str, OpenAICompatibleProvider] = {}
    for config in routing.providers:
        api_key = config.resolved_api_key()
        if not api_key:
            logger.warning(
                "Provider %s has no API key (%s); routes to it are disabled",
                config.name,
                config.api_key_env or "api_key",
            )
            continue
        client = create_openai_client(
            api_key=api_key,
            base_url=config.base_url,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )
        providers[config.name] = OpenAICompatibleProvider(
            client, name=config.name, locality=config.locality
        )

    analysis_provider, analysis_model = routing.resolve(routing.analysis)
    local_provider, _ = routing.resolve(routing.defaults.local)
    for required in (analysis_provider.name, local_provider.name):
        if required not in providers:
            raise GatewayConfigurationError(f"Provider {required} is not usable")
    return GatewayService(
        settings=settings,
        local_provider=providers[local_provider.name],
        cloud_provider=None,
        analyzer=RequestAnalyzer(
            provider=providers[analysis_provider.name], model=analysis_model.id
        ),
        policy=RoutingPolicy(),
        routing=routing,
        providers=dict(providers),
        usage_store=usage_store,
    )


def get_app_settings() -> Settings:
    return get_settings()
