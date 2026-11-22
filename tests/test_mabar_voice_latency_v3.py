"""Voice latency and own-play routing (V3).

- Voice effort: the rehearsal parity and the live overlay set ``codex_effort="high"`` for every turn, and turns with
  long reasoning were much slower. mabar now pins the bridge's own ladder: ``low`` by default, ``high`` only for the
  heavy reply classes (deep, rant).
- Own-play lines: ``own_play`` was not in ``cursor_trigger_types``, so every own-play turn fell through to the
  default provider chain, whose request was larger than that provider's tokens-per-minute limit. With own-play on,
  those turns now go to her voice (Codex) like the others.
"""
from scripts.livepath import mabar


def _cfg(tmp_path, name="run", **kw):
    return mabar.mabar_bridge_config(tmp_path / name, vts=False, mic=1, **kw)


def test_voice_effort_is_low_with_the_heavy_class_ladder(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg["codex_effort"] == "low"
    assert cfg["codex_effort_berat"] == "high"
    assert cfg["codex_effort_kelas_berat"] == ["deep", "rant"]


def test_live_overlay_high_effort_does_not_win(tmp_path):
    cfg = _cfg(tmp_path, live={"codex_effort": "high"})
    assert cfg["codex_effort"] == "low"


def test_own_play_turns_route_to_her_voice(tmp_path):
    live = {"cursor_trigger_types": ["yt_chat", "curious", "game", "mic", "ptt", "mic_pasif"]}
    on = _cfg(tmp_path, "on", live=dict(live), own_play=True)
    assert "own_play" in on["cursor_trigger_types"]
    # the existing kinds stay, in order
    assert on["cursor_trigger_types"][:6] == live["cursor_trigger_types"]


def test_own_play_off_leaves_routing_unchanged(tmp_path):
    live = {"cursor_trigger_types": ["yt_chat", "mic"]}
    off = _cfg(tmp_path, "off", live=dict(live))
    assert off["cursor_trigger_types"] == ["yt_chat", "mic"]


def test_own_play_without_a_live_list_adds_only_own_play(tmp_path):
    base = _cfg(tmp_path, "base", live={})
    cfg = _cfg(tmp_path, "on", own_play=True, live={})
    before = list(base.get("cursor_trigger_types") or ["yt_chat"])
    assert cfg["cursor_trigger_types"] == before + ["own_play"]
