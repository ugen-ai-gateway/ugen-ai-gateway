"""Input and output guards: prompt-injection signals and personal data the model
produced on its own."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from ugen_ai_gateway.analysis.pii import find_personal_data

_INJECTION_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
            r"\b(?:previous|prior|above|earlier|all|system|your)\b[^.\n]{0,20}"
            r"\b(?:instructions?|prompts?|rules|guidelines|directions)\b"
            r"|(?:忽略|無視|忘記|不要理會|跳過)[^。\n]{0,12}"
            r"(?:之前|先前|以上|上面|前面|所有|系統)[^。\n]{0,6}(?:的)?"
            r"(?:指示|指令|規則|設定|提示|限制)",
            re.IGNORECASE,
        ),
    ),
    (
        "reveal_system_prompt",
        re.compile(
            r"\b(?:reveal|print|show|repeat|output|tell me)\b[^.\n]{0,30}"
            r"\b(?:system prompt|hidden (?:instructions|prompt)|initial instructions)\b"
            r"|(?:顯示|輸出|告訴我|重複|印出)[^。\n]{0,10}(?:系統提示|系統指令|隱藏(?:的)?指令)",
            re.IGNORECASE,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\byou are now\b[^.\n]{0,40}\b(?:DAN|unrestricted|jailbroken|developer mode|"
            r"without (?:any )?(?:rules|restrictions|filters))\b"
            r"|\b(?:developer|god|jailbreak) mode\b"
            r"|\bpretend (?:that )?you (?:have|had) no (?:rules|restrictions|guidelines)\b"
            r"|你現在是[^。\n]{0,20}(?:沒有|不受)(?:任何)?(?:限制|規則|約束)"
            r"|(?:開發者|越獄)模式",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltrate_secrets",
        re.compile(
            r"\b(?:send|post|upload|exfiltrate)\b[^.\n]{0,40}"
            r"\b(?:api keys?|credentials|passwords?|tokens?|secrets?)\b[^.\n]{0,40}"
            r"\b(?:to|into)\b[^.\n]{0,10}(?:https?://|url|webhook|server)"
            r"|(?:把|將)[^。\n]{0,20}(?:金鑰|密碼|憑證|token)[^。\n]{0,20}(?:傳送|上傳|寄)到",
            re.IGNORECASE,
        ),
    ),
)


@dataclass
class InjectionCheck:
    signals: list[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.signals)


def check_injection(texts: Iterable[str]) -> InjectionCheck:
    """Look for common prompt-injection phrasings in user-provided text.

    This is a heuristic tripwire, not a guarantee: it catches the usual
    phrasings in English and Chinese and reports which ones matched.
    """
    joined = "\n".join(texts)
    return InjectionCheck(
        [name for name, pattern in _INJECTION_SIGNALS if pattern.search(joined)]
    )


# Characters at which streamed text can be released without splitting any
# personal-data value: none of the detectors match across them.
_BOUNDARY = re.compile(r"[\s，。、；：！？「」『』（）()]")
# Spaced numbers (cards, phones) contain whitespace but are at most this long.
_HOLD_BACK = 24


def _comparable(value: str) -> str:
    """Normalize a value so reformatting does not make it look new.

    "0912345678", "0912-345-678" and "+886 912 345 678" compare equal, as do
    "4111 1111 1111 1111" and "4111-1111-1111-1111" or e-mail case changes.
    """
    if "@" in value:
        return value.strip().casefold()
    compact = re.sub(r"[\s\-().]", "", value).casefold()
    compact = compact.removeprefix("+")
    if compact.startswith("886") and compact[3:4].isdigit() and len(compact) >= 11:
        compact = "0" + compact[3:]
    return compact


class OutputGuard:
    """Redact personal data in model output that the user never provided.

    Values found in the request (and values the vault restored) are allowed;
    anything else that looks like personal data, such as a phone number or key
    the model produced on its own, is replaced with a ``[LABEL]`` marker. In
    streaming mode text is released only at boundaries no value can cross.

    Names and places (named-entity recognition) are not checked: answers
    mention them all the time ("Tokyo", "Einstein"), and the model cannot leak
    one the user never gave it. Only structured data is: numbers, keys, cards.
    """

    def __init__(self, allowed_values: Iterable[str], *, redact: bool) -> None:
        self._allowed = {_comparable(value) for value in allowed_values if value}
        self._redact = redact
        self._pending = ""
        self.findings: list[str] = []

    def check(self, text: str) -> str:
        """Process a complete text at once."""
        return self._apply(text)

    def feed(self, text: str) -> str:
        self._pending += text
        cut = self._release_point(self._pending)
        if cut <= 0:
            return ""
        released, self._pending = self._pending[:cut], self._pending[cut:]
        return self._apply(released)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._apply(rest)

    def discard_pending(self) -> None:
        """Drop held-back text from an attempt that failed before it was sent."""
        self._pending = ""

    def _release_point(self, buffer: str) -> int:
        limit = len(buffer) - _HOLD_BACK
        if limit <= 0:
            return 0
        boundaries = [m.end() for m in _BOUNDARY.finditer(buffer, 0, limit)]
        if not boundaries:
            return 0
        cut = boundaries[-1]
        # A PEM block spans lines; hold it until its END line has arrived.
        begin = buffer.rfind("-----BEGIN")
        if begin != -1 and "-----END" not in buffer[begin:]:
            cut = min(cut, begin)
        for match in find_personal_data(buffer, entities=False):
            if match.start < cut < match.end:
                cut = match.start
        return cut

    def _apply(self, text: str) -> str:
        if not text:
            return text
        output = text
        for match in reversed(find_personal_data(text, entities=False)):
            if _comparable(match.value) in self._allowed:
                continue
            self.findings.append(match.type)
            if self._redact:
                output = (
                    output[: match.start] + f"[{match.label}]" + output[match.end :]
                )
        return output
