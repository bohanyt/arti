"""Bridge said.json: owner/chat speech as one game-page consideration. No live IO."""
import threading

import arti_help_bus as bus
import arti_help_relay as relay_module
from arti_help_relay import HelpRelay
from arti_stardew_live7_help import Live7HelpMixin


FISH = "Coba mancing bisa nggak?"
PARSNIP = (
    "Eh, arti, itu parsnipnya kamu ambil, kamu jual, kamu beli tas. Bisa nggak?"
)


def row(who="Bohan", platform="mic", text="coba mancing", at=1000.0, **changes):
    return dict(dict(kind="said_to_you", who=who, platform=platform, text=text, at=at), **changes)


def make_request(number=1, **changes):
    values = dict(request_id=f"{number:032x}", session_id="fake-session",
                  reason="no_progress_frozen_clock", game_date="Y1 Spring 3",
                  game_time=910, location="Farm", skill="walk", detail="clock frozen at 0910",
                  created_at=10000.0, expires_at=20000.0)
    values.update(changes)
    return bus.HelpRequest(**values)


def mixin(monkeypatch, now=1020.0):
    import arti_stardew_live7_help as H
    monkeypatch.setattr(H.time, "time", lambda: now)
    rt = Live7HelpMixin()
    rt._config = dict(live7_help_enabled=True)
    rt._lock = threading.RLock()
    rt._live7_discovery_scope = "save:actor"
    rt._live7_init_help(None)
    return rt


def test_file_channel_round_trip_cap_malformed_and_not_pending(tmp_path):
    channel = bus.FileHelpChannel(tmp_path)
    assert channel.read_said() == []
    assert channel.list_pending_requests() == ()
    first = [row(text=f"line {i}", at=1000.0 + i) for i in range(5)]
    first[1] = dict(kind="hint", who="Bohan", platform="mic", text="nope", at=1001.0)
    first.append(dict(kind="said_to_you", who="x" * 41, platform="mic", text="long", at=1006.0))
    channel.write_said(first)
    got = channel.read_said()
    assert [item["text"] for item in got] == ["line 2", "line 3", "line 4"]
    assert all(item["kind"] == "said_to_you" and set(item) == {"kind", "who", "platform", "text", "at"}
               for item in got)
    extra = row(text="keep extra", extra="drop")
    channel.write_said([extra])
    assert channel.read_said() == [row(text="keep extra")]
    (tmp_path / "said.json").write_text("{partial", encoding="utf-8")
    assert channel.read_said() == []
    (tmp_path / "said.json").write_text("null", encoding="utf-8")
    assert channel.read_said() == []
    channel.write_said([row()])
    channel.write_request(make_request())
    assert channel.list_pending_requests() == (make_request().request_id,)
    assert channel.pending_requests() == (make_request(),)
    assert "said" not in channel.list_pending_requests()


def test_note_said_rate_cap_replace_censor_and_no_window(tmp_path, monkeypatch):
    bare = HelpRelay({}, lambda *_a, **_kw: True)
    bare.note_said("Bohan", "mic", "tanpa jendela", now=1.0)
    assert bare._window is None and bare._said[0]["text"] == "tanpa jendela"
    channel = bus.FileHelpChannel(tmp_path)
    relay = HelpRelay({"speech_censor_enabled": True}, lambda *_a, **_kw: True,
                      file_channel=channel, clock=lambda: 1000.0)
    assert relay._window is None
    relay.note_said("Bohan", "mic", "coba mancing", now=1000.0)
    relay.note_said("Bohan", "mic", "coba mancing lagi", now=1010.0)
    relay.note_said("Mia", "youtube", "jangan goblok", viewer=True, now=1011.0)
    assert [(item["who"], item["text"]) for item in relay._said] == [
        ("Bohan", "coba mancing lagi"), ("Mia", "jangan sensor"),
    ]
    assert relay._window is None
    assert channel.read_said() == relay._said
    relay.note_said("Bohan", "mic", FISH, now=1030.0)
    assert [item["text"] for item in relay._said] == [
        "coba mancing lagi", "jangan sensor", FISH,
    ]
    monkeypatch.setattr(
        relay_module.arti_speech_censor, "censor_from_config",
        lambda text, cfg: "CENSORED",
    )
    relay.note_said("Mia", "youtube", "kata kasar", viewer=True, now=1031.0)
    assert relay._said[-1]["text"] == "CENSORED"
    class Boom:
        def write_said(self, _rows):
            raise OSError("disk")

    relay._channel = Boom()
    relay.note_said("Bohan", "mic", "tetap jalan", now=1060.0)


def test_relay_to_mixin_page_line(tmp_path, monkeypatch):
    channel = bus.FileHelpChannel(tmp_path)
    relay = HelpRelay({"streamer_name": "Bohan"}, lambda *_a, **_kw: True,
                      file_channel=channel, clock=lambda: 1000.0)
    relay.note_said("Bohan", "mic", "coba mancing bareng", now=1000.0)
    rt = mixin(monkeypatch, now=1000.0)
    assert rt.accept_said_to_you(channel.read_said()) == 1
    lines = rt._live7_said_lines("save:actor")
    assert len(lines) == 1
    assert lines[0].startswith("Dikatakan ke kamu soal game:")
    assert "coba mancing bareng" in lines[0]


def test_real_owner_lines_reach_page_with_rate_spacing(tmp_path, monkeypatch):
    clock = [1000.0]
    channel = bus.FileHelpChannel(tmp_path)
    relay = HelpRelay({"streamer_name": "Bohan"}, lambda *_a, **_kw: True,
                      file_channel=channel, clock=lambda: clock[0])
    relay.note_owner_text(FISH)
    clock[0] += 20.0
    relay.note_owner_text(PARSNIP)
    assert [item["text"] for item in channel.read_said()] == [FISH, PARSNIP]
    rt = mixin(monkeypatch, now=1020.0)
    assert rt.accept_said_to_you(channel.read_said()) == 2
    lines = rt._live7_said_lines("save:actor")
    assert len(lines) == 2
    assert all(line.startswith("Dikatakan ke kamu soal game:") for line in lines)
    assert FISH in lines[0] and PARSNIP in lines[1]


def test_note_owner_text_still_closes_help_window(tmp_path):
    replies = []
    channel = bus.FileHelpChannel(tmp_path)
    relay = HelpRelay({}, lambda *_a, **_kw: True, file_channel=channel,
                      reply_sink=replies.append, clock=lambda: 10000.0)
    relay.submit(make_request())
    relay.note_owner_text("jalan utara @synthetic-name https://example.org")
    assert replies[0].status == "hinted" and replies[0].spoken
    assert [(hint.source, hint.text) for hint in replies[0].hints] == [("owner", "jalan utara")]
    assert relay._window is None
    assert channel.read_said()[0]["text"] == "jalan utara"
    assert channel.read_said()[0]["who"] == "owner"
    assert channel.read_said()[0]["platform"] == "mic"
    relay.note_owner_text("jangan diulang")
    assert len(replies) == 1
