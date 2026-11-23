"""Golden Groq routing, HTTP payloads, and fallback, as the bridge had them before the split."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import arti_reply_policy


def _run_groq_case(
    bridge, statuses, *, roll=False, breaker=False, health_enabled=True, during_openrouter=None,
):
    events = []
    responses = iter(statuses)

    def post(url, *, headers, json, timeout):
        events.append((
            "post", url, headers["Authorization"], json["model"], json["max_tokens"],
            json.get("reasoning_effort"), tuple(message["content"] for message in json["messages"]),
            timeout,
        ))
        code = next(responses)
        return SimpleNamespace(
            status_code=code, text="stub error",
            json=lambda: {"choices": [{"message": {"content": "Jawaban Groq."}}]},
        )

    def openrouter(system, user, config):
        events.append(("openrouter", system, user))
        if during_openrouter is not None:
            during_openrouter()
        return "Jawaban OpenRouter.", "openrouter/stub"

    def trim(system, user):
        events.append(("trim", system, user))
        return "short system", "short user"

    privacy = SimpleNamespace(epoch=3, current=lambda epoch: epoch == 3)
    health = SimpleNamespace(
        enabled=lambda: health_enabled,
        is_available=lambda provider: provider != "groq" or not breaker,
        classify_status=lambda status: events.append(("classify", status)) or "upstream",
        HARD=set(),
        record_success=lambda provider: events.append(("success", provider)),
        record_failure=lambda provider, reason: events.append(("failure", provider, reason)),
    )
    config = {
        "groq_api_key": "stub-key",
        "groq_models": ["test/gpt-oss", "test/qwen"],
        "groq_model_fast": "test/qwen",
        "groq_roll_all_models_on_limit": roll,
    }
    with (
        patch.object(bridge, "CONFIG", config),
        patch.object(bridge, "requests", SimpleNamespace(post=post)),
        patch.object(bridge, "screen_privacy", privacy),
        patch.object(bridge, "arti_provider_health", health),
        patch.object(bridge, "arti_openrouter", SimpleNamespace(openrouter_live_completion=openrouter)),
        patch.object(bridge, "arti_reply_policy", arti_reply_policy),
        patch.object(bridge, "live_max_tokens_for_trigger", lambda user, cfg: 32),
        patch.object(bridge, "_trim_llm_payload", trim),
    ):
        result = bridge.groq_chat_completion("test/gpt-oss", "system", "user", {})
    return result, events


def _post_event(model, max_tokens, reasoning, messages=("system", "user")):
    return (
        "post", "https://api.groq.com/openai/v1/chat/completions", "Bearer stub-key",
        model, max_tokens, reasoning, messages, (3, 8),
    )


_FIRST = _post_event("test/gpt-oss", 288, "low")
_OR = ("openrouter", "system", "user")


@pytest.mark.parametrize(
    ("statuses", "options", "expected_result", "expected_events"),
    [
        (
            [429, 200], {"roll": True}, ("Jawaban Groq.", "test/qwen"),
            [_FIRST, _post_event("test/qwen", 32, "none"), ("success", "groq")],
        ),
        (
            [413, 200], {}, ("Jawaban Groq.", "test/qwen"),
            [
                _FIRST, ("trim", "system", "user"),
                _post_event("test/qwen", 288, "low", ("short system", "short user")),
                ("success", "groq"),
            ],
        ),
        (
            [503], {}, ("Jawaban OpenRouter.", "openrouter/stub"),
            [_FIRST, _OR, ("success", "openrouter:voice"), ("classify", 503), ("failure", "groq", "upstream")],
        ),
        (
            [], {"breaker": True}, ("Jawaban OpenRouter.", "openrouter/stub"),
            [_OR, ("success", "openrouter:voice")],
        ),
        (
            [200], {"health_enabled": False}, ("Jawaban Groq.", "test/gpt-oss"), [_FIRST],
        ),
    ],
)
def test_groq_chain_matches_original(statuses, options, expected_result, expected_events):
    import arti_bridge

    assert _run_groq_case(arti_bridge, statuses, **options) == (expected_result, expected_events)


def test_groq_fallback_health_is_isolated_between_bridge_loads(tmp_path, monkeypatch):
    from tests.test_bridge_startup_bugfix import _pristine_bridge_module

    config = tmp_path / "bridge-test-config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ARTI_BRIDGE_TEST_CONFIG", str(config))
    monkeypatch.delenv("ARTI_REHEARSAL_CONFIG", raising=False)
    with (
        patch("arti_env.load_project_env", return_value=False),
        patch("arti_buffered_writer.BatchedWriter", side_effect=lambda stream: stream),
        patch("threading.Thread.start", side_effect=AssertionError("Bridge import started a thread")),
    ):
        bridge_a = _pristine_bridge_module()
        bridge_b = _pristine_bridge_module()

    def call_bridge_b():
        assert _run_groq_case(bridge_b, [200]) == (
            ("Jawaban Groq.", "test/gpt-oss"), [_FIRST, ("success", "groq")],
        )

    assert _run_groq_case(bridge_a, [503], during_openrouter=call_bridge_b) == (
        ("Jawaban OpenRouter.", "openrouter/stub"),
        [_FIRST, _OR, ("success", "openrouter:voice"), ("classify", 503), ("failure", "groq", "upstream")],
    )
