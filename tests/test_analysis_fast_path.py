import pytest

from ugen_ai_gateway.analysis import cache as cache_module
from ugen_ai_gateway.analysis.cache import AnalysisCache
from ugen_ai_gateway.analysis.rules import rule_analysis
from ugen_ai_gateway.analysis.schemas import RequestAnalysis


@pytest.mark.parametrize(
    ("text", "task_type"),
    [
        ("Why does this fail?\n```python\nprint(x)\n```", "coding"),
        ("請把這段翻譯成英文", "translation"),
        ("幫我摘要這篇文章", "summarization"),
        ("為什麼天空是藍的", "reasoning"),
        ("hello there", "general"),
    ],
)
def test_rule_analysis_detects_task_type(text: str, task_type: str) -> None:
    assert rule_analysis(text).task_type == task_type


def test_rule_analysis_reports_regex_personal_data_and_length() -> None:
    result = rule_analysis("call 0912-345-678 " + "x" * 300)

    assert result.contains_personal_data is True
    assert result.personal_data_types == ["taiwan_mobile"]
    assert result.complexity == "moderate"


def make_analysis() -> RequestAnalysis:
    return RequestAnalysis(
        contains_personal_data=False,
        complexity="simple",
        task_type="general",
        reason="r",
    )


def test_cache_expires_entries_after_ttl(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    cache = AnalysisCache(max_size=4, ttl_seconds=10)
    cache.put("a", make_analysis())

    now[0] = 109.0
    assert cache.get("a") is not None
    now[0] = 111.0
    assert cache.get("a") is None


def test_cache_evicts_least_recently_used() -> None:
    cache = AnalysisCache(max_size=2, ttl_seconds=60)
    cache.put("a", make_analysis())
    cache.put("b", make_analysis())
    cache.get("a")
    cache.put("c", make_analysis())

    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_cache_disabled_when_size_is_zero() -> None:
    cache = AnalysisCache(max_size=0, ttl_seconds=60)
    cache.put("a", make_analysis())

    assert cache.get("a") is None
