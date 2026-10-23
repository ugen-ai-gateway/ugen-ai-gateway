from ugen_ai_gateway.config import Settings


def test_env_file_may_hold_settings_for_other_tools(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("GATEWAY_PORT=7632\nLOCAL_MODEL=qwen\n")
    assert Settings(_env_file=env).local_model == "qwen"  # type: ignore[call-arg]
