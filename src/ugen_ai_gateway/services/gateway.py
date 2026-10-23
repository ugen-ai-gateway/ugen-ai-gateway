import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Literal, cast

from openai.types.chat import ChatCompletionMessageParam

from ugen_ai_gateway.analysis.analyzer import RequestAnalyzer
from ugen_ai_gateway.analysis.cache import AnalysisCache
from ugen_ai_gateway.analysis.complexity import assess_complexity
from ugen_ai_gateway.analysis.guard import OutputGuard, check_injection
from ugen_ai_gateway.analysis.pii import (
    find_personal_data,
    has_slow_finders,
    supported_types,
)
from ugen_ai_gateway.analysis.policy import RoutingPolicy
from ugen_ai_gateway.analysis.prompts import MEMORY_SYSTEM_PROMPT
from ugen_ai_gateway.analysis.redaction import (
    PLACEHOLDER_INSTRUCTION,
    RedactionVault,
    StreamRestorer,
)
from ugen_ai_gateway.analysis.rules import rule_analysis
from ugen_ai_gateway.analysis.schemas import RequestAnalysis, TokenUsage
from ugen_ai_gateway.api.schemas import (
    ChatRequest,
    ChatResponse,
    PerformanceStats,
    RoutingInfo,
    UsageStats,
)
from ugen_ai_gateway.config import Settings
from ugen_ai_gateway.providers.base import Locality, ModelProvider
from ugen_ai_gateway.routing.config import RoutingConfig, config_from_settings
from ugen_ai_gateway.routing.engine import Route, RuleEngine
from ugen_ai_gateway.services.errors import (
    GatewayConfigurationError,
    InvalidChatRequest,
    PolicyViolation,
    PrivacyRedactionError,
    ProviderOutage,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
    RequestTooLarge,
)
from ugen_ai_gateway.services.keys import Principal
from ugen_ai_gateway.services.resilience import CircuitBreaker, is_outage
from ugen_ai_gateway.services.usage import UsageRecord, UsageStore, token_cost

logger = logging.getLogger(__name__)
READINESS_CACHE_SECONDS = 10.0

MAX_ANALYSIS_INPUT_CHARACTERS = 32_000
ProgressCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
AnalysisSource = Literal["llm", "rules", "cache"]


@dataclass
class GatewayEvent:
    """One step of a chat request: a stage update, a text delta, or the result."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    response: ChatResponse | None = None

    def to_json(self) -> dict[str, Any]:
        if self.response is not None:
            return {"type": self.type, "data": self.response.model_dump(mode="json")}
        return {"type": self.type, **self.data}


@dataclass
class _PreparedChat:
    analysis: RequestAnalysis
    route: Route
    provider: ModelProvider
    messages: list[ChatCompletionMessageParam]
    analysis_ms: float
    vault: RedactionVault | None

    @property
    def locality(self) -> Locality:
        return self.route.locality

    @property
    def model(self) -> str:
        return self.route.model.id

    @property
    def public_model(self) -> str:
        """The model name clients see: its alias when one is configured."""
        return self.route.model.public_id


@dataclass
class _Generated:
    content: str
    model: str
    usage: TokenUsage | None
    finish_reason: str | None


def _fallback_event(
    failed: _PreparedChat, fallback: _PreparedChat, reason: str
) -> GatewayEvent:
    return GatewayEvent(
        "fallback",
        {
            "from": failed.locality,
            "to": fallback.locality,
            "from_provider": failed.route.provider.name,
            "to_provider": fallback.route.provider.name,
            "reason": reason,
        },
    )


def _token_usage(raw_usage: Any) -> TokenUsage | None:
    if raw_usage is None:
        return None
    return TokenUsage(
        prompt_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
        total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
    )


class GatewayService:
    """Analyze requests and route them to the appropriate model provider."""

    def __init__(
        self,
        *,
        settings: Settings,
        local_provider: ModelProvider,
        cloud_provider: ModelProvider | None,
        analyzer: RequestAnalyzer,
        policy: RoutingPolicy,
        routing: RoutingConfig | None = None,
        providers: dict[str, ModelProvider] | None = None,
        usage_store: UsageStore | None = None,
    ) -> None:
        """Without ``routing``, the LOCAL_* / CLOUD_* settings and the two given
        providers define it. With ``routing``, ``providers`` maps each configured
        provider name to its client."""
        self._settings = settings
        self._usage_store = usage_store
        self._analyzer = analyzer
        self._policy = policy
        self._configured = routing is not None or bool(settings.local_model)
        if routing is None:
            routing = config_from_settings(
                settings,
                cloud_enabled=policy.cloud_enabled and cloud_provider is not None,
            )
            providers = {"local": local_provider}
            if cloud_provider is not None:
                providers["cloud"] = cloud_provider
        self._providers: dict[str, ModelProvider] = providers or {}
        self._engine = RuleEngine(routing, available=set(self._providers))
        self._breakers: dict[str, CircuitBreaker] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._readiness: tuple[float, str | None] = (-READINESS_CACHE_SECONDS, None)
        self._analysis_cache = AnalysisCache(
            max_size=settings.analysis_cache_size,
            ttl_seconds=settings.analysis_cache_ttl_seconds,
        )

    @property
    def routing(self) -> RoutingConfig:
        return self._engine.config

    @property
    def engine(self) -> RuleEngine:
        return self._engine

    async def readiness(self) -> bool:
        return await self.readiness_problem() is None

    async def readiness_problem(self) -> str | None:
        """Why the gateway cannot serve requests, or None when it can.

        Provider checks are reused for a few seconds, so frequent health
        probes do not turn into a stream of calls to paid cloud APIs."""
        if not self._configured:
            return "No local model is configured; set one on the Models page"
        checked_at, problem = self._readiness
        if time.monotonic() - checked_at < READINESS_CACHE_SECONDS:
            return problem
        problem = await self._check_providers()
        self._readiness = (time.monotonic(), problem)
        return problem

    async def _check_providers(self) -> str | None:
        for name, provider in self._providers.items():
            try:
                await provider.health_check()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - readiness reports any provider failure
                logger.warning("Provider %s is not reachable: %s", name, exc)
                return f"Provider '{name}' is not reachable; see the server logs"
        return None

    @property
    def configured(self) -> bool:
        """Whether a local model is set up, so requests can be served."""
        return self._configured

    @property
    def embedding_model(self) -> str:
        return self._settings.embedding_model

    async def embed(
        self, inputs: Any, *, options: dict[str, Any], principal: Principal
    ) -> Any:
        """Create embeddings on the local model only: documents being indexed
        never leave this machine, so they need no redaction."""
        model = self.embedding_model
        if not model:
            raise GatewayConfigurationError(
                "No embedding model is configured; an administrator can set one on "
                "the Models page"
            )
        route = self._engine.default("local")
        if route is None:
            raise ProviderUnavailable("Local provider is unavailable")
        provider = self._provider_for(route)
        record = UsageRecord(
            id=uuid.uuid4().hex,
            api_key_id=principal.api_key_id,
            user_id=principal.user_id,
            provider="local",
            provider_name=route.provider.name,
            model=model,
            task_type="embedding",
        )
        started = perf_counter()
        try:
            result = await provider.embed(model=model, input=inputs, **options)  # type: ignore[attr-defined]
        except TimeoutError as exc:
            record.status, record.error = "error", "timeout"
            await self._save(record)
            raise ProviderTimeout("Local model timed out") from exc
        except Exception as exc:
            record.status, record.error = "error", type(exc).__name__
            await self._save(record)
            error = ProviderOutage if is_outage(exc) else ProviderRequestError
            raise error(f"Embedding request failed: {exc}") from exc
        usage = getattr(result, "usage", None)
        record.prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        record.generation_ms = record.total_ms = (perf_counter() - started) * 1000
        await self._save(record)
        return result

    async def summarize_memory(
        self, previous: str, messages: list[dict[str, str]]
    ) -> str:
        """Fold older messages into the conversation memory using the local
        model, so nothing leaves this machine to do it."""
        if not self._configured:
            raise GatewayConfigurationError(
                "No local model is configured; an administrator can set one on the "
                "Models page"
            )
        route = self._engine.default("local")
        if route is None:
            raise ProviderUnavailable("Local provider is unavailable")
        transcript = "\n\n".join(
            f"{message['role'].upper()}: {message['content']}" for message in messages
        )
        try:
            completion = await self._provider_for(route).complete(
                model=route.model.id,
                messages=[
                    {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Current memory:\n{previous or '(empty)'}\n\n"
                        f"Older messages:\n{transcript}",
                    },
                ],
                temperature=0.2,
            )
        except TimeoutError as exc:
            raise ProviderTimeout("Local model timed out") from exc
        except Exception as exc:
            error = ProviderOutage if is_outage(exc) else ProviderRequestError
            raise error(f"Updating the memory failed: {exc}") from exc
        content = completion.choices[0].message.content if completion.choices else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderRequestError("The local model returned an empty memory")
        return content.strip()

    async def chat(
        self,
        request: ChatRequest,
        progress: ProgressCallback | None = None,
        *,
        options: dict[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> ChatResponse:
        """Run a request to completion, reporting stage events to ``progress``."""
        async for event in self.events(
            request, stream=False, options=options, principal=principal
        ):
            if event.response is not None:
                return event.response
            if progress is not None:
                await progress(event.type, event.data)
        raise RuntimeError("Gateway finished without a response")

    async def events(
        self,
        request: ChatRequest,
        *,
        stream: bool,
        options: dict[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> AsyncIterator[GatewayEvent]:
        """Yield stage events, text deltas (when streaming) and the final result.

        ``options`` are extra generation parameters (``max_tokens``, ``top_p``,
        ...) passed through to the provider unchanged. Every request, including
        failed ones, is recorded in the usage store (metadata only).
        """
        principal = principal or Principal(api_key_id=None)
        record = UsageRecord(
            id=uuid.uuid4().hex,
            api_key_id=principal.api_key_id,
            user_id=principal.user_id,
        )
        finished = False
        try:
            async for event in self._events(
                request, stream, options or {}, record, principal
            ):
                finished = finished or event.response is not None
                yield event
        except Exception as exc:
            if record.status == "ok":
                record.status = "error"
            record.error = type(exc).__name__
            await self._save(record)
            raise
        except (GeneratorExit, asyncio.CancelledError):
            if finished:  # closed after the result: already recorded as complete
                raise
            # The client went away mid-request. The prompt may already have
            # reached a model, so the request is still logged, without waiting.
            record.status = "cancelled"
            task = asyncio.get_running_loop().create_task(self._save(record))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            raise

    async def _save(self, record: UsageRecord) -> None:
        if self._usage_store is None:
            return
        try:
            await self._usage_store.record(record)
            await self._usage_store.append_audit(
                f"request.{record.status}",
                request_id=record.id,
                detail=_audit_detail(record),
            )
        except Exception:
            logger.exception("Could not record usage")

    async def _events(
        self,
        request: ChatRequest,
        stream: bool,
        options: dict[str, Any],
        record: UsageRecord,
        principal: Principal,
    ) -> AsyncIterator[GatewayEvent]:
        total_started_at = perf_counter()
        user_input = self._validated_user_input(request)
        injection_action = self._settings.injection_action
        # (reason, rule) when something other than routing keeps this request local.
        force_local: tuple[str, str] | None = await self._key_restriction(principal)
        if injection_action != "off":
            injection = check_injection(_user_texts(request.messages))
            if injection.detected:
                record.injection_signals = injection.signals
                yield GatewayEvent(
                    "guard",
                    {
                        "kind": "injection",
                        "signals": injection.signals,
                        "action": injection_action,
                    },
                )
                if injection_action == "block":
                    record.status = "blocked"
                    raise PolicyViolation(
                        "Request blocked: it looks like a prompt-injection attempt"
                    )
                if injection_action == "local":
                    force_local = (
                        "Possible prompt injection; kept on the local model.",
                        "guard.injection",
                    )
        if force_local:
            request = request.model_copy(
                update={"provider_selection": "local", "model": None}
            )

        yield GatewayEvent("analysis_started", {"request_id": record.id})
        analysis, analysis_ms, analysis_source = await self._analyze(
            request, user_input
        )
        yield GatewayEvent(
            "analysis_completed",
            {
                "source": analysis_source,
                "complexity": analysis.complexity,
                "task_type": analysis.task_type,
                "contains_personal_data": analysis.contains_personal_data,
                "personal_data_types": analysis.personal_data_types,
            },
        )

        record.task_type = analysis.task_type
        record.complexity = analysis.complexity
        record.analysis_source = analysis_source
        record.personal_data_types = list(analysis.personal_data_types)
        record.analysis_ms = analysis_ms

        # Redaction scans every message; slow detection (NER) runs off the
        # event loop so other requests keep streaming meanwhile.
        if has_slow_finders():
            prepared = await asyncio.to_thread(
                self._prepare, request, analysis, analysis_ms
            )
        else:
            prepared = self._prepare(request, analysis, analysis_ms)
        if force_local and prepared.route.rule != "privacy.never_cloud":
            reason, rule = force_local
            prepared.route = replace(prepared.route, reason=reason, rule=rule)
        record.redacted_values = len(prepared.vault or ())
        _note_route(record, prepared)
        output_action = self._settings.output_pii_action
        guard = (
            OutputGuard(
                self._allowed_output_values(request, analysis, prepared),
                redact=output_action == "redact",
            )
            if output_action != "off"
            else None
        )
        yield GatewayEvent(
            "routed",
            {
                "provider": prepared.locality,
                "provider_name": prepared.route.provider.name,
                "model": prepared.public_model,
                "rule": prepared.route.rule,
                "reason": prepared.route.reason,
                "complexity": analysis.complexity,
                "redacted_values": len(prepared.vault or ()),
            },
        )

        generation_started_at = perf_counter()
        attempt: _PreparedChat | None = prepared
        fallback_from: str | None = None
        result: _Generated | None = None
        while attempt is not None:
            breaker = self._breaker(attempt.route.provider.name)
            next_attempt = (
                self._fallback_for(request, attempt, analysis_source)
                if fallback_from is None
                else None
            )
            if not breaker.allow():
                if next_attempt is None:
                    raise ProviderUnavailable(
                        f"The {attempt.route.provider.name} provider is temporarily "
                        "unavailable"
                    )
                fallback_from = attempt.locality
                yield _fallback_event(attempt, next_attempt, "circuit open")
                attempt = next_attempt
                _note_route(record, attempt)
                record.fallback_from = fallback_from
                continue

            yield GatewayEvent(
                "generation_started",
                {
                    "provider": attempt.locality,
                    "provider_name": attempt.route.provider.name,
                    "model": attempt.public_model,
                },
            )
            emitted_text = False
            try:
                async for event in self._generate(
                    attempt, request, stream=stream, options=options, guard=guard
                ):
                    if isinstance(event, _Generated):
                        result = event
                    else:
                        emitted_text = True
                        yield event
            except (ProviderOutage, ProviderTimeout) as exc:
                breaker.record_failure()
                if emitted_text or next_attempt is None:
                    raise
                if guard is not None:
                    guard.discard_pending()
                fallback_from = attempt.locality
                yield _fallback_event(attempt, next_attempt, str(exc))
                attempt = next_attempt
                _note_route(record, attempt)
                record.fallback_from = fallback_from
                continue
            breaker.record_success()
            prepared = attempt
            break
        assert result is not None
        content = result.content
        # An alias hides the real model name, including the one the provider reports.
        resolved_model = prepared.route.model.alias or result.model
        usage = result.usage
        finish_reason = result.finish_reason
        generation_ms = (perf_counter() - generation_started_at) * 1000
        output_findings = guard.findings if guard else []
        if output_findings:
            yield GatewayEvent(
                "guard",
                {"kind": "output", "types": output_findings, "action": output_action},
            )
        record.output_redactions = len(output_findings)
        response = ChatResponse(
            id=record.id,
            model=resolved_model,
            content=content,
            finish_reason=finish_reason,
            provider=prepared.locality,
            routing=RoutingInfo(
                complexity=analysis.complexity,
                task_type=analysis.task_type,
                contains_personal_data=analysis.contains_personal_data,
                personal_data_types=analysis.personal_data_types,
                provider=prepared.locality,
                provider_name=prepared.route.provider.name,
                rule=prepared.route.rule,
                reason=prepared.route.reason,
                redacted_values=len(prepared.vault or ()),
                analysis_source=analysis_source,
                fallback_from=fallback_from,
                injection_signals=record.injection_signals,
                output_findings=output_findings,
            ),
            usage=UsageStats(analysis=analysis.usage, generation=usage),
            performance=PerformanceStats(
                analysis_ms=analysis_ms,
                generation_ms=generation_ms,
                total_ms=(perf_counter() - total_started_at) * 1000,
            ),
        )
        self._fill_record(record, response, prepared)
        await self._save(record)
        yield GatewayEvent("result", response=response)

    async def _key_restriction(self, principal: Principal) -> tuple[str, str] | None:
        """Why this API key must stay on local models, if it must."""
        if not principal.allow_cloud:
            return ("This API key may only use local models.", "key.local_only")
        if (
            principal.monthly_budget_usd is not None
            and principal.api_key_id
            and self._usage_store is not None
            and await self._usage_store.month_spend(principal.api_key_id)
            >= principal.monthly_budget_usd
        ):
            return ("Monthly cloud budget for this API key is used up.", "key.budget")
        if (
            principal.monthly_budget_usd is not None
            and principal.user_id
            and self._usage_store is not None
            and await self._usage_store.user_month_spend(principal.user_id)
            >= principal.monthly_budget_usd
        ):
            return ("Your monthly cloud budget is used up.", "user.budget")
        return None

    def _fill_record(
        self, record: UsageRecord, response: ChatResponse, prepared: _PreparedChat
    ) -> None:
        usage = response.usage.generation if response.usage else None
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        model = prepared.route.model
        baseline = self._engine.config.defaults.cloud
        baseline_model = self._engine.config.resolve(baseline)[1] if baseline else None
        record.provider = prepared.locality
        record.provider_name = prepared.route.provider.name
        record.model = response.model
        record.rule = prepared.route.rule
        record.fallback_from = response.routing.fallback_from
        record.prompt_tokens = prompt_tokens
        record.completion_tokens = completion_tokens
        if response.performance:
            record.generation_ms = response.performance.generation_ms
            record.total_ms = response.performance.total_ms
        record.cost_usd = token_cost(
            prompt_tokens,
            completion_tokens,
            model.input_cost_per_million,
            model.output_cost_per_million,
        )
        record.baseline_cost_usd = (
            token_cost(
                prompt_tokens,
                completion_tokens,
                baseline_model.input_cost_per_million,
                baseline_model.output_cost_per_million,
            )
            if baseline_model
            else 0.0
        )

    async def _generate(
        self,
        prepared: "_PreparedChat",
        request: ChatRequest,
        *,
        stream: bool,
        options: dict[str, Any],
        guard: OutputGuard | None = None,
    ) -> AsyncIterator["GatewayEvent | _Generated"]:
        """Call the provider; yield text deltas (streaming) and then the result.

        Text passes through placeholder restoration, then the output guard.
        """
        restore = prepared.vault is not None
        try:
            if stream:
                restorer = StreamRestorer(prepared.vault) if restore else None  # type: ignore[arg-type]
                parts: list[str] = []
                model = prepared.model
                usage: TokenUsage | None = None
                finish_reason: str | None = None
                async for chunk in prepared.provider.stream(
                    model=prepared.model,
                    messages=prepared.messages,
                    temperature=request.temperature,
                    **options,
                ):
                    model = getattr(chunk, "model", None) or model
                    usage = _token_usage(getattr(chunk, "usage", None)) or usage
                    for choice in chunk.choices or []:
                        finish_reason = (
                            getattr(choice, "finish_reason", None) or finish_reason
                        )
                        text = getattr(choice.delta, "content", None)
                        if text and restorer is not None:
                            text = restorer.feed(text)
                        if text and guard is not None:
                            text = guard.feed(text)
                        if text:
                            parts.append(text)
                            yield GatewayEvent("delta", {"content": text})
                rest = restorer.flush() if restorer is not None else ""
                if guard is not None:
                    rest = guard.feed(rest) + guard.flush()
                if rest:
                    parts.append(rest)
                    yield GatewayEvent("delta", {"content": rest})
                yield _Generated("".join(parts), model, usage, finish_reason)
                return

            completion = await prepared.provider.complete(
                model=prepared.model,
                messages=prepared.messages,
                temperature=request.temperature,
                **options,
            )
        except TimeoutError as exc:
            raise ProviderTimeout("Model provider timed out") from exc
        except Exception as exc:
            error = ProviderOutage if is_outage(exc) else ProviderRequestError
            raise error(f"Model provider request failed: {exc}") from exc

        if not completion.choices:
            raise RuntimeError("Model provider returned no choices")
        content = completion.choices[0].message.content
        if not isinstance(content, str):
            raise TypeError("Model provider returned no text content")
        if restore:
            content = prepared.vault.restore(content)  # type: ignore[union-attr]
        if guard is not None:
            content = guard.check(content)
        yield _Generated(
            content,
            completion.model,
            _token_usage(getattr(completion, "usage", None)),
            getattr(completion.choices[0], "finish_reason", None),
        )

    @staticmethod
    def _allowed_output_values(
        request: ChatRequest, analysis: RequestAnalysis, prepared: "_PreparedChat"
    ) -> set[str]:
        """Personal data the user supplied may appear in the answer."""
        allowed = {
            match.value
            for text in _message_texts(request.messages)
            for match in find_personal_data(text)
        }
        allowed.update(analysis.personal_data_values)
        if prepared.vault is not None:
            allowed.update(prepared.vault.values())
        return allowed

    def _breaker(self, provider: str) -> CircuitBreaker:
        breaker = self._breakers.get(provider)
        if breaker is None:
            breaker = self._breakers[provider] = CircuitBreaker(
                failure_threshold=self._settings.circuit_failure_threshold,
                cooldown_seconds=self._settings.circuit_cooldown_seconds,
            )
        return breaker

    def provider_status(self) -> dict[str, str]:
        return {name: self._breaker(name).state for name in self._providers}

    def _fallback_for(
        self,
        request: ChatRequest,
        prepared: "_PreparedChat",
        analysis_source: AnalysisSource,
    ) -> "_PreparedChat | None":
        """The plan to use if the routed provider is down, or None.

        Only automatic routing falls back; an explicit provider choice is kept.
        """
        if request.provider_selection != "auto":
            return None
        analysis = prepared.analysis
        if prepared.locality == "cloud":
            route = self._engine.default("local")
            if not (self._settings.fallback_to_local and route):
                return None
            # Reuse the redacted messages: still safe, and the vault restores them.
            return replace(
                prepared,
                route=replace(
                    route,
                    reason="Cloud provider unavailable; fell back to the local model.",
                ),
                provider=self._provider_for(route),
            )

        route = self._engine.default("cloud")
        if not (
            self._settings.fallback_to_cloud
            and route is not None
            and route.provider.name in self._providers
            and analysis_source != "rules"
            and not analysis.contains_personal_data
        ):
            return None
        vault = RedactionVault(analysis.personal_data_values)
        return replace(
            prepared,
            route=replace(
                route,
                reason="Local provider unavailable; fell back to the cloud model.",
            ),
            provider=self._provider_for(route),
            messages=self._redacted_messages(request, vault),
            vault=vault,
        )

    def _provider_for(self, route: Route) -> ModelProvider:
        provider = self._providers.get(route.provider.name)
        if provider is None:
            raise ProviderUnavailable(
                f"The {route.provider.name} provider is unavailable"
            )
        return provider

    def _validated_user_input(self, request: ChatRequest) -> str:
        if not self._configured:
            raise GatewayConfigurationError(
                "No local model is configured; an administrator can set one on the "
                "Models page"
            )

        # Text parts count too: many OpenAI clients send content as a list.
        user_input = "\n".join(_user_texts(request.messages))
        if not user_input:
            raise InvalidChatRequest("A text user message is required")
        if len(user_input) > MAX_ANALYSIS_INPUT_CHARACTERS:
            raise RequestTooLarge(
                f"User text exceeds the {MAX_ANALYSIS_INPUT_CHARACTERS}-character limit"
            )
        return user_input

    async def _analyze(
        self, request: ChatRequest, user_input: str
    ) -> tuple[RequestAnalysis, float, AnalysisSource]:
        """Analyze the request, returning the result, its duration and its source.

        Difficulty and task type come from the latest user message, so an
        earlier complex question does not send every later one to the cloud.
        Personal data is looked for in every user and system message (system
        messages carry conversation memory). Each message is analyzed once and
        cached, so a long conversation only costs one model call per turn.
        """
        started_at = perf_counter()
        mode = self._settings.analysis_mode
        source: AnalysisSource
        user_texts = _user_texts(request.messages)
        latest = user_texts[-1]
        assessment = assess_complexity(
            latest, user_texts[-2] if len(user_texts) > 1 else None
        )
        # The analysis model judges an unclear follow-up together with the
        # request it follows ("第二點再詳細一點" alone says little).
        message = latest
        if assessment.follow_up and not assessment.certain:
            latest = f"{user_texts[-2]}\n\n{message}"
        # The message itself is covered by the combined text when there is one.
        texts = list(
            dict.fromkeys(
                [
                    *(
                        text
                        for text in _analysis_texts(request.messages)
                        if text != message or latest == message
                    ),
                    latest,
                ]
            )
        )

        # The rules decide the difficulty when they are sure; otherwise the
        # analysis model does. Hybrid mode skips the model call only when the
        # rules are sure and the request stays on the local model, so
        # free-form personal data is still checked before anything can reach
        # the cloud.
        quick = _merge(rule_analysis(latest), [rule_analysis(t) for t in texts])
        quick = quick.model_copy(
            update={"complexity": assessment.complexity, "reason": assessment.reason}
        )
        if mode == "rules" or (
            mode == "hybrid"
            and assessment.certain
            and self._stays_local(request, quick)
        ):
            return quick, (perf_counter() - started_at) * 1000, "rules"

        results: dict[str, RequestAnalysis] = {}
        missing = []
        for text in texts:
            if cached := self._analysis_cache.get(text):
                results[text] = cached
            else:
                missing.append(text)
        # Hybrid: the latest message gets its own full analysis; older
        # messages whose cached result expired only need their personal data,
        # so they share one call, run alongside it. A long conversation costs
        # at most two model calls however many messages it has.
        latest_missing = latest in missing
        older = [text for text in missing if text != latest]
        calls = []
        if latest_missing:
            calls.append(self._analyzer.analyze(latest))
        if older:
            calls.append(self._analyzer.analyze(ANALYSIS_BATCH_SEPARATOR.join(older)))
        try:
            fresh = await asyncio.gather(*calls)
        except TimeoutError as exc:
            raise ProviderTimeout("Analysis provider timed out") from exc
        except Exception as exc:
            raise ProviderRequestError(f"Request analysis failed: {exc}") from exc
        if latest_missing:
            results[latest] = fresh[0]
            self._analysis_cache.put(latest, fresh[0])
            if latest != message:
                # Cache the message's own share, so the next turn, where it
                # is an earlier message, needs no call for it.
                share = _share_of_batch(message, fresh[0])
                if not (
                    fresh[0].contains_personal_data
                    and not fresh[0].personal_data_values
                ):
                    self._analysis_cache.put(message, share)
        if older:
            batch = fresh[-1]
            # Personal data reported without values cannot be traced to one
            # message, so every message in the batch carries it. Cache such
            # shares only for this request: the batch may hold another
            # account's text, and a shared message (the system prompt) would
            # carry its findings into everyone's requests.
            unplaced = batch.contains_personal_data and not batch.personal_data_values
            for text in older:
                result = _share_of_batch(text, batch)
                results[text] = result
                if not unplaced:
                    self._analysis_cache.put(text, result)
        source = "llm" if missing else "cache"

        analysis = _merge(results[latest], list(results.values()))
        usages = [result.usage for result in fresh if result.usage is not None]
        if usages:
            analysis = analysis.model_copy(
                update={
                    "usage": TokenUsage(
                        prompt_tokens=sum(u.prompt_tokens for u in usages),
                        completion_tokens=sum(u.completion_tokens for u in usages),
                        total_tokens=sum(u.total_tokens for u in usages),
                    )
                }
            )
        else:
            analysis = analysis.model_copy(update={"usage": None})
        if assessment.certain:
            analysis = analysis.model_copy(
                update={
                    "complexity": assessment.complexity,
                    "reason": assessment.reason,
                }
            )
        return analysis, (perf_counter() - started_at) * 1000, source

    def _stays_local(self, request: ChatRequest, analysis: RequestAnalysis) -> bool:
        """Whether the request would be generated locally given this analysis.

        Hybrid mode only skips the analysis model in that case, so free-form
        personal data (names, addresses) is still checked by the model before
        anything can be sent to the cloud.
        """
        return self._route(request, analysis).locality == "local"

    def _route(self, request: ChatRequest, analysis: RequestAnalysis) -> Route:
        return self._engine.decide(
            analysis,
            selection=request.provider_selection,
            model=request.model,
            chars=sum(len(text) for text in _message_texts(request.messages)),
        )

    def _prepare(
        self, request: ChatRequest, analysis: RequestAnalysis, analysis_ms: float
    ) -> _PreparedChat:
        route = self._route(request, analysis)
        if route.locality == "cloud" and not self._redactable(analysis):
            # The analysis found personal data it could not pin down (e.g. "a
            # name" without the name), so redaction cannot be trusted: keep the
            # request on this machine rather than refusing it.
            local = self._engine.default("local")
            if local is None:
                raise PrivacyRedactionError(
                    "Detected personal data could not be safely redacted"
                )
            route = Route(
                local.provider,
                local.model,
                "Personal data could not be redacted reliably; kept on the local model.",
                "privacy.unredactable",
            )
        provider = self._provider_for(route)

        # Personal data is always redacted, whichever model answers.
        vault = RedactionVault(analysis.personal_data_values)
        messages = self._redacted_messages(request, vault)

        return _PreparedChat(
            analysis=analysis,
            route=route,
            provider=provider,
            messages=messages,
            analysis_ms=analysis_ms,
            vault=vault,
        )

    @staticmethod
    def _redactable(analysis: RequestAnalysis) -> bool:
        """Whether every detected personal-data item can actually be redacted.

        With named-entity recognition on, names and places are looked for in
        every message by the redaction itself, so personal data the analysis
        model reports but cannot point to adds nothing that could be
        redacted: small models report it out of caution even for plain
        technical requests. Without NER the claim keeps the request local."""
        if has_slow_finders():
            return True
        unredactable_types = {
            data_type.casefold()
            for data_type in analysis.personal_data_types
            if data_type.casefold() not in supported_types()
        }
        if unredactable_types and not analysis.personal_data_values:
            return False
        return not (
            analysis.contains_personal_data
            and not (analysis.personal_data_types or analysis.personal_data_values)
        )

    @staticmethod
    def _redacted_messages(
        request: ChatRequest, vault: RedactionVault
    ) -> list[ChatCompletionMessageParam]:
        vault.reserve(_message_texts(request.messages))
        messages: list[ChatCompletionMessageParam] = []
        for message in request.messages:
            redacted_message = cast(dict[str, Any], message).copy()
            content = redacted_message.get("content")
            if isinstance(content, str):
                redacted_message["content"] = vault.redact(content)
            elif content is not None and not isinstance(content, dict):
                redacted_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        redacted_parts.append(part)
                        continue
                    redacted_part = part.copy()
                    if isinstance(redacted_part.get("text"), str):
                        redacted_part["text"] = vault.redact(redacted_part["text"])
                    redacted_parts.append(redacted_part)
                redacted_message["content"] = redacted_parts
            messages.append(cast(ChatCompletionMessageParam, redacted_message))

        messages = _merged_system_messages(messages)
        if len(vault):
            messages = _with_system_instruction(messages, PLACEHOLDER_INSTRUCTION)
        return messages


def _note_route(record: UsageRecord, prepared: _PreparedChat) -> None:
    record.provider = prepared.locality
    record.provider_name = prepared.route.provider.name
    record.model = prepared.public_model
    record.rule = prepared.route.rule


def _analysis_texts(messages: list[ChatCompletionMessageParam]) -> list[str]:
    """Texts checked for personal data: user messages and system messages
    (conversation memory). Assistant replies only repeat what users wrote."""
    return _message_texts(
        [message for message in messages if message["role"] in ("user", "system")]
    )


# Joins older messages analyzed together; analysis only reports values found
# in the text, so nothing can match across the join.
ANALYSIS_BATCH_SEPARATOR = "\n\n-----\n\n"


def _share_of_batch(text: str, batch: RequestAnalysis) -> RequestAnalysis:
    """One message's part of a combined analysis: the reported values that
    occur in it, with the batch's types when it has any (plus what the rules
    find in it on their own)."""
    rules = rule_analysis(text)
    values = [value for value in batch.personal_data_values if value in text]
    # Personal data reported without values cannot be traced to a message, so
    # every message in the batch keeps it (and the request stays redactable
    # only if the values were found some other way).
    unplaced = batch.contains_personal_data and not batch.personal_data_values
    types = list(
        dict.fromkeys(
            [
                *(batch.personal_data_types if values or unplaced else []),
                *rules.personal_data_types,
            ]
        )
    )
    return rules.model_copy(
        update={
            "contains_personal_data": bool(values or types or unplaced),
            "personal_data_types": types,
            "personal_data_values": list(
                dict.fromkeys([*values, *rules.personal_data_values])
            ),
            "usage": None,
        }
    )


def _merge(latest: RequestAnalysis, analyses: list[RequestAnalysis]) -> RequestAnalysis:
    """The latest message's difficulty with personal data from all messages."""
    return latest.model_copy(
        update={
            "contains_personal_data": any(a.contains_personal_data for a in analyses),
            "personal_data_types": list(
                dict.fromkeys(t for a in analyses for t in a.personal_data_types)
            ),
            "personal_data_values": list(
                dict.fromkeys(v for a in analyses for v in a.personal_data_values)
            ),
        }
    )


def _user_texts(messages: list[ChatCompletionMessageParam]) -> list[str]:
    return _message_texts(
        [message for message in messages if message["role"] == "user"]
    )


def _audit_detail(record: UsageRecord) -> dict[str, Any]:
    """Request metadata for the audit log; never message text or values."""
    fields = (
        "api_key_id",
        "status",
        "error",
        "provider",
        "provider_name",
        "model",
        "rule",
        "task_type",
        "complexity",
        "analysis_source",
        "personal_data_types",
        "redacted_values",
        "fallback_from",
        "injection_signals",
        "output_redactions",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
    )
    return {name: getattr(record, name) for name in fields}


def _message_texts(messages: list[ChatCompletionMessageParam]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return texts


def _system_text(message: ChatCompletionMessageParam) -> str | None:
    """The text of a system (or developer) message, given as a string or as
    text parts; None for any other message."""
    if message["role"] not in ("system", "developer"):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict) and part.get("type") == "text" for part in content
    ):
        return "\n".join(str(part.get("text", "")) for part in content)
    return None


def _merged_system_messages(
    messages: list[ChatCompletionMessageParam],
) -> list[ChatCompletionMessageParam]:
    """Join the system messages at the start (the gateway's system prompt, the
    account's instructions, conversation memory, the client's own) into one:
    some models' chat templates accept only a single system message."""
    texts: list[str] = []
    for message in messages:
        text = _system_text(message)
        if text is None:
            break
        texts.append(text)
    if len(texts) < 2:
        return messages
    return [{"role": "system", "content": "\n\n".join(texts)}, *messages[len(texts) :]]


def _with_system_instruction(
    messages: list[ChatCompletionMessageParam], instruction: str
) -> list[ChatCompletionMessageParam]:
    """Append to the first system message, or add one at the start."""
    for index, message in enumerate(messages):
        if message["role"] == "system" and isinstance(message.get("content"), str):
            updated = cast(dict[str, Any], message).copy()
            updated["content"] = f"{updated['content']}\n\n{instruction}"
            return [
                *messages[:index],
                cast(ChatCompletionMessageParam, updated),
                *messages[index + 1 :],
            ]
    return [{"role": "system", "content": instruction}, *messages]
