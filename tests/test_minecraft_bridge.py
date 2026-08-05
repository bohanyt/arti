"""Tests integrasi Minecraft di bridge: prioritas queue, console mc, konteks."""

from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_voice_queue as avq  # noqa: E402
import hermes_vtuber_bridge as bridge  # noqa: E402


# ---------------------------------------------------------------------------
# Prioritas & kekebalan cull trigger "game"
# ---------------------------------------------------------------------------

def test_game_priority_below_mic_above_default():
    assert avq._TRIGGER_PRIORITY["mic"] < avq._TRIGGER_PRIORITY["game"]
    assert avq._TRIGGER_PRIORITY["game"] < avq._DEFAULT_PRIORITY


def test_game_not_culled_like_curious():
    q = avq.VoiceTriggerQueue()
    q.enqueue(avq.QueuedVoiceTrigger(text="[chat]", trigger_type="yt_chat",
                                     viewer_name="@x"))
    # curious ditolak saat yt pending; game (reaksi kematian) TIDAK boleh ikut
    assert q.enqueue(avq.QueuedVoiceTrigger(text="c", trigger_type="curious")) is False
    assert q.enqueue(avq.QueuedVoiceTrigger(
        text="[MINECRAFT] mati", trigger_type="game")) is True
    assert q.drop_curious() == 0  # game bukan curious — tidak kena sapu
    # dequeue: manusia (yt_chat) menang atas game
    assert q.dequeue().trigger_type == "yt_chat"
    assert q.dequeue().trigger_type == "game"


def test_game_trigger_does_not_bump_life_detector(monkeypatch):
    # Event bot != manusia: detektor kehidupan tidak boleh bangun karenanya.
    monkeypatch.setattr(bridge, "_last_human_activity_ts", 123.0)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    bridge.queue_voice_trigger("[MINECRAFT] Kamu mati", trigger_type="game")
    assert bridge._last_human_activity_ts == 123.0
    # bersihkan antrian supaya test lain tidak kecipratan
    while not bridge.voice_trigger_queue.empty():
        bridge.voice_trigger_queue.get_nowait()


# ---------------------------------------------------------------------------
# Console `mc ...` (pola test_text_input)
# ---------------------------------------------------------------------------

def _run_worker(monkeypatch, lines: str):
    triggers: list[tuple] = []
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(lines))
    monkeypatch.setattr(
        bridge, "queue_voice_trigger",
        lambda text, trigger_type="mic", viewer_name=None, **kw: triggers.append(
            (text, trigger_type)
        ),
    )
    monkeypatch.setattr(
        bridge, "add_to_history",
        lambda source, message, arti_meta=None: None,
    )
    bridge.text_input_worker()
    return triggers


def test_mc_commands_require_enabled(monkeypatch, capsys):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", False)
    triggers = _run_worker(monkeypatch, "mc on\n")
    assert triggers == []  # bukan pesan streamer — jangan jadi trigger mic
    assert "minecraft_enabled=False" in capsys.readouterr().out


def test_mc_on_starts_runner(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    called = []
    monkeypatch.setattr(bridge, "_start_minecraft_runner", lambda: called.append(1))
    triggers = _run_worker(monkeypatch, "mc on\n")
    assert called == [1] and triggers == []


def test_mc_status_without_runner(monkeypatch, capsys):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", None)
    triggers = _run_worker(monkeypatch, "mc status\n")
    assert triggers == []
    assert "belum pernah join" in capsys.readouterr().out


def test_mc_manual_verb_validated_and_bad_rejected(monkeypatch, capsys):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    executed = []
    monkeypatch.setattr(bridge, "_execute_mc_tag", executed.append)
    _run_worker(monkeypatch, "mc say halo bohan\nmc teleport 1 2 3\n")
    assert executed == [{"cmd": "say", "text": "halo bohan"}]
    assert "tidak dikenal" in capsys.readouterr().out


def test_plain_message_still_mic_trigger(monkeypatch):
    # Regresi: klausa mc tidak boleh menelan pesan streamer biasa.
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    triggers = _run_worker(monkeypatch, "arti mck kamu lucu\n")
    assert triggers == [("arti mck kamu lucu", "mic")]


# ---------------------------------------------------------------------------
# _execute_mc_tag & konteks [DI MINECRAFT]
# ---------------------------------------------------------------------------

class _StubRunner:
    def __init__(self):
        self.sent: list[dict] = []
        self.last_status = {
            "ev": "status", "health": 17, "food": 9, "task": "follow",
            "dim": "overworld", "is_night": False,
            "pos": {"x": 1, "y": 2, "z": 3},
            "nearby_players": [{"name": "bohanyto", "distance": 4}],
            "nearby_hostiles": [],
        }

    def is_active(self):
        return True

    def send_command(self, cmd):
        self.sent.append(cmd)
        return True

    def events_snapshot(self):
        return []


def test_execute_tag_forwards_to_runner(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    stub = _StubRunner()
    monkeypatch.setattr(bridge, "_minecraft_runner", stub)
    bridge._execute_mc_tag({"cmd": "follow"})
    assert stub.sent == [{"cmd": "follow"}]


def test_execute_tag_join_leave_hit_lifecycle(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    calls = []
    monkeypatch.setattr(bridge, "_start_minecraft_runner", lambda: calls.append("start"))
    monkeypatch.setattr(bridge, "_stop_minecraft_runner", lambda: calls.append("stop"))
    bridge._execute_mc_tag({"cmd": "join"})
    bridge._execute_mc_tag({"cmd": "leave"})
    assert calls == ["start", "stop"]


def test_execute_tag_disabled_is_noop(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", False)
    stub = _StubRunner()
    monkeypatch.setattr(bridge, "_minecraft_runner", stub)
    bridge._execute_mc_tag({"cmd": "follow"})
    assert stub.sent == []


def test_append_context_disabled_untouched(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", False)
    assert bridge._append_minecraft_context("SYS") == "SYS"


def test_append_context_idle_offers_join_tag(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", None)
    out = bridge._append_minecraft_context("SYS")
    assert "[MC: join]" in out and "[DI MINECRAFT" not in out


def test_append_context_active_has_status_and_rules(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", _StubRunner())
    out = bridge._append_minecraft_context("SYS")
    assert "[DI MINECRAFT" in out and "BUKAN yang terlihat di layar" in out
    assert "HP 17/20" in out
    assert "[AKSI MINECRAFT]" in out and "MAKSIMAL SATU tag" in out
    assert "JANGAN pernah menyebut" in out


def test_initiative_materials_carry_minecraft_note(monkeypatch):
    monkeypatch.setitem(bridge.CONFIG, "minecraft_enabled", True)
    monkeypatch.setattr(bridge, "_minecraft_runner", _StubRunner())
    mats = bridge._initiative_materials()
    assert "HP 17/20" in mats["minecraft_note"]
