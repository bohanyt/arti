"""Viewer presence comes from session events/current authors, never memory."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import arti_bridge as b
import arti_curious as curious
import arti_vault_rag as rag


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(b, "_SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(b.time, "time", lambda: 10000.0)
    monkeypatch.setattr(b, "_viewer_session_started_ts", 9000.0, raising=False)
    monkeypatch.setattr(b, "_yt_viewers_seen", {})
    monkeypatch.setattr(b, "_yt_viewer_count", -1)
    monkeypatch.setattr(b, "_viewer_join_note", "")
    monkeypatch.setattr(b, "_viewer_join_note_ts", 0.0)
    monkeypatch.setattr(b, "CONFIG", dict(b.CONFIG, scouter_last_result={}))
    monkeypatch.setattr(b, "stream_history", b.collections.deque(maxlen=50))
    monkeypatch.setattr(b.session_transcript, "append_from_history", Mock())
    monkeypatch.setattr(b.arti_benang, "catat", Mock())
    monkeypatch.setattr(b.arti_renungan, "catat", Mock())
    monkeypatch.setattr(b.arti_renungan, "pop_kesimpulan", lambda: "")
    for name in ("_last_yt_chat_ts", "_last_yt_chat_gap_sec", "_last_human_activity_ts",
                 "_last_streamer_speech_ts", "_last_arti_reply_ts"):
        monkeypatch.setattr(b, name, 0.0)
    curious.reset_session()
    yield tmp_path
    curious.reset_session()


def test_only_recent_session_chat_authors_are_present(session):
    b._yt_viewers_seen.update({
        "@Active": 9990.0, "@WindowEdge": 9400.0,
        "@Stale": 9399.0, "@PreviousSession": 8999.0,
        "@Nightbot": 9990.0,
    })
    (session / "ARTI_VIEWERS.md").write_text(
        "### Active | Pertemuan: 1\n### MemoryOnly | Pertemuan: 7\n",
        encoding="utf-8",
    )
    assert b._initiative_materials()["present_viewers"] == ["@Active", "@WindowEdge"]
    block = b.viewer_presence_block()
    assert "@Active" in block and "@WindowEdge" in block
    for absent in ("Stale", "PreviousSession", "Nightbot", "MemoryOnly"):
        assert absent not in block
    # Profile lookup is for an explicit current trigger author, never a roster.
    assert "[VIEWER SAAT INI" in b.viewer_block_for("@ACTIVE")


def test_same_day_restart_clears_presence_and_count(session):
    b._yt_viewers_seen["@EarlierToday"] = 9999.0
    b._on_viewer_count_increase(6, 7)
    b.reset_viewer_presence_session()
    assert b._yt_viewer_count == -1
    assert b._viewer_join_note == ""
    assert b._initiative_materials()["present_viewers"] == []
    assert "EarlierToday" not in b.viewer_presence_block()
    # A stale timestamp must fail even if old state were accidentally copied in.
    b._yt_viewers_seen["@EarlierToday"] = 9999.0
    assert b._initiative_materials()["present_viewers"] == []


def test_memory_and_persona_names_are_explicitly_historical(session):
    block = b.viewer_presence_block()
    for source in ("memori", "RAG", "persona", "ringkasan", "profil"):
        assert source in block
    assert "dari ingatan, bukan yang lagi nonton" in block
    assert "dulu pernah ada" in block
    out = rag.format_hits_for_prompt([{
        "source_path": "vault/sessions/old_beats.md", "content": "MemoryOnly bilang yoi",
        "score": 0.9,
    }], 1200)
    assert "MemoryOnly" in out
    assert "bukan bukti hadir" in out
    out = curious.build_initiative_prompt(
        {}, memory_bullets=["- [2026-08-28] MemoryOnly bilang yoi"], rng=lambda: 0.0,
    )
    assert "MemoryOnly" in out and "bukan bukti hadir" in out
    b._yt_viewers_seen["@Active"] = 9990.0
    (session / "ARTI_VIEWERS.md").write_text(
        "### Active | Pertemuan: 1 | Teman: MemoryOnly\n", encoding="utf-8",
    )
    assert "profil dari ingatan" in b.viewer_block_for("Active")


def test_scouter_and_vault_topics_do_not_imply_presence(session):
    for kwargs in ({"scouter_summary": "MemoryOnly bilang yoi"},
                   {"vault_topic": "MemoryOnly bilang yoi", "mode": "host_chat"}):
        curious.reset_session()
        out = curious.build_initiative_prompt({}, rng=lambda: 0.0, **kwargs)
        assert "MemoryOnly" in out and "bukan bukti hadir" in out


def test_count_is_anonymous_even_when_known_chat_authors_exist(session):
    b._yt_viewers_seen["@Active"] = 9990.0
    b._on_viewer_count_increase(6, 7)
    note = b._initiative_materials()["viewer_join_note"]
    assert "hanya jumlah" in note and "nama tidak diketahui" in note
    assert "7" in note and "Active" not in note
    assert "TANPA menyebut angka" in note
    b._on_viewer_count_decrease(9, 8)
    assert "nama tidak diketahui" in b.viewer_presence_block()
    assert "8" not in b.viewer_presence_block()  # count is not a roster


def test_presence_rules_survive_prompt_trimming(session, monkeypatch):
    for name in ("_append_host_context", "_append_screen_context",
                 "_append_desktop_audio_context", "_append_minecraft_context",
                 "_append_watch_party_context"):
        monkeypatch.setattr(b, name, lambda text: text)
    trimmed = b.trim_system_prompt_for_llm("x" * 200, {"llm_system_prompt_max_chars": 100})
    assert "dari ingatan, bukan yang lagi nonton" in b._append_live_context(trimmed)


def _action(name, ts, renderer_type="liveChatTextMessageRenderer"):
    renderer = {"id": name, "authorName": {"simpleText": name},
                "message": {"runs": [{"text": "halo arti"}]}}
    if ts is not None:
        renderer["timestampUsec"] = ts if isinstance(ts, str) else str(int(ts * 1000000))
    if renderer_type != "liveChatTextMessageRenderer":
        renderer["purchaseAmountText"] = {"simpleText": "Rp 20.000"}
    if renderer_type == "liveChatPaidStickerRenderer":
        renderer.pop("message")
    return {"addChatItemAction": {"item": {renderer_type: renderer}}}


def _run_chat(monkeypatch, initial=(), live=(), video_id="synthetic", accept=True):
    monkeypatch.setattr(b, "_viewer_session_started_ts", 9950.0)
    page = {"contents": {"liveChatRenderer": {
        "continuation": "synthetic-continuation-token-0001",
        "actions": list(initial),
    }}}
    poll = {"continuationContents": {"liveChatContinuation": {"actions": list(live)}}}
    triggers = []
    fake = SimpleNamespace(
        headers={}, cookies=SimpleNamespace(set=lambda *a, **k: None),
        get=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None, text="var ytInitialData = " + json.dumps(page) + ";"),
        post=lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None, json=lambda: poll),
    )
    monkeypatch.setattr(b.requests, "Session", lambda: fake)
    monkeypatch.setattr(b, "_yt_chat_running", True)
    monkeypatch.setattr(b, "_yt_pernah_chat", set())
    monkeypatch.setattr(b, "_last_yt_trigger_by_viewer", {})
    monkeypatch.setattr(b, "_previous_youtube_video_id", "")
    def queue(text, trigger_type, viewer_name):
        triggers.append((text, trigger_type, viewer_name))
        return accept
    monkeypatch.setattr(b, "queue_voice_trigger", queue)
    monkeypatch.setattr(b.time, "sleep", lambda _: b.stop_youtube_chat())
    b.CONFIG.update(youtube_chat_enabled=True, youtube_video_id=video_id,
                    video_enabled=False, live7_help_enabled=False,
                    voice_queue_enabled=True, donation_alert_base_sec=0)
    b.youtube_chat_worker()
    return triggers


@pytest.mark.parametrize("undated", [None, "invalid"])
def test_initial_timestamp_guard_has_slack_and_logs_each_drop(session, monkeypatch, capsys, undated):
    actions = [_action("EarlierToday", 9949), _action("Undated", undated),
               _action("BeyondSlack", 10121), _action("Ahead", 10120), _action("Active", 9990)]
    triggers = _run_chat(monkeypatch, initial=actions)
    assert [t[2] for t in triggers] == ["Ahead", "Active"]
    assert b._initiative_materials()["present_viewers"] == ["Ahead", "Active"]
    drops = [line for line in capsys.readouterr().out.splitlines() if "Drop timestamp" in line]
    assert len(drops) == 3


def test_rejected_initial_message_stays_deduplicated_on_live_poll(session, monkeypatch):
    old = _action("EarlierToday", 9949)
    assert _run_chat(monkeypatch, initial=[old], live=[old]) == []
    assert b._initiative_materials()["present_viewers"] == []


@pytest.mark.parametrize("renderer_type", [
    "liveChatTextMessageRenderer", "liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer",
])
@pytest.mark.parametrize("skew", [-10, 10, None])
def test_live_poll_ignores_clock_skew_and_deduplicates(session, monkeypatch, capsys, renderer_type, skew):
    action = _action("LiveAuthor", None if skew is None else 10000 + skew, renderer_type)
    triggers = _run_chat(monkeypatch, live=[action, action])
    assert len(triggers) == 1 and triggers[0][2] == "LiveAuthor"
    assert triggers[0][1] == ("yt_chat" if renderer_type == "liveChatTextMessageRenderer" else "donation")
    assert b._initiative_materials()["present_viewers"] == ["LiveAuthor"]
    assert "Drop timestamp" not in capsys.readouterr().out


@pytest.mark.parametrize("renderer_type", ["liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"])
@pytest.mark.parametrize("timestamp", [9949, None, "invalid"])
def test_paid_backlog_is_thanked_once_without_presence(session, monkeypatch, renderer_type, timestamp):
    action = _action("EarlierDonor", timestamp, renderer_type)
    triggers = _run_chat(monkeypatch, initial=[action, action], live=[action])
    assert len(triggers) == 1
    text, kind, viewer = triggers[0]
    assert kind == "donation" and "EarlierDonor" in text
    assert "donasi tertunda" in text and "bukan bukti hadir" in text
    assert viewer is None  # no current-author presence override for historical donations
    assert b._initiative_materials()["present_viewers"] == []
    assert "EarlierDonor" not in b.viewer_presence_block()
    assert not any("[Viewer EarlierDonor" in line for line in b.stream_history)


def test_paid_backlog_is_not_lost_to_the_initial_five_message_tail(session, monkeypatch):
    paid = _action("EarlierDonor", 9949, "liveChatPaidMessageRenderer")
    tail = [_action(f"Live{i}", 9990) for i in range(5)]
    triggers = _run_chat(monkeypatch, initial=[paid, *tail])
    assert len([t for t in triggers if t[1] == "donation"]) == 1
    assert "EarlierDonor" not in b._active_viewer_names()


def test_paid_thank_you_survives_worker_restart(session, monkeypatch):
    paid = _action("PaidId", 10010, "liveChatPaidMessageRenderer")
    assert len(_run_chat(monkeypatch, live=[paid])) == 1
    # New worker, fresh in-memory seen_ids, same video and persisted ID.
    backlog = _action("PaidId", 9949, "liveChatPaidMessageRenderer")
    assert _run_chat(monkeypatch, initial=[backlog]) == []
    ledger = json.loads((session / "data" / "yt_thanked_paid.json").read_text(encoding="utf-8"))
    assert ledger == {"synthetic": ["PaidId"]}


def test_paid_ids_are_scoped_to_video(session, monkeypatch):
    paid = _action("PaidId", 9949, "liveChatPaidMessageRenderer")
    assert len(_run_chat(monkeypatch, initial=[paid], video_id="video-a")) == 1
    assert len(_run_chat(monkeypatch, initial=[paid], video_id="video-b")) == 1
    assert _run_chat(monkeypatch, initial=[paid], video_id="video-a") == []


def test_rejected_thank_you_is_not_persisted(session, monkeypatch):
    paid = _action("PaidId", 9949, "liveChatPaidMessageRenderer")
    _run_chat(monkeypatch, initial=[paid], accept=False)
    assert len(_run_chat(monkeypatch, initial=[paid])) == 1
    assert _run_chat(monkeypatch, initial=[paid]) == []


@pytest.mark.parametrize("contents", ["{broken", '{"video": "wrong-shape"}', None])
def test_paid_ledger_recovers_missing_or_corrupt_file_once(session, monkeypatch, capsys, contents):
    path = session / "data" / "yt_thanked_paid.json"
    if contents is not None:
        path.parent.mkdir()
        path.write_text(contents, encoding="utf-8")
    paid = _action("PaidId", 9949, "liveChatPaidMessageRenderer")
    assert len(_run_chat(monkeypatch, initial=[paid, paid], live=[paid])) == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {"synthetic": ["PaidId"]}
    warnings = [line for line in capsys.readouterr().out.splitlines() if "[YT Paid]" in line]
    assert len(warnings) == 1


def test_paid_ledger_is_bounded_and_atomically_replaced(session, monkeypatch):
    import arti_yt_paid
    path = session / "data" / "yt_thanked_paid.json"
    path.parent.mkdir()
    path.write_text(json.dumps({f"video-{v}": [f"id-{i}" for i in range(501)]
                                for v in range(6)}), encoding="utf-8")
    replace = Mock(wraps=arti_yt_paid.os.replace)
    monkeypatch.setattr(arti_yt_paid.os, "replace", replace)
    ledger = arti_yt_paid.ThankedPaidMessages(path)
    ledger.record("video-5", "new-id")
    assert not ledger.contains("video-0", "id-500")
    assert not ledger.contains("video-5", "id-1")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved) == 5 and all(len(ids) <= 500 for ids in saved.values())
    assert saved["video-5"][-1] == "new-id"
    ledger.record("video-6", "one")
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert list(saved) == ["video-2", "video-3", "video-4", "video-5", "video-6"]
    assert replace.call_count == 2
    source, target = replace.call_args.args
    assert source != target and target == path
    assert list(path.parent.iterdir()) == [path]


def test_delayed_donation_records_only_after_queue_acceptance(session, monkeypatch):
    pending, recorded = [], Mock()
    monkeypatch.setattr(b.threading, "Timer", lambda delay, fn: SimpleNamespace(
        start=lambda: pending.append(fn), daemon=False))
    queue = Mock(return_value=True)
    monkeypatch.setattr(b, "queue_voice_trigger", queue)
    b.CONFIG["donation_alert_base_sec"] = 5
    b.schedule_donation_trigger("[DONASI]", "Donor", "", on_queued=recorded)
    recorded.assert_not_called()
    pending.pop()()
    recorded.assert_called_once_with()


def test_failed_atomic_replace_preserves_previous_ledger(session, monkeypatch, capsys):
    import arti_yt_paid
    path = session / "paid.json"
    path.write_text('{"video":["old"]}', encoding="utf-8")
    ledger = arti_yt_paid.ThankedPaidMessages(path)
    monkeypatch.setattr(arti_yt_paid.os, "replace", Mock(side_effect=PermissionError("synthetic")))
    ledger.record("video", "new")
    ledger.record("video", "newer")
    assert json.loads(path.read_text(encoding="utf-8")) == {"video": ["old"]}
    assert list(session.glob("paid.json*")) == [path]
    assert capsys.readouterr().out.count("[YT Paid]") == 1


def test_seen_ids_evict_oldest_and_keep_the_latest_5000(session, monkeypatch):
    monkeypatch.setattr(b, "print", lambda *a, **k: None, raising=False)
    actions = [_action(f"id-{i}", 9990) for i in range(5001)]
    for action in actions:
        action["addChatItemAction"]["item"]["liveChatTextMessageRenderer"]["message"] = {
            "runs": [{"text": "!ignored-command"}]}
    parser = Mock(wraps=b.teks_pesan_chat)
    monkeypatch.setattr(b, "teks_pesan_chat", parser)
    _run_chat(monkeypatch, live=[*actions, *actions[-2500:], actions[0]])
    # Recent repeats stay deduped; only the evicted oldest ID is parsed again.
    assert parser.call_count == 5002


@pytest.mark.parametrize("platform,kind", [
    ("saweria", "donation"), ("streamlabs", "donation"),
    ("youtube", "membership"), ("streamlabs", "membership"),
])
def test_donation_and_membership_events_record_presence(session, monkeypatch, platform, kind):
    from arti_donations import DonationEvent
    (session / "ARTI_VIEWERS.md").write_text("### EventAuthor | Pertemuan: 2\n", encoding="utf-8")
    monkeypatch.setattr(b, "schedule_donation_trigger", Mock())
    b._on_donation(DonationEvent(platform, "EventAuthor", 20000, kind=kind))
    assert b._initiative_materials()["present_viewers"] == ["EventAuthor"]
    assert "EventAuthor" in b.viewer_block_for("EventAuthor")


def test_minecraft_chat_records_presence(session, monkeypatch):
    monkeypatch.setattr(b, "queue_voice_trigger", Mock())
    b._queue_minecraft_chat_pemain("halo arti", "GameAuthor")
    assert b._initiative_materials()["present_viewers"] == ["GameAuthor"]
    assert b.queue_voice_trigger.call_args.kwargs["viewer_name"] == "GameAuthor"


@pytest.mark.parametrize("kind,text", [
    ("donation", "[DONASI Saweria dari TurnAuthor]: halo"),
    ("donation", "[MEMBERSHIP YouTube dari TurnAuthor]: halo"),
    ("mc_chat_pemain", "TurnAuthor ngetik di Minecraft"),
    ("yt_chat", "[Pesan Live Chat dari Viewer TurnAuthor]: halo"),
])
def test_current_trigger_author_is_present_only_for_that_turn(session, monkeypatch, kind, text):
    from tests.test_bridge_batch9_robustness import fake_turn
    real_profile_lookup = b.viewer_block_for
    fake_turn(monkeypatch)
    monkeypatch.setattr(b, "viewer_block_for", real_profile_lookup)
    (session / "ARTI_VIEWERS.md").write_text("### TurnAuthor | Pertemuan: 2\n", encoding="utf-8")
    monkeypatch.setattr(b, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(b, "_should_route_to_cursor", lambda _: True)
    reply = AsyncMock(return_value=("Makasih!", [], "codex:synthetic"))
    monkeypatch.setattr(b, "_cursor_reply_with_fallback", reply)
    asyncio.run(b._handle_voice_trigger(b.VoiceTrigger(text, kind, "TurnAuthor"), [], "system"))
    system = reply.await_args.args[0]
    assert "TurnAuthor" in system and "hadir untuk giliran ini" in system
    prepared_system = b.arti_voice_pipeline.prepare_turn_context.await_args.args[2]
    assert "[VIEWER SAAT INI" in prepared_system and "TurnAuthor" in prepared_system
    assert b._initiative_materials()["present_viewers"] == []
    assert "TurnAuthor" not in b.viewer_presence_block()


def test_owner_is_exempt_from_historical_viewer_absence(session):
    assert b._initiative_materials()["present_viewers"] == []
    assert "kecuali streamer/Bohan (hadir lewat mic)" in b.viewer_presence_block()


@pytest.mark.parametrize("kind,expected", [
    ("mic", "Streamer (Bohan)"), ("mic_pasif", "Streamer (Bohan)"),
    ("ptt", "Streamer (Bohan)"), ("wake_word", "Streamer (Bohan)"),
    ("curious", "PROAKTIF"), ("game", "kejadian game"),
])
def test_current_turn_source_is_in_final_model_prompt(session, monkeypatch, kind, expected):
    from tests.test_bridge_batch9_robustness import fake_turn
    import time
    fake_turn(monkeypatch)
    monkeypatch.setattr(b, "hotkey_active", False)
    monkeypatch.setattr(b, "post_process_response", lambda text, *args: text)
    monkeypatch.setattr(b, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(b, "_should_route_to_cursor", lambda _: True)
    # An old viewer name may remain in context; it must not become this turn's author.
    prepared = b.arti_voice_pipeline.prepare_turn_context
    prepared.return_value.llm_system = "system; EarlierViewer pernah chat"
    monkeypatch.setattr(b.arti_voice_pipeline, "prepare_curious_turn_context", prepared)
    reply = AsyncMock(return_value=("Oke!", [], "codex:synthetic"))
    monkeypatch.setattr(b, "_cursor_reply_with_fallback", reply)
    asyncio.run(b._handle_voice_trigger(
        b.VoiceTrigger("screen observation", kind, queued_at=time.time() + 1), [], "system"))
    system = reply.await_args.args[0]
    assert expected in system
    assert "bukan pengirim giliran ini" in system
    assert "EarlierViewer hadir untuk giliran ini" not in system
