from ugen_ai_gateway.analysis.redaction import RedactionVault, StreamRestorer


def test_vault_round_trips_detected_and_additional_values() -> None:
    vault = RedactionVault(["Alex Chen"])

    redacted = vault.redact("Alex Chen: alex@example.com, 0912-345-678")

    assert redacted == "[PERSONAL_DATA_1]: [EMAIL_1], [TAIWAN_MOBILE_1]"
    assert len(vault) == 3
    assert vault.restore(redacted) == "Alex Chen: alex@example.com, 0912-345-678"


def test_vault_skips_placeholders_already_present_in_input() -> None:
    vault = RedactionVault()
    text = "Literal [EMAIL_1] and alex@example.com"
    vault.reserve([text])

    redacted = vault.redact(text)

    assert redacted == "Literal [EMAIL_1] and [EMAIL_2]"
    assert vault.restore(redacted) == text


def test_restore_leaves_unknown_or_altered_placeholders_alone() -> None:
    vault = RedactionVault()
    vault.redact("alex@example.com")

    assert vault.restore("[EMAIL_9] [email_1] [EMAIL_1]") == (
        "[EMAIL_9] [email_1] alex@example.com"
    )


def test_stream_restorer_holds_partial_placeholders_until_complete() -> None:
    vault = RedactionVault()
    vault.redact("alex@example.com")
    restorer = StreamRestorer(vault)

    out = [restorer.feed(chunk) for chunk in ["Hi [", "EMAIL", "_1", "]!"]]

    assert out == ["Hi ", "", "", "alex@example.com!"]
    assert restorer.flush() == ""


def test_stream_restorer_releases_brackets_that_are_not_placeholders() -> None:
    restorer = StreamRestorer(RedactionVault())

    assert restorer.feed("see [docs") == "see [docs"
    assert restorer.feed(" [A") == " "
    assert restorer.flush() == "[A"
