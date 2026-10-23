import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from ugen_ai_gateway.analysis import ner
from ugen_ai_gateway.services.errors import GatewayConfigurationError


class FakeAnalyzerEngine:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []

    def analyze(self, **kwargs: Any) -> list[Any]:
        self.calls.append(kwargs)
        text = kwargs["text"]
        results = []
        for word, entity in (
            ("王小明", "PERSON"),
            ("台北", "LOCATION"),
            ("王", "PERSON"),
        ):
            start = text.find(word)
            if start != -1:
                results.append(
                    SimpleNamespace(
                        entity_type=entity, start=start, end=start + len(word)
                    )
                )
        return results


@pytest.fixture
def fake_presidio(monkeypatch) -> dict[str, Any]:
    created: dict[str, Any] = {}

    class FakeProvider:
        def __init__(self, nlp_configuration: dict[str, Any]) -> None:
            created["nlp_configuration"] = nlp_configuration

        def create_engine(self) -> str:
            return "nlp-engine"

    def make_engine(**kwargs: Any) -> FakeAnalyzerEngine:
        created["engine"] = FakeAnalyzerEngine(**kwargs)
        return created["engine"]

    analyzer_module = ModuleType("presidio_analyzer")
    analyzer_module.AnalyzerEngine = make_engine  # type: ignore[attr-defined]
    nlp_module = ModuleType("presidio_analyzer.nlp_engine")
    nlp_module.NlpEngineProvider = FakeProvider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "presidio_analyzer", analyzer_module)
    monkeypatch.setitem(sys.modules, "presidio_analyzer.nlp_engine", nlp_module)
    return created


def test_presidio_finder_maps_entities_to_personal_data(fake_presidio: Any) -> None:
    find = ner.build_presidio_finder(
        language="zh", spacy_model="zh_core_web_sm", score_threshold=0.6
    )

    matches = find("我是王小明，住在台北")

    assert [(m.type, m.label, m.value) for m in matches] == [
        ("person_name", "PERSON_NAME", "王小明"),
        ("location", "LOCATION", "台北"),
    ]
    assert fake_presidio["nlp_configuration"]["models"] == [
        {"lang_code": "zh", "model_name": "zh_core_web_sm"}
    ]
    call = fake_presidio["engine"].calls[0]
    assert call["score_threshold"] == 0.6
    assert set(call["entities"]) == {"PERSON", "LOCATION"}


def test_missing_presidio_gives_an_install_hint(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)

    with pytest.raises(GatewayConfigurationError, match="--extra ner"):
        ner.build_presidio_finder(language="en", spacy_model="m", score_threshold=0.5)


def test_several_languages_run_only_on_their_script(fake_presidio: Any) -> None:
    find = ner.build_presidio_finder(
        language="zh,en",
        spacy_model="zh_core_web_sm,en_core_web_sm",
        score_threshold=0.5,
    )
    assert fake_presidio["nlp_configuration"]["models"] == [
        {"lang_code": "zh", "model_name": "zh_core_web_sm"},
        {"lang_code": "en", "model_name": "en_core_web_sm"},
    ]
    engine = fake_presidio["engine"]

    find("王小明住在台北")
    assert [call["language"] for call in engine.calls] == ["zh"]
    engine.calls.clear()
    find("王小明 met Alex in 台北")
    assert [call["language"] for call in engine.calls] == ["zh", "en"]
    engine.calls.clear()
    find("just English here")
    assert [call["language"] for call in engine.calls] == ["en"]


def test_language_and_model_lists_must_match(fake_presidio: Any) -> None:
    with pytest.raises(GatewayConfigurationError, match="same number"):
        ner.build_presidio_finder(
            language="zh,en", spacy_model="zh_core_web_sm", score_threshold=0.5
        )
