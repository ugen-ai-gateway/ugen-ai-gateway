from tests.test_accounts import AccountEnv, bearer
from ugen_ai_gateway.services.secret_box import PREFIX

SYSTEM_PROMPT = "Answer in Traditional Chinese."
INSTRUCTIONS = "I am a nurse; keep answers short."


def _set_prompts(env: AccountEnv, token: str) -> None:
    saved = env.client.put(
        "/v1/ugen/system-prompt", json={"text": SYSTEM_PROMPT}, headers=bearer(token)
    )
    assert saved.status_code == 200, saved.text
    saved = env.client.put(
        "/v1/ugen/auth/me/instructions",
        json={"text": f"  {INSTRUCTIONS}\n"},
        headers=bearer(token),
    )
    assert saved.json() == {"text": INSTRUCTIONS}


def test_prompts_go_ahead_of_the_conversation_as_one_system_message(
    monkeypatch,
) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    _set_prompts(env, alice)

    memory = {"role": "system", "content": "Earlier: we discussed rotas."}
    question = {"role": "user", "content": "please design a system"}
    assert env.chat(alice, messages=[memory, question]).status_code == 200
    assert env.cloud.calls[-1]["messages"] == [
        {
            "role": "system",
            "content": f"{SYSTEM_PROMPT}\n\nThe user's custom instructions:\n"
            f"{INSTRUCTIONS}\n\nEarlier: we discussed rotas.",
        },
        question,
    ]

    # Another account gets the system prompt but not Alice's instructions.
    assert env.chat(bob).status_code == 200
    assert env.cloud.calls[-1]["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        question,
    ]


def test_api_keys_get_the_system_prompt_but_no_account_instructions(
    monkeypatch,
) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    _set_prompts(env, alice)
    key = env.client.post(
        "/v1/ugen/keys", json={"name": "app"}, headers=bearer(alice)
    ).json()["key"]

    response = env.client.post(
        "/v1/chat/completions",
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "please design a system"}],
        },
        headers=bearer(key),
    )

    assert response.status_code == 200, response.text
    assert env.cloud.calls[-1]["messages"][0] == {
        "role": "system",
        "content": SYSTEM_PROMPT,
    }


def test_personal_data_in_instructions_is_redacted_like_any_text(
    monkeypatch,
) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    env.client.put(
        "/v1/ugen/auth/me/instructions",
        json={"text": "Reply to alex@example.com"},
        headers=bearer(alice),
    )

    assert env.chat(alice).status_code == 200
    sent = env.cloud.calls[-1]["messages"][0]["content"]
    assert "alex@example.com" not in sent and "[EMAIL_1]" in sent


def test_instructions_are_encrypted_and_only_for_their_account(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    _set_prompts(env, alice)

    stored = env.store._connection.execute(
        "SELECT instructions FROM users WHERE username = 'alice'"
    ).fetchone()[0]
    assert stored.startswith(PREFIX) and "nurse" not in stored
    me = "/v1/ugen/auth/me/instructions"
    assert env.client.get(me, headers=bearer(alice)).json() == {"text": INSTRUCTIONS}
    assert env.client.get(me, headers=bearer(bob)).json() == {"text": ""}
    assert env.client.get(me).status_code == 401
    assert (
        "instructions"
        not in env.client.get("/v1/ugen/auth/me", headers=bearer(alice)).json()
    )

    cleared = env.client.put(me, json={"text": ""}, headers=bearer(alice))
    assert cleared.json() == {"text": ""}
    assert env.client.get(me, headers=bearer(alice)).json() == {"text": ""}


def test_only_administrators_set_the_system_prompt(monkeypatch) -> None:
    env = AccountEnv(monkeypatch)
    alice = env.token("alice")
    bob = env.token("bob")
    url = "/v1/ugen/system-prompt"

    assert (
        env.client.put(url, json={"text": "x"}, headers=bearer(bob)).status_code == 403
    )
    assert env.client.get(url, headers=bearer(bob)).status_code == 403
    too_long = {"text": "x" * 8001}
    assert env.client.put(url, json=too_long, headers=bearer(alice)).status_code == 422

    env.client.put(url, json={"text": SYSTEM_PROMPT}, headers=bearer(alice))
    assert env.client.get(url, headers=bearer(alice)).json() == {"text": SYSTEM_PROMPT}
    # The audit log notes the change, never the prompt itself.
    details = [
        row["detail"]
        for row in env.store._connection.execute(
            "SELECT detail FROM audit WHERE event = 'system_prompt_updated'"
        )
    ]
    assert len(details) == 1 and '"characters": 30' in details[0]
    assert "Chinese" not in details[0]
