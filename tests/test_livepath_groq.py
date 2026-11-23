"""Probe the checked-out router/trimmer; never import the bridge or call providers."""
import ast
from functools import partial
import json
import re

import pytest

from scripts.livepath import bridge, rehearse


def row(capsys):
    return json.loads(capsys.readouterr().out.split("[Livepath] ", 1)[1])


def test_probe_records_counts_and_marker_survival_only(capsys):
    system = "PRIVATE_SYSTEM\n[NATIVE_FACTS] PRIVATE_FACT\n[KINI] now\n[STARDEW] farm\n[JIWA ARTI]"
    user = "history\n[Pesan/Panggilan Sekarang:] PRIVATE_MESSAGE"
    calls = []
    def pick(trigger, config, *, prompt_chars):
        calls.append((trigger, config, prompt_chars))
        return "model"
    def trim(s, u):
        calls.append((s, u))
        return s, u
    cfg = dict(groq_prompt_char_soft_cap=1)
    bridge.groq_probe("turn", "PRIVATE_MESSAGE", cfg, system, user, pick, trim)
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    event = json.loads(output.split("[Livepath] ", 1)[1])
    assert calls == [("PRIVATE_MESSAGE", cfg, len(system) + len(user)), (system, user)]
    assert event["size_forced_fast"] is True and event["current_survives"] is True
    assert all(event["present"].values()) and all(event["survives"].values())


def test_header_surviving_does_not_hide_lost_native_facts(capsys):
    bridge.groq_probe("turn", "ask", {}, "[NATIVE_FACTS] real facts", "ask",
                      lambda *a, **kw: "fast", lambda *a: ("[NATIVE_FACTS]", "ask"))
    event = row(capsys)
    assert event["present"]["native_facts"] is True
    assert event["survives"]["native_facts"] is False


@pytest.mark.parametrize("stage", ["pick", "trim", "emit"])
def test_probe_exceptions_never_affect_turn(stage, monkeypatch, capsys):
    def fail(*a, **kw):
        raise RuntimeError("PRIVATE_ERROR")
    pick = fail if stage == "pick" else lambda *a, **kw: "model"
    trim = fail if stage == "trim" else lambda s, u: (s, u)
    if stage == "emit":
        monkeypatch.setattr(bridge, "emit", fail)
    bridge.groq_probe("turn", "ask", {}, "system", "user", pick, trim)
    assert "PRIVATE_ERROR" not in capsys.readouterr().out


def test_probe_preserves_router_round_robin_state(capsys):
    def pick(*a, **kw):
        pick._rr_idx = getattr(pick, "_rr_idx", 0) + 1
        return "model"
    bridge.groq_probe("turn", "ask", {}, "s", "u", pick, lambda s, u: (s, u))
    assert not hasattr(pick, "_rr_idx")
    pick._rr_idx = 7
    bridge.groq_probe("turn", "ask", {}, "s", "u", pick, lambda s, u: (s, u))
    assert pick._rr_idx == 7


def test_probe_calls_actual_checked_out_functions(capsys):
    tree = ast.parse((rehearse.ROOT / "arti_runtime/groq_chat.py").read_text(encoding="utf-8"))
    names = {"pick_groq_model", "_trim_llm_payload", "_split_stardew_status", "_remove_system_prompt_block"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    cfg = dict(groq_models=["fast", "strong"], groq_model_fast="fast", groq_model_strong="strong",
               smart_groq_routing=True, groq_prompt_char_soft_cap=10000)
    assert {node.name for node in nodes} == names
    scope = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "checked-out-groq", "exec"), scope)

    def pick(*args, **kwargs):
        return scope["pick_groq_model"](*args, **kwargs, default_config=cfg, pick_groq_model=pick)

    trim = partial(
        scope["_trim_llm_payload"],
        _split_stardew_status=partial(scope["_split_stardew_status"], config=cfg, re=re),
        _remove_system_prompt_block=partial(
            scope["_remove_system_prompt_block"], re=re,
            _SYSTEM_PROMPT_BLOCK_MARKERS=(), _SYSTEM_PROMPT_KEEP_MARKERS=(),
        ),
        _SYSTEM_PROMPT_BLOCK_MARKERS=(), SOUL_MARKER="\n\n[JIWA ARTI]", re=re,
    )
    system = "x" * 20000 + "\n[NATIVE_FACTS] present\n[KINI] farm\n[JIWA ARTI] soul"
    user = "[Pesan/Panggilan Sekarang:] kenapa?"
    expected_s, expected_u = trim(system, user)
    bridge.groq_probe("turn", "kenapa?", cfg, system, user, pick, trim)
    event = row(capsys)
    assert event["event"] == "groq_probe"
    assert event["model"] == "fast" and event["size_forced_fast"] is True
    assert event["chars_after"] == len(expected_s) + len(expected_u)
    assert event["current_survives"] == (user in expected_u)
    assert event["survives"]["native_facts"] == ("[NATIVE_FACTS] present" in expected_s)
