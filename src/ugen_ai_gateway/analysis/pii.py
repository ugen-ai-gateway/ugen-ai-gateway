import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

# Boundaries are ASCII-only on purpose: in Python, \w also matches CJK
# characters, so "(?<!\w)" would miss "電話0912345678".
_NOT_AFTER_WORD = r"(?<![A-Za-z0-9_])"
_NOT_BEFORE_WORD = r"(?![A-Za-z0-9_])"

EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.+-])[A-Za-z0-9_+-]+(?:\.[A-Za-z0-9_+-]+)*@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?![A-Za-z0-9_-])"
)
TAIWAN_MOBILE_PATTERN = re.compile(
    _NOT_AFTER_WORD
    + r"(?:09\d{2}|(?:\+?886[- ]?)9\d{2})[- ]?\d{3}[- ]?\d{3}"
    + _NOT_BEFORE_WORD
)
# Area code 02-08 (or +886 2-8), then a 6-8 digit subscriber number.
TAIWAN_LANDLINE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_+])"
    r"(?:\(0[2-8]\d{0,2}\)|0[2-8]\d{0,2}|\+?886[- ]?\(?[2-8]\d{0,2}\)?)"
    r"[- ]?\d{2,4}[- ]?\d{4}" + _NOT_BEFORE_WORD
)
TAIWAN_ID_PATTERN = re.compile(
    _NOT_AFTER_WORD + r"[A-Z][12]\d{8}" + _NOT_BEFORE_WORD, re.IGNORECASE
)
# Resident certificate, new format: letter + 8/9 + 8 digits.
TAIWAN_ARC_PATTERN = re.compile(
    _NOT_AFTER_WORD + r"[A-Z][89]\d{8}" + _NOT_BEFORE_WORD, re.IGNORECASE
)
# Old format (two letters + 8 digits) looks exactly like a 統一發票 number, so
# it only counts right after a keyword naming it.
# The keyword may be followed by a few words first: "居留證號碼是",
# "ARC number is", "Alien Resident Certificate (ARC) No.:".
TAIWAN_OLD_ARC_PATTERN = re.compile(
    r"(?:居留證|居留|統一證號|\bARC\b|\bUI No\b|resident (?:permit|certificate))"
    r"[^\n]{0,24}?(?<![A-Za-z0-9])(?P<value>[A-Z][A-D]\d{8})" + _NOT_BEFORE_WORD,
    re.IGNORECASE,
)
CREDIT_CARD_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
# Short numeric identifiers only count right after a keyword naming them.
TAIWAN_UBN_PATTERN = re.compile(
    r"(?:統一編號|統編|UBN|VAT(?: number)?|tax id)\s*[:：#]?\s*(?P<value>\d{8})(?!\d)",
    re.IGNORECASE,
)
PASSPORT_PATTERN = re.compile(
    r"(?:護照(?:號碼|號)?|passport(?: (?:no\.?|number))?)\s*[:：#]?\s*"
    r"(?P<value>[A-Z0-9]{6,9})" + _NOT_BEFORE_WORD,
    re.IGNORECASE,
)
NHI_CARD_PATTERN = re.compile(
    r"(?:健保卡(?:號|號碼)?|NHI(?: card)?(?: number)?)\s*[:：#]?\s*"
    r"(?P<value>\d{4}[- ]?\d{4}[- ]?\d{4})(?!\d)",
    re.IGNORECASE,
)
API_KEY_PATTERN = re.compile(
    _NOT_AFTER_WORD
    + r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}"
    r"|AIza[0-9A-Za-z_-]{35}|xox[abprs]-[A-Za-z0-9-]{10,})"
)
JWT_PATTERN = re.compile(
    _NOT_AFTER_WORD
    + r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )*PRIVATE KEY-----"
)
PASSWORD_PATTERN = re.compile(
    r"(?:password|passwd|pwd|passcode|密碼)\s*[:=：]\s*(?P<value>[^\s,;，；]{4,})",
    re.IGNORECASE,
)

_TAIWAN_ID_CODES = {
    "A": 10,
    "B": 11,
    "C": 12,
    "D": 13,
    "E": 14,
    "F": 15,
    "G": 16,
    "H": 17,
    "I": 34,
    "J": 18,
    "K": 19,
    "L": 20,
    "M": 21,
    "N": 22,
    "O": 35,
    "P": 23,
    "Q": 24,
    "R": 25,
    "S": 26,
    "T": 27,
    "U": 28,
    "V": 29,
    "W": 32,
    "X": 30,
    "Y": 31,
    "Z": 33,
}
_ID_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1, 1)


def _letter_checksum(letter: str) -> int | None:
    code = _TAIWAN_ID_CODES.get(letter.upper())
    return None if code is None else code // 10 + (code % 10) * 9


def _is_valid_taiwan_id(value: str) -> bool:
    """National ID and new-format resident certificate (second digit 8 or 9)."""
    checksum = _letter_checksum(value[0])
    if checksum is None or not value[1:].isdigit():
        return False
    digits = [int(digit) for digit in value[1:]]
    checksum += sum(digit * weight for digit, weight in zip(digits, _ID_WEIGHTS))
    return checksum % 10 == 0


def _is_valid_resident_certificate(value: str) -> bool:
    if value[1].isdigit():
        return value[1] in "89" and _is_valid_taiwan_id(value)
    checksum = _letter_checksum(value[0])
    gender_code = _TAIWAN_ID_CODES.get(value[1].upper())
    if checksum is None or gender_code is None:
        return False
    digits = [gender_code % 10, *(int(digit) for digit in value[2:])]
    checksum += sum(digit * weight for digit, weight in zip(digits, _ID_WEIGHTS))
    return checksum % 10 == 0


# A Luhn check alone passes 1 in 10 random numbers, such as tracking or order
# numbers. Payment cards start with 2 (Mastercard 2221-2720), 3 (Amex, Diners,
# JCB), 4 (Visa), 5 (Mastercard, Maestro) or 6 (Discover, UnionPay, Maestro).
_CARD_PREFIXES = re.compile(
    r"(?:222[1-9]|22[3-9]\d|2[3-6]\d{2}|27[01]\d|2720|[3-6]\d{3})\d+"
)


def _is_valid_credit_card(value: str) -> bool:
    digits = [int(digit) for digit in value if digit.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    if not _CARD_PREFIXES.fullmatch("".join(map(str, digits))):
        return False

    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _is_valid_ubn(value: str) -> bool:
    """Taiwan business ID (統一編號) checksum, including the 7th-digit-7 rule."""
    products = [
        int(digit) * weight for digit, weight in zip(value, (1, 2, 1, 2, 1, 2, 4, 1))
    ]
    total = sum(product // 10 + product % 10 for product in products)
    return total % 5 == 0 or (value[6] == "7" and (total + 1) % 5 == 0)


def _is_landline(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("886"):
        digits = "0" + digits[3:]
    return 9 <= len(digits) <= 10 and not digits.startswith("09")


@dataclass(frozen=True)
class Detector:
    """A personal-data type: how to find it and how to label it.

    When the pattern has a ``value`` group, only that part is reported (and
    redacted), so a keyword such as "密碼:" stays readable.
    """

    type: str
    label: str
    pattern: re.Pattern[str]
    validate: Callable[[str], bool] | None = None

    def matches(self, text: str) -> Iterator[tuple[int, int, str]]:
        has_value_group = "value" in self.pattern.groupindex
        for match in self.pattern.finditer(text):
            group = "value" if has_value_group else 0
            value = match.group(group)
            if self.validate is None or self.validate(value):
                yield match.start(group), match.end(group), value


@dataclass(frozen=True)
class PersonalDataMatch:
    type: str
    label: str
    start: int
    end: int
    value: str


DETECTORS: tuple[Detector, ...] = (
    Detector("private_key", "PRIVATE_KEY", PRIVATE_KEY_PATTERN),
    Detector("api_key", "API_KEY", API_KEY_PATTERN),
    Detector("jwt", "TOKEN", JWT_PATTERN),
    Detector("password", "PASSWORD", PASSWORD_PATTERN),
    Detector("email", "EMAIL", EMAIL_PATTERN),
    Detector("taiwan_mobile", "TAIWAN_MOBILE", TAIWAN_MOBILE_PATTERN),
    Detector("taiwan_id", "TAIWAN_ID", TAIWAN_ID_PATTERN, _is_valid_taiwan_id),
    Detector(
        "taiwan_resident_certificate",
        "TAIWAN_ARC",
        TAIWAN_ARC_PATTERN,
        _is_valid_resident_certificate,
    ),
    Detector(
        "taiwan_resident_certificate",
        "TAIWAN_ARC",
        TAIWAN_OLD_ARC_PATTERN,
        _is_valid_resident_certificate,
    ),
    Detector("taiwan_ubn", "TAIWAN_UBN", TAIWAN_UBN_PATTERN, _is_valid_ubn),
    Detector("passport", "PASSPORT", PASSPORT_PATTERN),
    Detector("nhi_card", "NHI_CARD", NHI_CARD_PATTERN),
    Detector("credit_card", "CREDIT_CARD", CREDIT_CARD_PATTERN, _is_valid_credit_card),
    Detector(
        "taiwan_landline", "TAIWAN_LANDLINE", TAIWAN_LANDLINE_PATTERN, _is_landline
    ),
)
# Order used when listing detected types (stable regardless of match position).
_TYPE_ORDER = (
    "email",
    "taiwan_mobile",
    "taiwan_landline",
    "taiwan_id",
    "taiwan_resident_certificate",
    "taiwan_ubn",
    "passport",
    "nhi_card",
    "credit_card",
    "api_key",
    "jwt",
    "private_key",
    "password",
)

Finder = Callable[[str], list[PersonalDataMatch]]
_extra_finders: list[Finder] = []
_extra_types: set[str] = set()
# Administrator-defined terms (custom_rules); replaced as a whole when saved.
_custom_finder: Finder | None = None
_custom_types: frozenset[str] = frozenset()


def set_custom_finder(finder: Finder | None, *, types: Iterable[str] = ()) -> None:
    global _custom_finder, _custom_types
    _custom_finder, _custom_types = finder, frozenset(types)


def register_finder(finder: Finder, *, types: Iterable[str]) -> None:
    """Add a detector that is not a regex, such as an NER model."""
    _extra_finders.append(finder)
    _extra_types.update(types)


def clear_registered_finders() -> None:
    _extra_finders.clear()
    _extra_types.clear()


def supported_types() -> frozenset[str]:
    return (
        frozenset(detector.type for detector in DETECTORS)
        | _extra_types
        | _custom_types
    )


def has_slow_finders() -> bool:
    """Whether a slow detector (named-entity recognition) is registered, so
    callers should keep detection off the event loop and run it less often."""
    return bool(_extra_finders)


# Marks "use the installed custom terms" in find_personal_data.
_INSTALLED = object()


def find_personal_data(
    text: str, *, custom_finder: Any = _INSTALLED, entities: bool = True
) -> list[PersonalDataMatch]:
    """Return non-overlapping personal-data matches in text order.

    Detectors earlier in ``DETECTORS`` win when two matches overlap; registered
    finders (NER) come last. ``custom_finder`` replaces the installed custom
    terms for this call only (None for none), so previewing unsaved rules
    never changes what concurrent requests detect.
    """
    found: list[PersonalDataMatch] = []

    def add(candidate: PersonalDataMatch) -> None:
        if not any(
            candidate.start < other.end and other.start < candidate.end
            for other in found
        ):
            found.append(candidate)

    for detector in DETECTORS:
        for start, end, value in detector.matches(text):
            add(PersonalDataMatch(detector.type, detector.label, start, end, value))
    custom = _custom_finder if custom_finder is _INSTALLED else custom_finder
    finders = [custom] if custom is not None else []
    for finder in [*finders, *(_extra_finders if entities else [])]:
        for candidate in finder(text):
            add(candidate)
    return sorted(found, key=lambda item: item.start)


def detect_personal_data(text: str) -> list[str]:
    """Detect personal-data types in text, in a stable order."""
    found = {match.type for match in find_personal_data(text)}
    ordered = [data_type for data_type in _TYPE_ORDER if data_type in found]
    return ordered + sorted(found.difference(_TYPE_ORDER))


def value_pattern(value: str) -> re.Pattern[str]:
    """Match a literal value, not as part of a longer ASCII word.

    CJK text has no spaces, so boundaries only apply to ASCII word characters.
    """
    left = _NOT_AFTER_WORD if re.match(r"[A-Za-z0-9_]", value) else ""
    right = _NOT_BEFORE_WORD if re.search(r"[A-Za-z0-9_]\Z", value) else ""
    return re.compile(left + re.escape(value) + right)


def redact_personal_data(text: str, *, additional_values: Iterable[str] = ()) -> str:
    """Replace supported personal-data patterns with descriptive markers."""
    redacted = text
    for match in reversed(find_personal_data(text)):
        redacted = redacted[: match.start] + f"[{match.label}]" + redacted[match.end :]

    for value in sorted(set(additional_values), key=len, reverse=True):
        if len(value) < 2 or value not in redacted:
            continue
        redacted = value_pattern(value).sub("[PERSONAL_DATA]", redacted)

    return redacted
