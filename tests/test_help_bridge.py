"""Default-off bridge help hooks with fake runtime/chat/voice dependencies."""

from pathlib import Path
import json
import queue
import time
from types import SimpleNamespace

import pytest

import arti_bridge as bridge
from arti_help_bus import HelpRequest
import arti_help_relay as relay_module
from arti_help_relay import HelpRelay


def make_request():
    return HelpRequest(request_id="e" * 32, session_id="fake-session", reason="stuck_clock",
                       game_date="Y1 Spring 3", game_time=910, location="Farm", skill=None,
                       detail="frozen clock", created_at=10000, expires_at=10600)


@pytest.fixture
def rig(monkeypatch):
    clock, voice, replies = [10000.0], [], []
    relay = HelpRelay({}, lambda *_a, **_kw: True, reply_sink=replies.append, clock=lambda: clock[0])
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_speak_enabled", True)
    monkeypatch.setattr(bridge, "_live7_help_relay", relay)
    monkeypatch.setattr(bridge, "_stardew_runtime", None)
    monkeypatch.setattr(bridge, "_brain_busy", False)
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge, "_host_mode", False)
    monkeypatch.setattr(bridge, "_note_screen_privacy", lambda *_a: None)
    monkeypatch.setattr(bridge, "_note_streamer_text_for_afk", lambda *_a: None)
    monkeypatch.setattr(bridge.session_transcript, "log_trigger", lambda *_a: "fake-turn")
    monkeypatch.setitem(bridge.CONFIG, "voice_queue_enabled", False)
    monkeypatch.setattr(bridge, "voice_trigger_queue", queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    return relay, clock, voice, replies


@pytest.mark.parametrize("kind", ["ptt", "mic", "wake_word"])
def test_owner_capture_keeps_runtime_ingress_and_normal_queue(rig, monkeypatch, kind):
    relay, _, _, replies = rig
    ingress = []
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress",
                        lambda *args: ingress.append(args) or 7)
    relay.submit(make_request())
    assert bridge.queue_voice_trigger("jalan utara", trigger_type=kind)
    assert ingress[0][:3] == (kind, None, "jalan utara")
    trigger = bridge.voice_trigger_queue.get_nowait()
    assert trigger.text == "jalan utara" and trigger.trigger_type == kind
    assert trigger.stardew_voice_epoch == 7
    assert replies[0].hints[0].source == "owner"


def test_stale_owner_epoch_and_viewer_metadata_do_not_capture(rig, monkeypatch):
    relay, _, _, replies = rig
    relay.submit(make_request())
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress", lambda *_a: -1)
    assert not bridge.queue_voice_trigger("stale", trigger_type="ptt")
    assert not replies
    monkeypatch.setattr(bridge, "_note_stardew_owner_ingress", lambda *_a: None)
    assert bridge.queue_voice_trigger("untrusted", trigger_type="mic", viewer_name="synthetic")
    assert not replies


def test_owner_hint_and_voice_are_preserved_while_busy(rig, monkeypatch):
    relay, _, _, replies = rig
    relay.submit(make_request())
    monkeypatch.setattr(bridge, "_brain_busy", True)
    assert bridge.queue_voice_trigger("utara", trigger_type="ptt")
    assert replies[0].status == "hinted"
    assert replies[0].hints[0].source == "owner"
    assert replies[0].hints[0].text == "utara"
    trigger = bridge.voice_trigger_queue.get_nowait()
    assert (trigger.text, trigger.trigger_type) == ("utara", "ptt")
    assert bridge.voice_trigger_queue.empty()


def test_feature_off_keeps_owner_routing_without_hint_capture(rig, monkeypatch):
    relay, _, _, replies = rig
    relay.submit(make_request())
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    assert bridge.queue_voice_trigger("utara", trigger_type="mic")
    assert not replies


def test_help_game_gate_without_minecraft_still_expires_and_preempts(rig, monkeypatch):
    relay, clock, _, _ = rig
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    monkeypatch.setattr(bridge.time, "time", lambda: clock[0])
    monkeypatch.setitem(bridge.CONFIG, "minecraft_reaction_ttl_sec", 10)
    trigger = bridge.VoiceTrigger("[Stardew] Aku buntu: stuck clock", "game", None, clock[0])
    assert not bridge._game_reaction_expired(trigger)
    assert bridge._game_reaction_expired(bridge.VoiceTrigger("ordinary game", "game", None, clock[0]))
    clock[0] += 11
    assert bridge._game_reaction_expired(trigger)
    fresh = bridge.VoiceTrigger(trigger.text, "game", None, clock[0])
    assert not bridge._game_reaction_expired(fresh)
    clock[0] += 1
    assert bridge.queue_voice_trigger("paid event", trigger_type="donation")
    assert bridge._game_reaction_expired(fresh)
    fresh = bridge.VoiceTrigger(trigger.text, "game", None, clock[0] + 1)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_speak_enabled", False)
    assert bridge._game_reaction_expired(fresh)


def test_help_gate_exception_is_feature_gated(rig, monkeypatch):
    _, clock, _, _ = rig
    monkeypatch.setattr(bridge, "_mc_runner_active", lambda: False)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    trigger = bridge.VoiceTrigger("[Stardew] Aku buntu: stuck clock", "game", None, clock[0])
    assert bridge._game_reaction_expired(trigger)


class FakeThread:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass

    def join(self, **_kwargs):
        pass


@pytest.mark.parametrize("has_hooks", [False, True])
def test_startup_runtime_feature_detection_and_shutdown(monkeypatch, tmp_path, has_hooks):
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", False)
    received = []
    runtime = SimpleNamespace()
    if has_hooks:
        runtime.set_help_sink = lambda sink: received.append(sink)
        runtime.accept_help_hint = lambda *args: received.append(args)
    monkeypatch.setattr(bridge, "_stardew_runtime", runtime)
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_dir", str(tmp_path))
    monkeypatch.setitem(bridge.CONFIG, "live7_help_speak_enabled", False)
    monkeypatch.setattr(relay_module, "HelpRelay", lambda *args, **kwargs: HelpRelay(
        *args, **kwargs, clock=lambda: 10000, thread_factory=FakeThread))
    bridge._start_live7_help_if_enabled()
    relay = bridge._live7_help_relay
    bridge._start_live7_help_if_enabled()
    assert bridge._live7_help_relay is relay
    assert relay._channel.help_dir == tmp_path
    if has_hooks:
        received[0](make_request())
        relay.note_owner_text("utara")
        assert received[1][0] == make_request().request_id
    else:
        assert received == []
    bridge._stop_live7_help_relay()
    bridge._stop_live7_help_relay()
    assert bridge._live7_help_relay is None and relay._stop_event.is_set()


def test_disabled_startup_does_not_construct_relay(monkeypatch, tmp_path):
    monkeypatch.setitem(bridge.CONFIG, "live7_status_facts_enabled", False)
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", False)
    monkeypatch.setitem(bridge.CONFIG, "live7_moments_enabled", False)
    # A configured run/command dir (as in real config_local files) must not
    # start the relay by itself: off means off.
    monkeypatch.setitem(bridge.CONFIG, "live7_help_dir", str(tmp_path))
    monkeypatch.setitem(bridge.CONFIG, "stardew_command_dir", str(tmp_path / "command"))

    def fail(*_a, **_kw):
        pytest.fail("disabled help must not construct relay")

    monkeypatch.setattr(relay_module, "HelpRelay", fail)
    bridge._start_live7_help_if_enabled()
    assert bridge._live7_help_relay is None


class FakeChatSession:
    def __init__(self, messages):
        self.headers = {}
        self.cookies = SimpleNamespace(set=lambda *_a, **_kw: None)
        actions = [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
            "id": f"synthetic-{index}", "authorName": {"simpleText": viewer},
            "timestampUsec": str(int(time.time() * 1000000)),
            "message": {"runs": [{"text": text}]},
        }}}} for index, (viewer, text) in enumerate(messages)]
        data = {"contents": {"liveChatRenderer": {"continuation": "x" * 24, "actions": actions}}}
        self.text = "var ytInitialData = " + json.dumps(data) + ";"

    def get(self, *_a, **_kw):
        return SimpleNamespace(raise_for_status=lambda: None, text=self.text)

    def post(self, *_a, **_kw):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})


def run_chat(monkeypatch, messages):
    history, voice, donations = [], [], []
    monkeypatch.setitem(bridge.CONFIG, "youtube_chat_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "youtube_video_id", "synthetic-help-test")
    monkeypatch.setattr(bridge, "_yt_chat_running", True)
    monkeypatch.setattr(bridge, "_yt_pernah_chat", set())
    monkeypatch.setattr(bridge, "_last_yt_trigger_by_viewer", {})
    monkeypatch.setattr(bridge, "is_bot_viewer", lambda viewer, _cfg: viewer == "synthetic-bot")
    monkeypatch.setattr(bridge.requests, "Session", lambda: FakeChatSession(messages))
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a: bridge.stop_youtube_chat())
    monkeypatch.setattr(bridge, "add_to_history", lambda *args: history.append(args))
    monkeypatch.setattr(bridge, "queue_voice_trigger", lambda *args, **kwargs: voice.append((args, kwargs)) or True)
    monkeypatch.setattr(bridge, "schedule_donation_trigger", lambda *args, **kwargs: donations.append(args))
    bridge.youtube_chat_worker()
    return history, voice, donations


def test_help_commands_consumed_before_generic_skip_without_reply(rig, monkeypatch):
    relay, _, _, replies = rig
    relay.submit(make_request())
    history, voice, donations = run_chat(monkeypatch, [
        ("synthetic-one", "!bantu jalan utara"),
        ("synthetic-bot", "!tolong bot advice"),
    ])
    assert not history and not voice and not donations and not bridge._yt_pernah_chat
    relay.note_owner_text("coba utara")
    assert len(replies[0].hints) == 2
    assert [hint.source for hint in replies[0].hints] == ["viewer", "owner"]
    assert all("synthetic" not in hint.text for hint in replies[0].hints)


@pytest.mark.parametrize("enabled", [False, True])
def test_generic_bang_skip_unchanged_outside_window(rig, monkeypatch, enabled):
    relay, _, _, replies = rig
    monkeypatch.setitem(bridge.CONFIG, "live7_help_enabled", enabled)
    history, voice, donations = run_chat(monkeypatch, [
        ("synthetic-one", "!bantu jalan utara"),
        ("synthetic-two", "!tolong tangga"),
        ("synthetic-three", "!nasi"),
    ])
    assert history == voice == donations == replies == []
    assert not bridge._yt_pernah_chat and not bridge._last_yt_trigger_by_viewer
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    assert ('        if not paid and chat_msg.lstrip().startswith("!"):\n'
            '            print(f"[YT Chat Info] Perintah bot dari {viewer} — dilewati.")\n'
            '            return\n') in source


def test_help_hook_preserves_paid_command_exception(rig, monkeypatch):
    relay, _, _, _ = rig
    relay.submit(make_request())
    original = bridge.extract_superchat
    monkeypatch.setattr(bridge, "extract_superchat", lambda item: {"paid_amount": "USD 1"})
    history, voice, donations = run_chat(monkeypatch, [("synthetic-one", "!bantu paid advice")])
    assert len(history) == len(donations) == 1 and not voice
    assert not relay._window.hints


def test_shutdown_stops_relay_before_runtime_and_slow_pipeline():
    source = Path(bridge.__file__).read_text(encoding="utf-8")
    shutdown = source[source.index("# PALING AWAL: tutup telinga"):]
    assert shutdown.index("stop_youtube_chat()") < shutdown.index("_stop_live7_help_relay()")
    assert shutdown.index("_stop_live7_help_relay()") < shutdown.index("arti_agy_agent.shutdown_session()")
    assert shutdown.index("_stop_live7_help_relay()") < shutdown.index("_stop_stardew_runtime()")


def test_config_defaults_are_published():
    expected = dict(live7_help_enabled=False, live7_help_dir="", live7_help_min_gap_sec=900,
                    live7_help_max_per_hour=3, live7_help_speak_enabled=True,
                    live7_help_window_sec=180, live7_help_viewer_hints=5)
    assert {key: bridge.CONFIG[key] for key in expected} == expected


def test_recreated_runtime_rebinds_help_sink_and_stop_unbinds(monkeypatch):
    """A runtime re-created mid-session (e.g. trial restart) gets the relay sink again."""
    relay = HelpRelay({}, lambda *_a, **_kw: True, clock=lambda: 10000.0)
    old_sinks, new_sinks = [], []
    old_runtime = SimpleNamespace(set_help_sink=old_sinks.append, stop=lambda: None)
    new_runtime = SimpleNamespace(set_help_sink=new_sinks.append, stop=lambda: None)
    monkeypatch.setattr(bridge, "_live7_help_relay", relay)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", None)
    monkeypatch.setitem(bridge.CONFIG, "stardew_runtime_enabled", True)

    import arti_stardew_runtime
    runtimes = iter([old_runtime, new_runtime])
    monkeypatch.setattr(arti_stardew_runtime, "construct_if_enabled", lambda cfg: next(runtimes))

    bridge._start_stardew_runtime_if_enabled()
    assert old_sinks == [relay.submit] and relay._runtime is old_runtime
    bridge._stop_stardew_runtime()
    assert relay._runtime is None
    bridge._start_stardew_runtime_if_enabled()
    assert new_sinks == [relay.submit] and relay._runtime is new_runtime
    bridge._stop_stardew_runtime()


def test_runtime_start_without_relay_is_unchanged(monkeypatch):
    monkeypatch.setattr(bridge, "_live7_help_relay", None)
    monkeypatch.setattr(bridge, "_stardew_bridge_trial", None)
    monkeypatch.setitem(bridge.CONFIG, "stardew_runtime_enabled", False)
    bridge._start_stardew_runtime_if_enabled()
    assert bridge._stardew_runtime is None
