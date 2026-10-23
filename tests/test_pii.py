import pytest

from ugen_ai_gateway.analysis.pii import (
    PersonalDataMatch,
    clear_registered_finders,
    detect_personal_data,
    find_personal_data,
    redact_personal_data,
    register_finder,
    supported_types,
)


@pytest.mark.parametrize(
    "email",
    [
        "alex@example.com",
        "alex.smith+work@example.co.uk",
        "alex_smith-2@sub.example.com",
    ],
)
def test_detects_email_addresses(email: str) -> None:
    assert detect_personal_data(f"Contact me at {email}.") == ["email"]


@pytest.mark.parametrize(
    "phone_number",
    [
        "0912345678",
        "0912-345-678",
        "0912 345 678",
        "+886912345678",
        "+886 912 345 678",
        "886-912-345-678",
    ],
)
def test_detects_taiwan_mobile_numbers(phone_number: str) -> None:
    assert detect_personal_data(f"Call me: {phone_number}") == ["taiwan_mobile"]


def test_detects_multiple_categories_in_stable_order() -> None:
    text = "Email alex@example.com or call 0912-345-678."

    assert detect_personal_data(text) == ["email", "taiwan_mobile"]


def test_reports_each_category_once_when_multiple_values_are_present() -> None:
    text = "alex@example.com, sam@example.org, 0912345678, +886 912 345 678"

    assert detect_personal_data(text) == ["email", "taiwan_mobile"]


@pytest.mark.parametrize(
    "text",
    [
        "",
        "No contact information is included.",
        "alex@example",
        "alex@example..com",
        "091234567",
        "09123456789",
        "88691234567",
        "Call 1234567890 instead.",
    ],
)
def test_returns_empty_for_text_without_supported_personal_data(text: str) -> None:
    assert detect_personal_data(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "alex@example.com-",
        "x0912345678",
        "0912345678x",
    ],
)
def test_does_not_match_identifiers_embedded_in_word_like_text(text: str) -> None:
    assert detect_personal_data(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Email alex@example.com", "Email [EMAIL]"),
        ("Call 0912-345-678", "Call [TAIWAN_MOBILE]"),
        (
            "alex@example.com / +886 912 345 678",
            "[EMAIL] / [TAIWAN_MOBILE]",
        ),
        ("No personal data here", "No personal data here"),
    ],
)
def test_redacts_detected_personal_data(text: str, expected: str) -> None:
    assert redact_personal_data(text) == expected


# --- Expanded detectors -------------------------------------------------------

POSITIVE_CASES = [
    ("電話0912345678", "taiwan_mobile", "0912345678"),
    ("身分證A123456789。", "taiwan_id", "A123456789"),
    ("辦公室 02-2345-6789", "taiwan_landline", "02-2345-6789"),
    ("Office (02)2345-6789", "taiwan_landline", "(02)2345-6789"),
    ("Call +886-2-2345-6789", "taiwan_landline", "+886-2-2345-6789"),
    ("高雄 07-123-4567", "taiwan_landline", "07-123-4567"),
    ("苗栗037-12-3456", "taiwan_landline", "037-12-3456"),
    ("居留證 A800000005", "taiwan_resident_certificate", "A800000005"),
    ("舊居留證AC00000003", "taiwan_resident_certificate", "AC00000003"),
    ("統編：22099131", "taiwan_ubn", "22099131"),
    ("公司統一編號 10458575", "taiwan_ubn", "10458575"),
    ("VAT number 04595257", "taiwan_ubn", "04595257"),
    ("護照號碼：312345678", "passport", "312345678"),
    ("passport no. X1234567", "passport", "X1234567"),
    ("健保卡號 0000-1234-5678", "nhi_card", "0000-1234-5678"),
    ("卡號 4111 1111 1111 1111", "credit_card", "4111 1111 1111 1111"),
    (
        "key: sk-proj-abcdefghijklmnopqrstuvwx",
        "api_key",
        "sk-proj-abcdefghijklmnopqrstuvwx",
    ),
    ("AWS AKIAABCDEFGHIJKLMNOP", "api_key", "AKIAABCDEFGHIJKLMNOP"),
    (
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "api_key",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ),
    (
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
        "jwt",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    ),
    ("password=hunter22", "password", "hunter22"),
    ("密碼：Abc12345，請保管", "password", "Abc12345"),
]


@pytest.mark.parametrize(("text", "data_type", "value"), POSITIVE_CASES)
def test_expanded_detectors_find_the_exact_value(
    text: str, data_type: str, value: str
) -> None:
    matches = find_personal_data(text)

    assert [(match.type, match.value) for match in matches] == [(data_type, value)]


def test_private_key_blocks_are_detected() -> None:
    text = (
        "here:\n-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n"
        "-----END RSA PRIVATE KEY-----\nthanks"
    )

    assert detect_personal_data(text) == ["private_key"]
    assert redact_personal_data(text) == "here:\n[PRIVATE_KEY]\nthanks"


@pytest.mark.parametrize(
    "text",
    [
        "身分證A123456788",  # bad checksum
        "日期 2024-01-31 結帳",
        "訂單編號 20231231",  # 8 digits without a business-ID keyword
        "統編：12345678",  # bad UBN checksum
        "金額 1234-5678 元",
        "版本 0.2.3，台北 101",
        "居留證 A800000004",  # bad checksum
        "the password is long",  # no assignment
        "sk-short",
        "卡號 4111 1111 1111 1112",  # bad Luhn
    ],
)
def test_expanded_detectors_ignore_near_misses(text: str) -> None:
    assert find_personal_data(text) == []


def test_keyword_stays_readable_when_only_the_value_is_redacted() -> None:
    assert redact_personal_data("統編：22099131，密碼: hunter22") == (
        "統編：[TAIWAN_UBN]，密碼: [PASSWORD]"
    )


def test_additional_values_are_redacted_inside_chinese_text() -> None:
    assert (
        redact_personal_data("我叫王小明，住台北", additional_values=["王小明"])
        == "我叫[PERSONAL_DATA]，住台北"
    )


def test_additional_ascii_values_still_respect_word_boundaries() -> None:
    assert (
        redact_personal_data("Al and Alice", additional_values=["Al"])
        == "[PERSONAL_DATA] and Alice"
    )


def test_registered_finders_extend_detection() -> None:
    def names(text: str) -> list[PersonalDataMatch]:
        start = text.find("王小明")
        if start == -1:
            return []
        return [PersonalDataMatch("person_name", "PERSON", start, start + 3, "王小明")]

    register_finder(names, types=["person_name"])
    try:
        assert detect_personal_data("王小明 alex@example.com") == [
            "email",
            "person_name",
        ]
        assert "person_name" in supported_types()
        assert redact_personal_data("我是王小明") == "我是[PERSON]"
    finally:
        clear_registered_finders()
    assert "person_name" not in supported_types()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Real card formats are still found.
        ("卡號 4111 1111 1111 1111", ["4111 1111 1111 1111"]),
        ("card 5555-5555-5555-4444", ["5555-5555-5555-4444"]),
        ("Amex 378282246310005", ["378282246310005"]),
        ("Discover 6445644564456445", ["6445644564456445"]),
        ("Diners 36000000000008", ["36000000000008"]),
        ("Maestro 5018000000000009", ["5018000000000009"]),
        # Luhn-valid numbers without a card issuer prefix are not cards.
        ("物流單號 1234567812345670", []),
        ("order 8000000000000002", []),
    ],
)
def test_card_numbers_need_an_issuer_prefix(text: str, expected: list[str]) -> None:
    found = [m.value for m in find_personal_data(text) if m.type == "credit_card"]
    assert found == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("居留證號碼 AB10000004", ["AB10000004"]),
        ("ARC: AB10000004", ["AB10000004"]),
        ("我的居留證號碼是AB10000004", ["AB10000004"]),
        ("居留證號碼（ARC）：AB10000004", ["AB10000004"]),
        ("my ARC number is AB10000004", ["AB10000004"]),
        ("Alien Resident Certificate (ARC) No.: AB10000004", ["AB10000004"]),
        ("A810000008", ["A810000008"]),
        # Old format without a keyword reads as a 統一發票 number.
        ("統一發票 AB10000004 中獎了嗎", []),
        ("AB10000004", []),
    ],
)
def test_old_resident_certificates_need_a_keyword(
    text: str, expected: list[str]
) -> None:
    found = [
        m.value
        for m in find_personal_data(text)
        if m.type == "taiwan_resident_certificate"
    ]
    assert found == expected
