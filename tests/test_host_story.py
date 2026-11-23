import json
import os
import time
import uuid

import pytest

from arti_runtime import host_story, native_story
from arti_runtime.event_framing import MomentText


def row(seq, phase, **kw):
    base = dict(v=1, utc_ms=int(time.time() * 1000), runtime_id="host-test", relay_window=1,
                witness="host", save_id="452", host_player_id="-1", episode="story:9", seq=seq,
                phase=phase, event_id="60367", event_type="Event", date="1:spring:1", location="Farm",
                speaker="UNKNOWN", narration=False, text="", change="")
    base.update(kw)
    return json.dumps(base) + "\n"


_PRESENCE_EVENTS = {}


def _event_api():
    import ctypes
    from ctypes import wintypes

    from scripts.m1_operator.win_facts import _w

    _, k32 = _w()
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.restype = wintypes.BOOL
    return k32


def revoke_presence(path):
    _, _, handle = _PRESENCE_EVENTS.pop(path)
    k32 = _event_api()
    assert k32.ResetEvent(handle)
    assert k32.CloseHandle(handle)


def _presence_gate(path, runtime_id, window):
    if os.name != "nt":
        return None
    existing = _PRESENCE_EVENTS.get(path)
    identity = runtime_id, window
    if existing and existing[0] == identity:
        return existing[1]
    if existing:
        revoke_presence(path)
    if window is None:
        return None
    name = "Local\\ArtiHostStory-" + uuid.uuid4().hex
    handle = _event_api().CreateEventW(None, True, True, name)
    assert handle
    _PRESENCE_EVENTS[path] = identity, name, handle  # Windows closes the remaining fixture handles at process exit.
    return name


def presence(path, *, window=1, epoch=None, runtime_id="host-test", utc_ms=None, pid=None):
    path.with_name(path.stem + ".session.json").write_text(json.dumps(dict(
        window=epoch if epoch is not None else (window or 2),
        runtime_id=runtime_id, relay_window=window, utc_ms=int(time.time() * 1000) if utc_ms is None else utc_ms,
        pid=os.getpid() if pid is None else pid, gate=_presence_gate(path, runtime_id, window))), encoding="utf-8")


def write(path, *rows, partial="", publish=True, **state):
    if publish:
        presence(path, **state)
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(rows) + partial)


@pytest.fixture
def relay(tmp_path):
    return host_story.HostStoryRelay(tmp_path / "host_story.jsonl", mind_base=tmp_path / "mind")


def test_lines_reach_her_in_order_as_held_moment_facts(relay, tmp_path):
    write(relay.path, row(1, "started"), row(2, "line", speaker="Lewis", text="Welcome!"),
          row(3, "line", speaker="Robin", text="Hi there"))
    sent = []
    assert relay.tick(lambda text, trigger_type: sent.append((text, trigger_type)) or True)
    text, trigger = sent[0]
    assert trigger == "game" and isinstance(text, MomentText) and text._live7_hold
    assert text.index('Lewis: "Welcome!"') < text.index('Robin: "Hi there"')
    assert "60367" in text and "HOST" in text
    assert relay.pending == []


def test_busy_voice_keeps_fresh_lines_in_order(relay):
    write(relay.path, *[row(i, "line", speaker="A", text="line %d" % i) for i in range(1, 12)])
    assert not relay.tick(lambda *a, **k: pytest.fail("enqueued while busy"), ready=lambda: False)
    assert len(relay.pending) == 11
    assert not relay.tick(lambda *a, **k: False)  # refused by the queue: still pending
    assert len(relay.pending) == 11
    sent = []
    relay.tick(lambda text, **k: sent.append(text) or True)
    relay.tick(lambda text, **k: sent.append(text) or True)
    joined = " ".join(sent)
    assert all('"line %d"' % i in joined for i in range(1, 12)) and relay.pending == []


def test_partial_line_waits_but_restart_drops_pending_and_unread_lines(relay, tmp_path):
    write(relay.path, row(1, "line", speaker="A", text="one"), partial=row(2, "line", speaker="A", text="two")[:20])
    relay.poll()
    assert [p["text"] for p in relay.pending] == ["one"]
    write(relay.path, partial=row(2, "line", speaker="A", text="two")[20:] + row(3, "line", text="unread"))
    old_state = json.loads(relay.state_path.read_text(encoding="utf-8"))
    old_state.update(pending=relay.pending, context_lines=relay.context_lines, episodes=relay.episodes)
    relay.state_path.write_text(json.dumps(old_state), encoding="utf-8")
    again = host_story.HostStoryRelay(relay.path, mind_base=tmp_path / "mind")
    assert again.pending == [] and again.reply_context() == ""
    write(again.path, row(4, "line", text="new after restart"))
    again.poll()
    assert [p["text"] for p in again.pending] == ["new after restart"]


def test_coop_dialogue_and_mail_never_enter_voice_or_reply_context(relay):
    write(relay.path, row(1, "line", speaker="Robin", text="Private greeting", relay_window=None),
          row(2, "line", speaker="Evelyn", text="Private letter", relay_window=None), window=None)
    assert relay.reply_context() == ""
    assert not relay.tick(lambda *a, **k: pytest.fail("voiced a co-op line"))
    assert relay.pending == []
    assert native_story.recent(relay.mind_base, "452:777", 1) == []
    write(relay.path, row(3, "line", text="HOST played on alone", relay_window=3), window=3)
    spoken = []
    assert relay.tick(lambda text, **k: spoken.append(text) or True)
    assert "HOST played on alone" in spoken[0]


def test_disconnect_drops_pending_and_invalidates_already_queued_batch(relay):
    write(relay.path, *[row(i, "line", text=f"old batch {i}") for i in range(1, 12)])
    queued = []
    assert relay.tick(lambda text, **kw: queued.append(text) or True)
    assert queued[0].is_current() and len(relay.pending) == 3
    # Voice may miss the joined interval entirely. The HOST observed both edges.
    write(relay.path, row(12, "line", text="new offline line", relay_window=3), window=3)
    assert not queued[0].is_current()
    assert relay.tick(lambda text, **kw: queued.append(text) or True)
    assert "new offline line" in queued[1] and "old batch" not in queued[1]
    assert relay.pending == []


def test_old_event_cannot_enqueue_or_be_spoken_after_a_queue_wait(tmp_path):
    now = [100.]
    relay = host_story.HostStoryRelay(tmp_path / "host_story.jsonl", clock=lambda: now[0])
    write(relay.path, row(1, "line", text="letter from a minute ago", utc_ms=100_000), utc_ms=100_000)
    queued = []
    assert relay.tick(lambda text, **kw: queued.append(text) or True)
    now[0] = 161.
    write(relay.path, row(2, "line", text="stale egg-hunt replay", utc_ms=100_000),
          row(3, "line", text="new scene", utc_ms=161_000), utc_ms=161_000)
    assert not queued[0].is_current()
    assert relay.tick(lambda text, **kw: queued.append(text) or True)
    assert "new scene" in queued[1] and "egg-hunt" not in queued[1]
    assert "letter from a minute ago" not in relay.reply_context()


def test_line_appended_during_tail_open_is_not_lost(tmp_path, monkeypatch):
    now = [100.]
    relay = host_story.HostStoryRelay(tmp_path / "host_story.jsonl", clock=lambda: now[0])
    presence(relay.path, utc_ms=100_000)
    original = host_story.ipc.open_shared

    def append_before_open(path, *args, **kwargs):
        if path == relay.path and now[0] == 100.:
            now[0] = 101.
            write(path, row(1, "line", text="New during read", utc_ms=101_000), utc_ms=101_000)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(host_story.ipc, "open_shared", append_before_open)
    assert "New during read" in relay.reply_context()


def test_tail_waits_for_background_publication_of_new_offline_window(tmp_path):
    now = [100.]
    relay = host_story.HostStoryRelay(tmp_path / "host_story.jsonl", clock=lambda: now[0])
    presence(relay.path, window=None, epoch=2, utc_ms=100_000)
    now[0] = 101.
    write(relay.path, row(1, "line", text="HOST plays on", relay_window=3, utc_ms=101_000), publish=False)
    assert relay.reply_context() == "" and relay.offset == relay.last_seq == 0
    presence(relay.path, window=3, utc_ms=101_000)
    assert "HOST plays on" in relay.reply_context()
    assert relay.last_seq == 1


def test_same_window_heartbeat_during_read_keeps_pending_and_context(tmp_path, monkeypatch):
    now = [100.]
    path = tmp_path / "host_story.jsonl"
    relay = host_story.HostStoryRelay(path, clock=lambda: now[0])
    write(path, row(1, "line", text="Keep this line", utc_ms=100_000), utc_ms=100_000)
    relay.poll()
    original = host_story.ipc.read_json

    def heartbeat_while_reading(file):
        if file == relay.presence_path and now[0] == 100.:
            now[0] = 101.
            presence(path, utc_ms=101_000)
        return original(file)

    monkeypatch.setattr(host_story.ipc, "read_json", heartbeat_while_reading)
    assert "Keep this line" in relay.reply_context()
    assert len(relay.pending) == 1


@pytest.mark.parametrize("termination", ["title", "missing heartbeat", "session stop", "cycle stop", "voice stop"])
def test_end_discards_backlog_and_rejects_queued_line(tmp_path, termination):
    from arti_help_relay import HelpRelay

    now = [100.]
    path = tmp_path / "host_story.jsonl"
    config = dict(host_story_relay_file=str(path), arti_mind_dir=str(tmp_path / "mind"),
                  live7_status_facts_enabled=False)
    queued = []
    help_relay = HelpRelay(config, lambda text, **kw: queued.append(text) or True, clock=lambda: now[0])
    write(path, *[row(i, "line", text=f"line {i}", utc_ms=100_000) for i in range(1, 11)], utc_ms=100_000)
    help_relay.tick()
    assert queued[0].is_current() and help_relay._host_story.pending
    if termination == "title":
        presence(path, window=None, utc_ms=100_000)
    elif termination == "missing heartbeat":
        now[0] += 4
    elif termination == "session stop":
        (tmp_path / "bridge-stop-request").touch()
    elif termination == "cycle stop":
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime" / "LIVE7_STOP").touch()
    else:
        help_relay.stop()
    assert not queued[0].is_current()
    help_relay.tick()
    assert len(queued) == 1 and help_relay._host_story.pending == []
    assert "line 1" not in help_relay.reply_facts()


def test_owner_can_relay_after_farmhand_cycle_has_drained(tmp_path):
    from arti_help_relay import HelpRelay, Live7HelpChannelResolver
    from arti_stardew_stop import write_sentinel

    cycle = tmp_path / "runtime" / "cycles" / "ended"
    for path in (tmp_path / "runtime" / "LIVE7_STOP", cycle / "LIVE7_STOP"):
        write_sentinel(path, {}, writer="supervisor.host_left")
    (cycle / "WATCH_STOP").touch()
    path = tmp_path / "host_story.jsonl"
    config = dict(host_story_relay_file=str(path), arti_mind_dir=str(tmp_path / "mind"),
                  live7_status_facts_enabled=False)
    sent = []
    resolver = Live7HelpChannelResolver(config, active_cycles=lambda *_a, **_kw: ())
    resolver.cycle_dir = cycle
    relay = HelpRelay(config, lambda text, **kw: sent.append(text) or True, channel_resolver=resolver)
    write(path, row(1, "line", text="HOST kept playing"))
    relay.tick()
    assert relay.game_session_stopped()
    assert len(sent) == 1 and "HOST kept playing" in sent[0] and sent[0].is_current()


@pytest.mark.parametrize("bad", ['{"v":2}', "not json", json.dumps(dict(v=1, witness="farmhand", phase="line", seq=5,
                                                                           episode="e", save_id="1"))])
def test_rows_outside_the_contract_are_ignored(relay, bad):
    write(relay.path, bad + "\n")
    relay.poll()
    assert relay.pending == [] and relay.invalid == 1


def test_episode_is_remembered_for_her_scope_on_the_same_save(relay, tmp_path):
    write(relay.path, row(1, "started"), row(2, "line", speaker="Lewis", text="This farm is yours now."),
          row(3, "ended"))
    relay.poll()
    base = tmp_path / "mind"
    shown = native_story.recent(base, "452:777", 1)
    assert len(shown) == 1
    assert "This farm is yours now." in shown[0]["text"] and "HOST" in shown[0]["text"]
    assert native_story.recent(base, "999:777", 1) == []  # another save never sees it


def test_host_scope():
    assert host_story.host_scope("452:777") == "452:host"
    assert host_story.host_scope(None) is None


def test_help_relay_is_unchanged_without_the_flag(tmp_path):
    from arti_help_relay import HelpRelay
    relay = HelpRelay({}, lambda *a, **k: True)
    assert relay._host_story is None
    relay = HelpRelay(dict(host_story_relay_file=str(tmp_path / "host_story.jsonl"),
                           arti_mind_dir=str(tmp_path / "mind")), lambda *a, **k: True)
    assert relay._host_story is not None


def test_opening_before_the_save_exists_is_voiced_then_bound_and_remembered(relay, tmp_path):
    write(relay.path, row(1, "started", save_id="0", date="1:spring:0", event_type="GrandpaStory", event_id="UNKNOWN",
                          episode="story:1"),
          row(2, "line", save_id="0", date="1:spring:0", event_type="GrandpaStory", episode="story:1",
              text="Grandpa's words"),
          row(3, "ended", save_id="0", date="1:spring:0", event_type="GrandpaStory", episode="story:1"))
    sent = []
    relay.tick(lambda text, **k: sent.append(text) or True)
    assert "Grandpa's words" in sent[0]                       # voiced even with no save identity yet
    base = tmp_path / "mind"
    assert native_story.recent(base, "452:777", 1) == []      # not yet bound to any save
    write(relay.path, row(4, "started", episode="story:2"), row(5, "ended", episode="story:2"))
    relay.poll()                                              # a later row names save 452
    shown = native_story.recent(base, "452:777", 1)
    assert any("Grandpa's words" in s["text"] and "1:spring:0" in s["text"] for s in shown)
