from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
)

Locality = Literal["local", "cloud"]


class ModelProvider(Protocol):
    name: str
    locality: Locality

    async def complete(
        self,
        *,
        model: str,
        messages: list[ChatCompletionMessageParam],
        temperature: float = 0.2,
        **options: Any,
    ) -> ChatCompletion:
        """Generate a chat completion using the specified model and messages."""
        ...

    def stream(
        self,
        *,
        model: str,
        messages: list[ChatCompletionMessageParam],
        temperature: float = 0.2,
        **options: Any,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Stream a chat completion; the final chunk carries token usage."""
        ...
