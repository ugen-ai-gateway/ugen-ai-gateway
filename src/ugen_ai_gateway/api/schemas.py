from typing import Any, Literal

from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, Field, field_validator, model_validator

from ugen_ai_gateway.analysis.schemas import TaskType, TokenUsage

MAX_CHAT_MESSAGES = 64
MAX_CHAT_TEXT_CHARACTERS = 32_000
MAX_MODEL_NAME_CHARACTERS = 128


class RoutingInfo(BaseModel):
    complexity: Literal["simple", "moderate", "complex"]
    task_type: TaskType
    contains_personal_data: bool
    personal_data_types: list[str] = Field(default_factory=list)
    provider: Literal["local", "cloud"]
    provider_name: str = ""
    rule: str | None = None
    reason: str
    redacted_values: int = Field(default=0, ge=0)
    analysis_source: Literal["llm", "rules", "cache"] = "llm"
    fallback_from: Literal["local", "cloud"] | None = None
    injection_signals: list[str] = Field(default_factory=list)
    output_findings: list[str] = Field(default_factory=list)


class UsageStats(BaseModel):
    analysis: TokenUsage | None = None
    generation: TokenUsage | None = None


class PerformanceStats(BaseModel):
    analysis_ms: float = Field(ge=0)
    generation_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class ChatRequest(BaseModel):
    provider: Literal["local", "cloud"] = "local"
    provider_selection: Literal["auto", "local", "cloud"] = "auto"
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_MODEL_NAME_CHARACTERS,
    )
    messages: list[ChatCompletionMessageParam] = Field(
        min_length=1, max_length=MAX_CHAT_MESSAGES
    )
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    @field_validator("messages")
    @classmethod
    def materialize_content_parts(
        cls, messages: list[ChatCompletionMessageParam]
    ) -> list[ChatCompletionMessageParam]:
        """The OpenAI types declare content parts as an Iterable, which pydantic
        turns into a one-shot iterator; store them as a list so they can be
        read more than once (analysis, redaction, guards)."""
        materialized: list[ChatCompletionMessageParam] = []
        for message in messages:
            content = message.get("content")
            if content is not None and not isinstance(content, str | list | dict):
                message = {**message, "content": list(content)}  # type: ignore[assignment, call-overload]
            materialized.append(message)
        return materialized

    @field_validator("model")
    @classmethod
    def strip_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model must not be blank")
        return normalized

    @model_validator(mode="before")
    @classmethod
    def validate_text_size(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value

        messages = value.get("messages")
        if not isinstance(messages, list):
            return value

        text_characters = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                text_characters += len(content)
            elif isinstance(content, list):
                text_characters += sum(
                    len(part["text"])
                    for part in content
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )

        if text_characters > MAX_CHAT_TEXT_CHARACTERS:
            raise ValueError(
                f"Message text exceeds the {MAX_CHAT_TEXT_CHARACTERS}-character limit"
            )
        return value


class ChatResponse(BaseModel):
    id: str = ""
    model: str
    content: str
    finish_reason: str | None = None
    provider: str
    routing: RoutingInfo
    usage: UsageStats | None = None
    performance: PerformanceStats | None = None
