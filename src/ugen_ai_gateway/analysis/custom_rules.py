"""Administrator-defined sensitive terms: words or regular expressions treated
like personal data (redacted before the cloud, restored in the reply), and
optionally kept off the cloud entirely."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ugen_ai_gateway.analysis.pii import PersonalDataMatch, value_pattern


class CustomRule(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    kind: Literal["keyword", "regex"] = "keyword"
    # Keywords: one term per line. Regex: one pattern.
    pattern: str = Field(min_length=1, max_length=2000)
    case_sensitive: bool = False
    keep_local: bool = False

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9 _-]+", value):
            raise ValueError(
                "Use letters, digits, spaces, dashes or underscores in names"
            )
        return value

    @model_validator(mode="after")
    def check_pattern(self) -> "CustomRule":
        if self.kind == "regex":
            if len(self.pattern) > 300:
                raise ValueError("Keep regular expressions under 300 characters")
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"Invalid regular expression: {exc}") from exc
            if compiled.match(""):
                raise ValueError("The regular expression must not match empty text")
        elif not self.terms:
            raise ValueError("Add at least one keyword")
        return self

    @property
    def terms(self) -> list[str]:
        return [term.strip() for term in self.pattern.splitlines() if term.strip()]

    @property
    def type(self) -> str:
        return "custom:" + self.slug

    @property
    def label(self) -> str:
        return self.slug.upper()

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_") or "term"

    def compiled(self) -> re.Pattern[str]:
        flags = 0 if self.case_sensitive else re.IGNORECASE
        if self.kind == "regex":
            return re.compile(self.pattern, flags)
        # Longer terms first, so "Project Falcon X" wins over "Project Falcon".
        alternatives = [
            value_pattern(term).pattern
            for term in sorted(set(self.terms), key=len, reverse=True)
        ]
        return re.compile("|".join(alternatives), flags)


class PrivacySettings(BaseModel):
    # All built-in personal data is always redacted; settings saved by older
    # versions may still carry a "never_cloud" list, which is ignored.
    rules: list[CustomRule] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_names(self) -> "PrivacySettings":
        slugs = [rule.slug for rule in self.rules]
        if len(slugs) != len(set(slugs)):
            raise ValueError("Rule names must be different")
        return self

    def never_cloud_types(self) -> list[str]:
        return [rule.type for rule in self.rules if rule.keep_local]


def rules_finder(rules: list[CustomRule]):
    """A finder for find_personal_data that reports custom-rule matches."""
    compiled = [(rule, rule.compiled()) for rule in rules]

    def find(text: str) -> list[PersonalDataMatch]:
        return [
            PersonalDataMatch(
                rule.type, rule.label, match.start(), match.end(), match.group()
            )
            for rule, pattern in compiled
            for match in pattern.finditer(text)
            if match.end() > match.start()
        ]

    return find
