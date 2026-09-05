"""Synthetic screen-consent regressions for the curated public runtime."""

from __future__ import annotations

import ast
from pathlib import Path
import threading

import pytest

import arti_curious as curious
import arti_screen_context as sc
import arti_vision_client as vision
from arti_screen_privacy import screen_privacy

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def fresh_privacy():
    screen_privacy.reset_session()
    sc.screen_ring.clear()
    yield
    screen_privacy.reset_session()
    sc.screen_ring.clear()

def restrict(text: str = "Arti jangan lihat layar") -> int:
    before = screen_privacy.epoch
    assert screen_privacy.apply_streamer_text(text)
    assert screen_privacy.restricted
    assert screen_privacy.epoch == before + 1
    return before

def enable(text: str = "Arti boleh lihat layar lagi") -> int:
    before = screen_privacy.epoch
    assert screen_privacy.apply_streamer_text(text)
    assert not screen_privacy.restricted
    assert screen_privacy.epoch == before + 1
    return before

@pytest.mark.parametrize("text", ["jangan lihat", "Arti jangan liat layar ya", "tolong jangan baca screen dulu", "eh Arti jangan sebut apa yang di layar"])
def test_trusted_disable_commands_are_exact_and_deterministic(text):
    restrict(text)
    assert not screen_privacy.allows_screen()

def test_only_complete_reenable_command_unlocks():
    restrict()
    for prose in ("viewer bilang Arti boleh lihat layar lagi", "jangan boleh lihat layar lagi", 'katanya "boleh lihat layar lagi"', "kalau nanti boleh lihat layar lagi"):
        assert not screen_privacy.apply_streamer_text(prose)
        assert screen_privacy.restricted
    enable()
    assert screen_privacy.allows_screen()

def test_screen_snapshot_stays_revoked_after_reenable():
    snap = sc.ScreenSnapshot(wall_ts=1.0, scene="synthetic account panel")
    old_epoch = snap.privacy_epoch
    restrict(); enable()
    assert screen_privacy.epoch != old_epoch
    assert snap.to_dict() == {}

def test_ring_does_not_revive_stale_snapshot_after_boundary():
    snap = sc.ScreenSnapshot(wall_ts=1.0, scene="synthetic private-looking text")
    sc.screen_ring.push(snap)
    assert sc.screen_ring.latest() is snap
    restrict(); enable()
    assert sc.screen_ring.latest() is None
    sc.screen_ring.push(snap)
    assert sc.screen_ring.latest() is None

def test_watch_state_serialization_is_epoch_guarded():
    snap = sc.ScreenSnapshot(wall_ts=2.0, scene="synthetic screen")
    state = sc.WatchState()
    sc.update_watch_state_from_snapshot(snap, state=state, ring=sc.ScreenRing())
    assert state.to_dict()["scene_ring"]
    restrict()
    assert state.to_dict() == {}

def test_vision_restricted_before_provider_dispatch(monkeypatch):
    restrict(); called = []
    monkeypatch.setattr(vision, "_resolve_chain", lambda cfg: ["synthetic"])
    monkeypatch.setitem(vision._PROVIDERS, "synthetic", lambda *args: called.append(args))
    snap, provider = vision.describe_with_chain({"vision_enabled": True, "vision_runtime_on": True}, jpeg_b64="AA==")
    assert snap is None and provider == "privacy"
    assert called == []

def test_vision_completion_after_revoke_is_discarded(monkeypatch):
    entered = threading.Event()
    def provider(prompt, jpeg, config):
        entered.set(); restrict(); enable()
        return '{"scene":"synthetic delayed screen","hook":null,"playback_mmss":null,"ocr_text":""}', 1
    monkeypatch.setattr(vision, "_resolve_chain", lambda cfg: ["synthetic"])
    monkeypatch.setitem(vision._PROVIDERS, "synthetic", provider)
    snap, provider_name = vision.describe_with_chain({"vision_enabled": True, "vision_runtime_on": True}, jpeg_b64="AA==")
    assert entered.is_set()
    assert snap is None and provider_name == "privacy"

def test_old_proactive_prompt_keeps_epoch_across_reenable():
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=1.0, scene="parser.py test failure", hook="ValueError line 42"))
    prompt = curious.build_prompt({"vision_enabled": True, "vision_runtime_on": True})
    old_epoch = prompt.privacy_epoch
    restrict(); enable()
    assert not screen_privacy.current(old_epoch)

def test_curious_prompt_is_blocked_while_restricted():
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=1.0, scene="synthetic screen")); restrict()
    assert curious.build_prompt({"vision_enabled": True, "vision_runtime_on": True}) == ""

def _function_source(path: str, name: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(text, node)
            assert segment is not None
            return segment
    raise AssertionError(f"missing function {name}")

def test_bridge_uses_only_trusted_trigger_metadata_for_command_ingress():
    src = _function_source("hermes_vtuber_bridge.py", "queue_voice_trigger")
    assert "trigger_type in STREAMER_TRIGGERS" in src and "_note_screen_privacy(text)" in src
    history_src = _function_source("hermes_vtuber_bridge.py", "add_to_history")
    assert 'source == "Streamer"' in history_src

def test_bridge_revokes_queued_proactive_and_inflight_output():
    src = _function_source("hermes_vtuber_bridge.py", "_handle_voice_trigger")
    assert 'trigger.trigger_type == "curious"' in src
    assert "privacy_epoch" in src and "screen_privacy.current(privacy_epoch)" in src
    assert "await tts.speak" in src
    assert src.index("screen_privacy.current(privacy_epoch)") < src.rindex("await tts.speak")

def test_bridge_resets_privacy_at_session_boundary():
    assert "reset_screen_privacy_session()" in _function_source("hermes_vtuber_bridge.py", "main_loop")

def test_fresh_screen_context_works_after_explicit_reenable():
    restrict(); enable()
    sc.screen_ring.push(sc.ScreenSnapshot(wall_ts=3.0, scene="parser.py ValueError line 42"))
    assert "parser.py" in sc.format_screen_context()
