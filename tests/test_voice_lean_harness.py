"""V13: her voice lane's Codex child without the coding-agent harness (`codex_voice_lean_harness`, mabar on).

Unit tests use a fake CODEX_HOME. The golden renders `codex debug prompt-input` (no model call, no app-server)
with the lane's real overrides when the Codex binary is installed, and skips cleanly when it is not.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import arti_codex_agent as ca
from arti_runtime import codex_voice_harness as harness

ROOT = Path(__file__).resolve().parent.parent
LEAN = {"codex_voice_lean_harness": True, "codex_model": "gpt-6-luna"}
ENTRY = {"slug": "gpt-6-luna", "context_window": 272000, "max_context_window": 872000,
         "effective_context_window_percent": 95, "multi_agent_version": "v2", "tool_mode": "code_mode_only"}


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "models_cache.json").write_text(json.dumps({"models": [{"slug": "other"}, ENTRY]}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(harness, "_catalogs", {})
    monkeypatch.setattr(harness, "_catalog_warned", set())
    return home


def _catalog_path(overrides):
    found = [o for o in overrides if o.startswith("model_catalog_json=")]
    return Path(json.loads(found[0].split("=", 1)[1])) if found else None


def test_flag_off_overrides_are_unchanged():
    assert ca.codex_overrides({}) == ('sandbox_mode="read-only"',)
    assert ca.codex_overrides({"codex_voice_lean_harness": False, "codex_model": "gpt-6-luna"}) == (
        'sandbox_mode="read-only"',)
    assert ca.codex_overrides({"codex_web_search": "disabled"}) == ('sandbox_mode="read-only"', 'web_search="disabled"')


def test_flag_on_removes_every_tool_feature_skill_and_text_block(fake_home):
    overrides = ca.codex_overrides(dict(LEAN, codex_web_search="disabled"))
    assert overrides[:2] == ('sandbox_mode="read-only"', 'web_search="disabled"')
    for name in harness.LEAN_FEATURES:
        assert f"features.{name}=false" in overrides
    for setting in harness.LEAN_SETTINGS:
        assert setting in overrides
    assert "mcp_servers={}" in overrides and 'model="gpt-6-luna"' in overrides


def test_catalog_is_one_model_without_the_multi_agent_role_and_outside_the_scratch_cwd(fake_home, tmp_path):
    path = _catalog_path(ca.codex_overrides(LEAN))
    expected = dict(ENTRY)
    expected.pop("multi_agent_version")
    assert json.loads(path.read_text(encoding="utf-8")) == {"models": [expected]}
    assert json.loads((fake_home / "models_cache.json").read_text(encoding="utf-8"))["models"][1] == ENTRY  # untouched
    assert path.parent.name.startswith("arti-codex-voice-catalog-"), "the child's scratch cwd must stay empty"
    assert _catalog_path(ca.codex_overrides(LEAN)) == path  # once per process


def test_missing_catalog_entry_keeps_the_rest_of_the_trim_and_logs_once(fake_home, capsys):
    cfg = dict(LEAN, codex_model="gpt-unknown")
    first, second = ca.codex_overrides(cfg), ca.codex_overrides(cfg)
    assert first == second and _catalog_path(first) is None
    assert "features.shell_tool=false" in first and "mcp_servers={}" in first
    assert capsys.readouterr().out.count("no catalog entry for gpt-unknown") == 1


def test_lean_harness_and_append_context_compose(fake_home):
    overrides = ca.codex_overrides(dict(LEAN, voice_append_context=True, codex_context_window=872000))
    assert "features.multi_agent=false" in overrides and "model_context_window=872000" in overrides
    assert any(o.startswith("model_auto_compact_token_limit=") for o in overrides)


def test_only_the_voice_lane_uses_the_lean_harness():
    users = sorted(p.name for p in ROOT.glob("*.py") if "codex_voice_harness" in p.read_text(encoding="utf-8"))
    users += sorted(str(p.relative_to(ROOT)) for p in (ROOT / "arti_runtime").glob("*.py")
                    if p.name != "codex_voice_harness.py" and "codex_voice_harness" in p.read_text(encoding="utf-8"))
    assert users == ["arti_codex_agent.py"]
    import arti_codex_role

    for role in ("scout", "observer", "lookup", "context", "diary"):
        assert "include_environment_context=false" not in arti_codex_role.role_overrides(role)


def test_mabar_turns_the_lean_harness_on(tmp_path):
    from scripts.livepath import mabar

    assert mabar.mabar_bridge_config(tmp_path / "r", vts=True, mic=1)["codex_voice_lean_harness"] is True


# --- golden: the model-visible input the voice child really renders ------------------------------------------------

PROMPT = "V13 golden: halo"
HARNESS_BOUND_CHARS = 512        # with the catalog: nothing in front of her own prompt
HARNESS_BOUND_NO_CATALOG = 4096  # without a cache entry only the multi-agent role text may remain


def _codex_binary():
    binary = Path(ca.DEFAULT_BIN)
    return binary if binary.is_file() else None


@pytest.mark.skipif(_codex_binary() is None, reason="Codex CLI not installed on this machine")
def test_prompt_input_golden_the_voice_child_sees_no_tools_and_no_harness(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_catalogs", {})
    overrides = ca.codex_overrides(dict(LEAN, codex_web_search="disabled"))
    cwd = tmp_path / "empty"
    cwd.mkdir()
    args = [str(_codex_binary()), "debug", "prompt-input"]
    for override in overrides:
        args += ["-c", override]
    result = subprocess.run(args + [PROMPT], capture_output=True, text=True, encoding="utf-8", cwd=cwd, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    items = json.loads(result.stdout)
    rendered = json.dumps(items, ensure_ascii=False)
    assert not any(item.get("type") == "additional_tools" or "tools" in item for item in items), "a tool remains"
    for marker in ("You are Codex", "<skills_instructions>", "<environment_context>", "spawn_agent", "mcp__",
                   "<permissions instructions>", "<collaboration_mode>"):
        assert marker not in rendered, marker
    harness_items = [item for item in items if PROMPT not in json.dumps(item, ensure_ascii=False)]
    size = len(json.dumps(harness_items, ensure_ascii=False))
    if _catalog_path(overrides) is not None:
        assert "<multi_agent_role>" not in rendered
        assert size <= HARNESS_BOUND_CHARS, harness_items
    else:
        assert size <= HARNESS_BOUND_NO_CATALOG, harness_items
