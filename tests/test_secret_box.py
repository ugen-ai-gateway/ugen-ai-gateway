import os

from ugen_ai_gateway.services.model_config import StoredModels, sealed, unsealed
from ugen_ai_gateway.services.secret_box import PREFIX, SecretBox


def test_values_round_trip_and_plain_values_pass_through(tmp_path) -> None:
    box = SecretBox.for_database(str(tmp_path / "ugen.db"))
    sealed_value = box.seal("sk-secret")
    assert sealed_value.startswith(PREFIX) and "sk-secret" not in sealed_value
    assert box.open(sealed_value) == "sk-secret"
    assert box.open("legacy-plain") == "legacy-plain"
    assert box.seal("") == ""


def test_key_file_is_private_and_reused(tmp_path) -> None:
    first = SecretBox.for_database(str(tmp_path / "ugen.db"))
    key_file = tmp_path / "secret.key"
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
    second = SecretBox.for_database(str(tmp_path / "ugen.db"))
    assert second.open(first.seal("value")) == "value"


def test_secret_key_setting_wins_and_a_wrong_key_reads_empty(tmp_path) -> None:
    configured = SecretBox.for_database(str(tmp_path / "ugen.db"), "my secret")
    assert not (tmp_path / "secret.key").exists()
    other = SecretBox.for_database(str(tmp_path / "ugen.db"), "another secret")
    assert other.open(configured.seal("value")) == ""


def test_model_configuration_keys_are_sealed(tmp_path) -> None:
    box = SecretBox.for_database(str(tmp_path / "ugen.db"))
    stored = StoredModels.model_validate(
        {"cloud": {"api_key": "sk-cloud"}, "local": {"api_key": "local-secret-1"}}
    )
    data = sealed(stored, box)
    assert (
        "sk-cloud" not in str(data) and "local-secret-1" not in data["local"]["api_key"]
    )
    assert unsealed(data, box).cloud.api_key == "sk-cloud"
