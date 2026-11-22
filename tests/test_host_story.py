import json

import pytest

import arti_mind
from arti_runtime import host_story, native_story
from arti_runtime.event_framing import MomentText


def row(seq, phase, **kw):
    base = dict(v=1, utc_ms=1, witness="host", save_id="452", host_player_id="-1", episode="story:9", seq=seq,
                phase=phase, event_id="60367", event_type="Event", date="1:spring:1", location="Farm",
                speaker="UNKNOWN", narration=False, text="", change="")
    base.update(kw)
    return json.dumps(base) + "\n"


def write(path, *rows, partial=""):
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


def test_busy_voice_keeps_lines_and_never_drops_them(relay):
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


def test_partial_line_waits_and_restart_resumes_without_repeats(relay, tmp_path):
    write(relay.path, row(1, "line", speaker="A", text="one"), partial=row(2, "line", speaker="A", text="two")[:20])
    relay.poll()
    assert [p["text"] for p in relay.pending] == ["one"]
    again = host_story.HostStoryRelay(relay.path, mind_base=tmp_path / "mind")
    assert [p["text"] for p in again.pending] == ["one"]
    with relay.path.open("a", encoding="utf-8") as f:
        f.write(row(2, "line", speaker="A", text="two")[20:])
    again.poll()
    assert [p["text"] for p in again.pending] == ["one", "two"]


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
