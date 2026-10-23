import asyncio
import json

import pytest

from tests.test_gateway import FakeAnalyzer, analysis, make_service
from ugen_ai_gateway.analysis.complexity import CASES_FILE, assess_complexity
from ugen_ai_gateway.api.schemas import ChatRequest

# Realistic requests (600, in two batches by different authors) labeled by whether a 7-9B local model answers them well
# (simple) or a stronger model should (moderate, complex). Written separately
# from the rules, in Chinese and English, with deliberately tricky cases.
CASES = json.loads(CASES_FILE.read_text("utf-8"))


def test_rules_are_never_wrong_when_they_are_sure() -> None:
    """Routing depends on simple versus not simple; when the rules decide on
    their own they must agree with the label every time."""
    wrong = []
    certain = 0
    for case in CASES:
        result = assess_complexity(case["text"], case["previous"])
        if result.certain:
            certain += 1
            if (result.complexity == "simple") != (case["label"] == "simple"):
                wrong.append((case["text"], result.complexity, case["label"]))
    assert wrong == []
    # The rest goes to the analysis model; the rules still settle a good share.
    assert certain / len(CASES) >= 0.3


def test_rules_only_mode_gets_most_requests_right() -> None:
    wrong = [
        case
        for case in CASES
        if (assess_complexity(case["text"], case["previous"]).complexity == "simple")
        != (case["label"] == "simple")
    ]
    # Rules-only mode guesses on unclear requests; the default asks the model.
    assert len(wrong) / len(CASES) <= 0.2


@pytest.mark.parametrize(
    ("text", "previous", "complexity", "certain"),
    [
        ("你好", None, "simple", True),
        ("幫我設計一句生日祝福", None, "simple", True),
        ("什麼是微服務", None, "simple", True),
        ("What is system design?", None, "simple", True),
        ("幫我設計一個會員系統", None, "complex", True),
        ("證明根號2是無理數", None, "complex", True),
        ("implement an LRU cache", None, "complex", True),
        # A multi-line brief is read as a whole, not just its first line.
        (
            "請設計一套適用於資源受限環境的 AI Gateway 路由判定系統，使用 Python 實作。\n"
            "需求：\n1. 實作 classify_by_rules(text)\n2. 實作 classify_request(text)\n"
            "3. 提供至少 20 個 pytest 測試案例，並用 mock 驗證模型呼叫次數\n"
            + "環境限制：本地模型可能逾時或無法連線，必須有安全降級策略。"
            * 4,
            None,
            "complex",
            True,
        ),
        ("設計一套可容錯的微服務架構", None, "complex", True),
        # Rewriting a pasted passage: only the instruction line counts.
        (
            "幫我翻譯成英文：\n" + "我們設計了一套分散式系統，處理高併發與容錯。" * 8,
            None,
            "simple",
            True,
        ),
        ("第二點再詳細一點", "幫我設計一個會員系統", "complex", True),
        ("謝謝，很清楚！", "幫我設計一個會員系統", "simple", True),
        # No clear signal: the analysis model decides.
        ("為什麼 TCP 需要三次握手而不是兩次", None, "moderate", False),
        ("solve this", None, "simple", False),
    ],
)
def test_examples(
    text: str, previous: str | None, complexity: str, certain: bool
) -> None:
    result = assess_complexity(text, previous)
    assert (result.complexity, result.certain) == (complexity, certain)


def _ask(service, *texts: str):
    messages = []
    for index, text in enumerate(texts):
        if index:
            messages.append({"role": "assistant", "content": "…"})
        messages.append({"role": "user", "content": text})
    return asyncio.run(service.chat(ChatRequest(messages=messages)))


def test_hybrid_skips_the_model_only_when_the_rules_are_sure() -> None:
    service, _, _, analyzer = make_service(
        analyzer=FakeAnalyzer(analysis(complexity="complex")), analysis_mode="hybrid"
    )

    sure = _ask(service, "謝謝！")
    unsure = _ask(service, "為什麼 TCP 需要三次握手")

    assert analyzer.inputs == ["為什麼 TCP 需要三次握手"]
    assert sure.routing.analysis_source == "rules"
    assert sure.provider == "local"
    # When the rules are unsure, the model's judgement decides.
    assert unsure.routing.complexity == "complex"
    assert unsure.provider == "cloud"


def test_the_rules_overrule_the_model_when_they_are_sure() -> None:
    service, _, _, _ = make_service(
        analyzer=FakeAnalyzer(analysis(complexity="simple")),
        analysis_mode="llm",
    )

    response = _ask(service, "幫我設計一個會員系統")

    assert response.routing.complexity == "complex"
    assert response.provider == "cloud"


def test_an_unclear_follow_up_is_judged_with_the_request_before_it() -> None:
    service, _, _, analyzer = make_service(
        analyzer=FakeAnalyzer(analysis(complexity="complex")), analysis_mode="hybrid"
    )

    _ask(service, "我想開早午餐店，預算 250 萬", "如果預算砍到 150 萬要怎麼改？")

    assert (
        "我想開早午餐店，預算 250 萬\n\n如果預算砍到 150 萬要怎麼改？"
        in analyzer.inputs
    )


ARTICLE = (
    "我們團隊去年完成了一套分散式系統的重構，處理高併發與容錯，也做了除錯與效能優化。"
    * 6
)
CODE = "def add(a, b):\n    return a - b\n\nprint(add(1, 2))\n" * 3


@pytest.mark.parametrize(
    ("text", "complexity", "certain"),
    [
        # The instruction may come first or last; pasted text in between asks
        # for nothing, whatever words it contains.
        ("請把以下翻譯成英文：\n" + ARTICLE, "simple", True),
        (ARTICLE + "\n\n請把上面這段翻譯成英文", "simple", True),
        (
            "We rebuilt our distributed system and debugged race conditions. " * 6
            + "\n\nTranslate the above into Traditional Chinese.",
            "simple",
            True,
        ),
        # Hard words only inside pasted text never make the rules sure.
        (ARTICLE + "\n\n這篇在講什麼？", "moderate", False),
        (
            "我們公司今年要導入新的會員系統，需要和舊的 POS 與客服系統整合，" * 5
            + "\n"
            + "另外要注意資料遷移、權限控管與高併發時的穩定性。" * 5,
            "moderate",
            False,
        ),
        # Explaining code is for the model to judge; debugging it is hard work.
        (CODE + "\n請解釋這段程式碼在做什麼", "moderate", False),
        (CODE + "\n幫我除錯這段程式", "complex", True),
        (
            "背景：我們是 20 人的電商團隊。\n"
            + ARTICLE
            + "\n請幫我設計一套可容錯的微服務架構",
            "complex",
            True,
        ),
        # A translation that also asks for analysis is not a plain rewrite.
        (ARTICLE + "\n\n翻譯成英文，然後分析它的論證並提出反駁", "simple", False),
    ],
)
def test_the_instruction_line_decides_not_pasted_text(
    text: str, complexity: str, certain: bool
) -> None:
    result = assess_complexity(text)
    assert (result.complexity, result.certain) == (complexity, certain)
