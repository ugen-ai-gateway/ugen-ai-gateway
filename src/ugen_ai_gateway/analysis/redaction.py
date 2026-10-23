"""Reversible redaction: replace personal data with numbered placeholders and
restore the original values in model output, without the values ever leaving
this process."""

import re
from collections.abc import Iterable
from typing import Any

from ugen_ai_gateway.analysis.pii import _INSTALLED, find_personal_data, value_pattern

PLACEHOLDER_PATTERN = re.compile(r"\[([A-Z][A-Z_]*)_(\d+)\]")
# A trailing fragment that could still grow into a placeholder, e.g. "[EMA" or "[EMAIL_1".
_PARTIAL_PLACEHOLDER = re.compile(r"\[[A-Z_]*\d*\Z")
_MAX_PLACEHOLDER_LENGTH = 40
ADDITIONAL_VALUE_LABEL = "PERSONAL_DATA"

PLACEHOLDER_INSTRUCTION = (
    "Some values in this conversation were replaced with placeholders such as "
    "[EMAIL_1] to protect personal data. Keep every placeholder exactly as written "
    "when you refer to it, and never guess or invent the value behind it."
)


class RedactionVault:
    """Per-request mapping between placeholders and the values they replace.

    The same value always gets the same placeholder, so the model can tell that
    two mentions refer to one thing. The mapping lives only in memory.
    """

    def __init__(
        self, additional_values: Iterable[str] = (), *, custom_finder: Any = _INSTALLED
    ) -> None:
        # Custom terms to use instead of the installed ones (previews only).
        self._custom_finder = custom_finder
        self._additional_values = sorted(
            {value for value in additional_values if len(value) >= 2},
            key=len,
            reverse=True,
        )
        self._placeholder_by_value: dict[str, str] = {}
        self._value_by_placeholder: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self._reserved: set[str] = set()

    def __len__(self) -> int:
        return len(self._value_by_placeholder)

    def values(self) -> list[str]:
        return list(self._value_by_placeholder.values())

    def reserve(self, texts: Iterable[str]) -> None:
        """Never issue placeholders that already appear in the input."""
        for text in texts:
            self._reserved.update(
                match.group() for match in PLACEHOLDER_PATTERN.finditer(text)
            )

    def redact(self, text: str) -> str:
        redacted = text
        for match in reversed(
            find_personal_data(text, custom_finder=self._custom_finder)
        ):
            placeholder = self._placeholder(match.value, match.label)
            redacted = redacted[: match.start] + placeholder + redacted[match.end :]

        for value in self._additional_values:
            if value not in redacted:
                continue
            redacted = value_pattern(value).sub(
                lambda _, value=value: self._placeholder(value, ADDITIONAL_VALUE_LABEL),
                redacted,
            )
        return redacted

    def restore(self, text: str) -> str:
        if not self._value_by_placeholder:
            return text
        return PLACEHOLDER_PATTERN.sub(
            lambda match: self._value_by_placeholder.get(match.group(), match.group()),
            text,
        )

    def _placeholder(self, value: str, label: str) -> str:
        existing = self._placeholder_by_value.get(value)
        if existing is not None:
            return existing
        while True:
            self._counters[label] = self._counters.get(label, 0) + 1
            placeholder = f"[{label}_{self._counters[label]}]"
            if placeholder not in self._reserved:
                break
        self._placeholder_by_value[value] = placeholder
        self._value_by_placeholder[placeholder] = value
        return placeholder


class StreamRestorer:
    """Restore placeholders in streamed text, even when one is split across chunks."""

    def __init__(self, vault: RedactionVault) -> None:
        self._vault = vault
        self._pending = ""

    def feed(self, text: str) -> str:
        buffer = self._pending + text
        start = buffer.rfind("[")
        tail = buffer[start:] if start != -1 else ""
        if (
            tail
            and len(tail) <= _MAX_PLACEHOLDER_LENGTH
            and _PARTIAL_PLACEHOLDER.match(tail)
        ):
            self._pending = tail
            buffer = buffer[:start]
        else:
            self._pending = ""
        return self._vault.restore(buffer)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._vault.restore(rest)
