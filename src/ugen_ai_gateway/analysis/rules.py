"""Fast rule-based request analysis that needs no model call."""

import re

from ugen_ai_gateway.analysis.pii import detect_personal_data
from ugen_ai_gateway.analysis.schemas import RequestAnalysis, TaskType

_TASK_CUES: tuple[tuple[TaskType, re.Pattern[str]], ...] = (
    (
        "coding",
        re.compile(
            r"```|\bdef |\bclass |\bfunction\b|\bimport |traceback|exception|"
            r"\b(?:python|javascript|typescript|java|golang|rust|sql|regex|bug|"
            r"compile|stack trace)\b|程式|程式碼|函式|除錯|報錯",
            re.IGNORECASE,
        ),
    ),
    (
        "translation",
        re.compile(r"\btranslat|翻譯|譯成|翻成", re.IGNORECASE),
    ),
    (
        "summarization",
        re.compile(r"\bsummar|\btl;?dr\b|摘要|總結|重點整理|歸納", re.IGNORECASE),
    ),
    (
        "reasoning",
        re.compile(r"\bwhy\b|\bprove\b|\breason|為什麼|推理|證明|邏輯", re.IGNORECASE),
    ),
)
_MODERATE_LENGTH = 280


def rule_analysis(user_input: str) -> RequestAnalysis:
    """Classify a request with regular expressions only.

    Complexity here is only by length; the gateway replaces it with
    ``complexity.assess_complexity``, which reads the request's wording.
    """
    personal_data_types = detect_personal_data(user_input)
    task_type: TaskType = next(
        (task for task, pattern in _TASK_CUES if pattern.search(user_input)),
        "general",
    )
    return RequestAnalysis(
        contains_personal_data=bool(personal_data_types),
        personal_data_types=personal_data_types,
        complexity="moderate" if len(user_input) >= _MODERATE_LENGTH else "simple",
        task_type=task_type,
        reason="Rule-based analysis.",
    )
