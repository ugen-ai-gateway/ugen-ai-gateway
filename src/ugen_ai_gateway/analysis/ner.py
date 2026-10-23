"""Optional named-entity detection (people, places) through Microsoft Presidio.

Install with ``uv sync --extra ner`` plus a spaCy model for your language, for
example ``uv run python -m spacy download zh_core_web_sm``.
"""

import re
from typing import Any

from ugen_ai_gateway.analysis.pii import Finder, PersonalDataMatch
from ugen_ai_gateway.services.errors import GatewayConfigurationError

# Presidio entity -> (personal-data type, placeholder label)
ENTITY_TYPES: dict[str, tuple[str, str]] = {
    "PERSON": ("person_name", "PERSON_NAME"),
    "LOCATION": ("location", "LOCATION"),
}


# Which script a language's model reads, so a model only runs on text it can
# understand (an English model learns nothing from Chinese text, and back).
_SCRIPTS = {
    "zh": re.compile(r"[\u3400-\u9fff]"),
    "ja": re.compile(r"[\u3040-\u30ff\u3400-\u9fff]"),
    "ko": re.compile(r"[\uac00-\ud7af]"),
}
_LATIN = re.compile(r"[A-Za-z]")


def build_presidio_finder(
    *, language: str, spacy_model: str, score_threshold: float
) -> Finder:
    """A finder for people and places. ``language`` and ``spacy_model`` may
    list several, comma-separated and in the same order (``zh,en`` with
    ``zh_core_web_sm,en_core_web_sm``) for text that mixes languages."""
    languages = [code.strip() for code in language.split(",") if code.strip()]
    models = [name.strip() for name in spacy_model.split(",") if name.strip()]
    if not languages or len(languages) != len(models):
        raise GatewayConfigurationError(
            "PII_NER_LANGUAGE and PII_NER_SPACY_MODEL need the same number of "
            "comma-separated entries"
        )
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError as exc:
        raise GatewayConfigurationError(
            "PII_NER=presidio needs the optional dependency: uv sync --extra ner"
        ) from exc

    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": code, "model_name": model}
                for code, model in zip(languages, models, strict=True)
            ],
        }
    ).create_engine()
    engine: Any = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=languages)

    def find(text: str) -> list[PersonalDataMatch]:
        matches: list[PersonalDataMatch] = []
        for code in languages:
            script = _SCRIPTS.get(code.split("_")[0], _LATIN)
            if len(languages) > 1 and not script.search(text):
                continue
            results = engine.analyze(
                text=text,
                language=code,
                entities=list(ENTITY_TYPES),
                score_threshold=score_threshold,
            )
            for result in results:
                data_type, label = ENTITY_TYPES[result.entity_type]
                value = text[result.start : result.end]
                overlaps = any(
                    result.start < other.end and other.start < result.end
                    for other in matches
                )
                if len(value.strip()) >= 2 and not overlaps:
                    matches.append(
                        PersonalDataMatch(
                            data_type, label, result.start, result.end, value
                        )
                    )
        return sorted(matches, key=lambda match: match.start)

    return find
