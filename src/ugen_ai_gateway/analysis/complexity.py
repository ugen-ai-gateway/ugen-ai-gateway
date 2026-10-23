"""How demanding a request is, judged from its text alone.

Keyword rules cannot tell "幫我設計一句生日祝福" from "幫我設計一個會員系統"
in every case, so they only decide when the signal is unambiguous: a greeting,
a one-line factual question, a short rewrite or translation, a definition, or
an explicit design, proof or algorithm task. Everything in between is marked
uncertain and left to the analysis model. A short follow-up ("第二點再詳細一點",
"continue") takes the difficulty of the request it follows.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Complexity = Literal["simple", "moderate", "complex"]

# Labeled requests for checking the rules (tests) and the analysis model
# (python -m ugen_ai_gateway.cli check-routing).
CASES_FILE = Path(__file__).with_name("complexity_cases.json")


@dataclass(frozen=True)
class Assessment:
    complexity: Complexity
    # True when the rules are sure; otherwise the analysis model decides.
    certain: bool
    reason: str
    # A follow-up that only makes sense together with the previous message.
    follow_up: bool = False


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(patterns), re.IGNORECASE)


# Demanding on their own: design and architecture, proofs, algorithms,
# debugging, planning under constraints.
_HARD = _any(
    r"architect(?:ure|ural)|system design|trade[ -]?offs?|step[ -]?by[ -]?step|"
    r"comprehensive|in[ -]depth|thorough|multi[ -]?step|refactor|debug|"
    r"distributed|scalab|high[ -]availability|concurren|race condition|deadlock|"
    r"memory leak|time complexity|big[ -]o\b|\bprove\b|\bproof\b|theorem|derive|"
    r"\bcrdts?\b|\braft\b|\bpaxos\b|consensus algorithm|\bp ?(?:=|vs\.?|versus) ?np\b|"
    r"\b(?:design|build|implement|architect|write|create|develop) (?:a|an|the|my|our) "
    r"(?:[\w-]+ ){0,3}(?:system|service|api|backend|database|schema|architecture|"
    r"pipeline|protocol|limiter|cache|queue|platform|compiler|interpreter|engine|"
    r"scheduler|parser|framework|microservices?|app|application)\b|"
    r"\bimplement(?:ing)? (?:an? |the )?(?:[\w-]+ ){0,3}(?:tree|graph|heap|trie|"
    r"cache|queue|algorithm|sort|search|hash ?map|linked list|lru|lfu|parser)\b|"
    r"\b(?:lru|lfu) cache\b|re-?render|stack ?trace|traceback|segfault|"
    r"business plan|go-to-market|financial model|due diligence|"
    r"架構(?!是什麼|是甚麼|的意思)|取捨|權衡|逐步|一步一步|多步驟|重構|除錯|偵錯|"
    r"完整方案|全面分析|深入分析|詳細分析|分散式|高可用|高併發|併發|競態|死結|記憶體洩漏|"
    r"時間複雜度|證明|推導|演算法|資料結構|商業計畫|營運計畫|可行性(?:分析|評估)|"
    r"風險評估|預算(?:規劃|編列)|時程(?:規劃|安排)|"
    r"(?:設計|建置|規劃|開發|實作|架設)[^，。、；：！？,.;:!?\n]{0,40}?"
    r"(?:系統|資料庫|API|後端|架構|演算法|平台|服務|網站|App|應用程式|微服務|流程)",
)
# Everyday task words: demanding in some requests, casual in others.
_TASK_WORDS = _any(
    r"\b(?:designs?|designed|designing|compar(?:e|es|ed|ing|isons?)|"
    r"evaluat(?:e|es|ed|ing|ions?)|analy(?:[sz]e|[sz]es|[sz]ed|[sz]ing|sis|ses)|"
    r"implement(?:s|ed|ing|ations?)?|optimi[sz](?:e|es|ed|ing|ations?)|"
    r"strateg(?:y|ies)|plan(?:s|ning)?|explain|why|how (?:does|do|can|should|would)|"
    r"pros and cons|difference between|recommend)\b|"
    r"設計|比較|評估|規劃|策略|整合|優化|分析|解釋|說明|為什麼|為何|怎麼做|如何|"
    r"優缺點|差別|差異|建議|計畫|計劃",
)
# Short tasks a small model does well whatever their topic.
_LIGHT_TASK = _any(
    r"\btranslat|\bin (?:english|chinese|japanese)\b|翻譯|譯成|翻成|換成(?:英文|中文|日文)|"
    r"\b(?:rewrite|rephrase|paraphrase|polish|proofread|shorten|simplify)\b|"
    r"\bfix (?:the |my )?(?:typos?|grammar|spelling)|\bmake it (?:shorter|more formal|"
    r"friendlier|polite)|潤飾|潤稿|改寫|換句話說|修飾|錯字|文法|縮短|精簡|"
    r"\b(?:summari[sz]e|tl;?dr)\b|摘要|總結|重點整理|"
    r"祝福|賀詞|生日|卡片|標語|slogan|caption|hashtag|取名|起名|暱稱|"
    r"\bname (?:ideas|suggestions)|\bbirthday|\bwish(?:es)?\b|\bjoke\b|笑話",
)
# Writing to a brief: an email, letter or report with requirements.
_COMPOSITION = _any(
    r"\b(?:e-?mail|letter|cover letter|report|essay|article|proposal|speech|"
    r"announcement|press release|blog post|resume|cv)\b|"
    r"一封|信件|求職信|自傳|履歷|報告|文章|企劃|提案|講稿|演講稿|公告|新聞稿|心得|讀書心得|"
    r"文案|貼文|廣告|宣傳|介紹|簡介|\b(?:post|copy|ad|ads|product description|bio)\b",
)
# Asking what something is: simple even when the topic is not.
# A definition question that also asks for more ("…並舉例說明").
_MORE_ASKED = _any(
    r"\b(?:explain|compare|difference|versus|vs\.?|why|how|examples?|pros|cons|"
    r"trade-?offs?|in detail|detailed|walk me through|step)\b|"
    r"說明|解釋|比較|差別|差異|舉例|例子|為什麼|為何|怎麼|如何|優缺點|取捨|詳細|步驟|原理",
)
_DEFINITION = _any(
    r"^\s*(?:what(?:'s| is| are| does)|who (?:is|was)|define|meaning of)\b(?!.*"
    r"\b(?:difference|between|compare|versus|vs\.?)\b)",
    r"^\s*(?:什麼是|甚麼是|何謂|請問什麼是)",
    r"(?:由來|起源|典故|怎麼來的|字源|縮寫)",
    r"\b(?:origin|etymology|stands? for|abbreviation)\b",
    r"(?:是什麼|是甚麼|是誰|的意思|什麼意思|甚麼意思|的定義)\s*[?？。!！]*\s*$",
)
_CHITCHAT = _any(
    r"^\s*(?:hi|hello|hey|yo|good (?:morning|afternoon|evening|night)|thanks?(?: you)?|"
    r"thx|ok(?:ay)?|cool|great|nice|got it|bye|你好|您好|哈囉|嗨|早安|午安|晚安|謝謝|"
    r"感謝|好的|好|了解|收到|沒問題|掰掰|再見|讚)[\s!！.。~～?？,，]*$",
    r"^\s*(?:你是誰|你叫什麼|who are you|what can you do|你會什麼|你能做什麼)"
    r"[\s?？!！。]*$",
)
# A message that only makes sense with the one before it.
_FOLLOW_UP = _any(
    r"^\s*(?:繼續|接著|然後呢|還有呢|再來|下一步|go on|continue|keep going|and then|"
    r"next)[\s!！.。?？]*$",
    r"詳細|展開|再.{0,6}(?:說明|解釋|一點|一些|多一點)|第[一二三四五六七八九十\d]+(?:點|步|項|部分|段|個)|"
    r"那如果|如果改|改用|改成|換用|那(?:要|該)?怎麼|為什麼要這樣|這(?:個|樣|段)|上面|剛剛|剛才|"
    r"\bmore detail|\belaborate|\bexpand\b|\bstep \d|\bwhat about\b|\bhow about\b|"
    r"\band if\b|\bwhat if\b|\binstead\b|\bthat\b|\bthis one\b|\babove\b|\bpart \d",
)
_ACK_WORD = (
    r"(?:thanks?(?: you| a lot| so much)?|thx|ty|ok(?:ay)?|great|perfect|got it|cool|"
    r"nice|awesome|謝謝|謝啦|謝囉|感謝|感恩|好的|好喔|好|了解了?|收到|讚|太好了|沒問題|懂了|"
    r"很清楚|清楚了|明白了?|很棒|很有幫助|helpful|makes sense|"
    r"OK)"
)
_ACKNOWLEDGEMENT = re.compile(
    rf"^\s*{_ACK_WORD}(?:[\s,，!！.。~～]*{_ACK_WORD})*[\s!！.。~～,，]*$",
    re.IGNORECASE,
)
# Work a small model often gets wrong: code to write, calculations to show,
# and advice on a specific medical, legal or financial situation.
# Asks for a short answer.
_BRIEF = _any(
    r"\b(?:briefly|in short|keep it short|short answer|in one sentence|eli5|simply)\b|"
    r"簡單說|簡單講|簡短|一句話|國小程度|小學生|白話",
)
_SUBSTANTIAL = _any(
    r"\b(?:write|create|build|make) (?:a|an|me a) (?:[\w-]+ ){0,3}(?:function|script|"
    r"component|class|program|scraper|bot|query|regex|game|cover letter)\b|"
    r"\bfix (?:this|my|the) (?:code|regex|query|bug)|\bshow (?:the |your )?(?:work|steps)|"
    r"\b(?:doctor|prescri|medication|dose|diagnos|symptom|lawyer|legal|contract|"
    r"enforceable|lawsuit|sue|tax|equity|vest|mortgage|invest)\w*|"
    r"寫(?:一個|個|一支|支)[^，。\n]{0,20}(?:函式|函數|程式|腳本|爬蟲|元件|組件|遊戲|API|SQL|類別)|"
    r"算式|計算過程|列式|步驟|流程|時間表|"
    r"醫生|醫師|藥|劑量|症狀|病|診斷|律師|法律|合約|契約|違法|告|勞基法|押金|稅|保險|"
    r"投資|股票|貸款|房貸|退休金",
)
_CODE = re.compile(
    r"```|^\s{4,}\S|\b(?:def|class|function|import|return)\b|[{};]\s*$", re.MULTILINE
)
# Asks for code to be written or tested.
_BUILD = _any(
    r"實作|寫(?:一個|個|一支)?程式|撰寫程式|程式碼|函式|單元測試|測試案例|"
    r"\bimplement|\bwrite (?:the |a |some )?(?:code|function|class|module|tests?)\b|"
    r"\bunit tests?\b|\bpytest\b",
)
_LIST_ITEM = re.compile(r"(?:^|\n)\s*(?:\d+[.)、]|[-*•])\s*\S")
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def _size(text: str) -> int:
    """Length with each CJK character counted twice (Chinese says more per
    character)."""
    return len(text) + len(_CJK.findall(text))


def _instruction_lines(text: str) -> list[str] | None:
    """The lines of a longer multi-line message that can hold its instruction:
    its first and last line when they are short (possibly none). None for a
    message read as a whole: a single line, or a short message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or _size(text) < 200:
        return None
    return [line for line in dict.fromkeys((lines[0], lines[-1])) if _size(line) <= 160]


def assess_complexity(text: str, previous: str | None = None) -> Assessment:
    """Judge ``text``, the latest user message; ``previous`` is the user
    message before it, if any."""
    stripped = text.strip()
    size = _size(stripped)

    if previous and size <= 60:
        if _ACKNOWLEDGEMENT.search(stripped):
            return Assessment("simple", True, "An acknowledgement.")
        if _FOLLOW_UP.search(stripped) and not _LIGHT_TASK.search(stripped):
            earlier = assess_complexity(previous)
            return Assessment(
                earlier.complexity,
                earlier.certain,
                "A follow-up to the previous request, judged with it.",
                follow_up=True,
            )

    if _CHITCHAT.search(stripped) or _ACKNOWLEDGEMENT.search(stripped):
        return Assessment("simple", True, "Small talk.")

    # In a longer multi-line message the instruction is a short first or last
    # line ("翻譯成英文：…" or "…\n請把上面翻譯成英文"); the lines between may
    # be pasted text whose words ask for nothing. Design or debugging words
    # there do not make the rules sure; the analysis model decides.
    edges = _instruction_lines(stripped)
    transform = edges is not None and any(
        _LIGHT_TASK.search(line) and not _HARD.search(line) for line in edges
    )
    if edges is None:
        hard = _HARD.search(stripped)
    else:
        hard = None if transform else _HARD.search("\n".join(edges))
    has_code = bool(_CODE.search(stripped))
    # Only when nothing else is asked ("什麼是 CAP 定理？請舉例說明取捨" is more).
    definition = (
        bool(_DEFINITION.search(stripped))
        and size <= 80
        and not _MORE_ASKED.search(stripped)
        and len(re.findall(r"[?？]", stripped)) <= 1
    )

    if definition and not has_code:
        return Assessment("simple", True, "Asks what something is.")
    if hard and not (_LIGHT_TASK.search(stripped) and size <= 120):
        return Assessment(
            "complex", True, "Design, proof, algorithm or debugging work."
        )
    # A long brief listing several things to build: a specification to
    # implement, whatever its individual words.
    if (
        size >= 400
        and len(_LIST_ITEM.findall(stripped)) >= 3
        and _BUILD.search(stripped)
        and not transform
    ):
        return Assessment("complex", True, "A multi-part brief to implement.")

    light = bool(_LIGHT_TASK.search(stripped))
    task_words = {word.lower() for word in _TASK_WORDS.findall(stripped)}
    list_items = len(_LIST_ITEM.findall(stripped))

    composing = bool(_COMPOSITION.search(stripped))
    if (
        light
        and not composing
        and not has_code
        and size <= 600
        and len(task_words) <= 1
    ):
        return Assessment("simple", True, "A short rewrite, translation or summary.")

    # Uncertain: the analysis model decides. Below is the best guess for
    # rules-only mode.
    substantial = bool(_SUBSTANTIAL.search(stripped))
    if size >= 1400 or list_items >= 4 or len(task_words) >= 3:
        guess: Complexity = "complex"
    elif substantial and size >= 120:
        guess = "complex"
    elif (
        _BRIEF.search(stripped)
        and not has_code
        and size < 160
        or light
        and not composing
        and not has_code
    ):
        guess = "simple"
    elif has_code or substantial or composing or size >= 160 or task_words:
        guess = "moderate"
    else:
        guess = "simple"
    if previous and size <= 80:
        # A short message after another one is judged with it.
        earlier = assess_complexity(previous)
        order = ("simple", "moderate", "complex")
        guess = max(guess, earlier.complexity, key=order.index)
        return Assessment(
            guess, False, "A follow-up to the previous request.", follow_up=True
        )
    return Assessment(guess, False, "No clear signal; judged by the analysis model.")
