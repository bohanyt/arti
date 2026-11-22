"""ADR 228 part A: the voice reads the game record through one digest file. Synthetic rows only."""
import asyncio
import hashlib
import json
import statistics
import time
from unittest.mock import AsyncMock, Mock

import pytest

import arti_mind as M
from arti_runtime import experience_digest as D
from arti_runtime import stream_notes as N
from arti_runtime.seen_receipts import SeenReceipts

SCOPE = "synthetic-save:7"
QUESTION = "udah kenal siapa aja di Pelican Town?"
YESTERDAY = "kemarin kamu ngapain?"


def scene_line(base, scope, speaker, text, *, day, place, source="host_screen"):
    """The shape part C writes: a captured text owner plus a dialogue_read receipt pointing at it."""
    event_id = "story_line:%s:%s" % (speaker, hashlib.sha256(text.encode()).hexdigest()[:16])
    M.record_discoveries(base, "stardew", scope=scope, day=day, day_label=M.plan_dates.label("stardew", day),
                         discoveries=[dict(id=event_id, kind="captured_text", subject=speaker, text=text,
                                           source=source, event_place=place)])
    ref = hashlib.sha256((scope + "\0" + event_id).encode()).hexdigest()
    SeenReceipts(base, scope).note("dialogue_read", speaker + ":" + hashlib.sha256(text.encode()).hexdigest()[:16],
                                   game_day=day, clock=None, place=place, source=source, text_ref=ref)


def synthetic_record(base, scope=SCOPE):
    scene_line(base, scope, "Lewis", "Selamat datang di Pelican Town! Ini rumah barumu.", day=1, place="Farm")
    scene_line(base, scope, "Robin", "Aku Robin, tukang kayu kota ini.", day=1, place="Farm")
    store = SeenReceipts(base, scope)
    # Identity only, from save data: friendship records (even one carrying program text) and save flags.
    M.record_discoveries(base, "stardew", scope=scope, day=3, day_label="Y1 spring 3", discoveries=[dict(
        id="npc_met:Kent", kind="npc_met", subject="Kent", text="Kent: met (personal friendship record)",
        source="game_state")])
    kent = hashlib.sha256((scope + "\0npc_met:Kent").encode()).hexdigest()
    store.note("villager_met", "Kent", game_day=3, clock=None, place=None, source="game_state", text_ref=kent)
    store.note("villager_met", "Penny", game_day=3, clock=None, place=None, source="game_state")
    store.note("place_entered", "Beach", game_day=4, clock=900, place="Beach", source="native_visit")
    store.note("place_entered", "Desert", game_day=4, clock=None, place=None,
               source="native_save_flags:places_visited")
    M.save_journey_row(base, "stardew", dict(v=M.MIND_VERSION, schema=M.JOURNEY_SCHEMA, id="Y1 spring 4", day=4,
        date="Y1 spring 4", reflection=dict(today="Aku mancing di pantai sampai sore, dapat dua ikan.")))
    M.save_goals(base, "stardew", [dict(v=1, id="long-1", horizon="long", status="open", set_on_day=2,
                                        authored_on_day=2, text="Aku ingin kenal semua warga")])
    row = dict(session="synthetic", speaker="owner", ingress="ptt", text="Coba siram tanaman dulu sebelum pergi ya.",
               wall_ts=1794913200.0, wib="2026-11-17T19:00:00+07:00")
    assert N.record_line(base, scope, row, dict(day="Y1 spring 4", clock=1900, place="Farm")) == "kept"


def digest_text(base, **kw):
    digest = D.build(base, SCOPE, day=5, place="Farm", actions={4: ["tangkap_ikan", "tangkap_ikan"]}, **kw)
    D.write(base, digest)
    return digest


def test_digest_people_are_scene_receipts_only_and_identity_names_are_a_count(tmp_path):
    synthetic_record(tmp_path)
    digest = digest_text(tmp_path)
    names = [p["name"] for p in digest["people"]]
    assert names == ["Lewis", "Robin"]
    assert digest["people"][0]["line"].startswith("Selamat datang") and digest["people"][0]["source"] == "host_screen"
    assert digest["unnamed_names"] == 2  # Kent and Penny: friendship data, no scene
    text = digest["text"]
    assert "Kent" not in text and "Penny" not in text and "nama tercatat tanpa adegan: 2" in text
    assert [p["place"] for p in digest["places"]] == ["Beach"]  # the save flag Desert is not a visit
    assert "Desert" not in text
    assert digest["days"]["kemarin"]["summary"].startswith("Aku mancing di pantai")
    assert digest["said"][0]["by"] == "owner" and digest["said"][0]["date"] == "2026-11-17"
    assert digest["wants"] == [dict(horizon="long", text="Aku ingin kenal semua warga")]
    assert len(text.encode("utf-8")) <= D.BLOCK_MAX_BYTES and "\n" not in text
    # Atomic, whole-file JSON that the bridge reader accepts.
    on_disk = json.loads(D.path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["schema"] == D.SCHEMA and on_disk["text"] == text


def test_reader_drops_missing_malformed_stale_restricted_and_oversized(tmp_path):
    reader = D.Reader()
    file = D.path(tmp_path)
    assert reader.block(file) == ""
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("{not json", encoding="utf-8")
    assert reader.block(file) == ""
    synthetic_record(tmp_path)
    digest = digest_text(tmp_path)
    block = reader.block(file)
    assert block == "\n" + digest["text"] + "\n"
    assert reader.block(file, not_before=digest["written_ts"] + 1) == ""  # written before this privacy scope
    assert reader.block(file, restricted=True) == ""
    file.write_text(json.dumps(dict(digest, text=D.TAG + " x" * 2000)), encoding="utf-8")
    assert reader.block(file) == ""
    file.write_text(json.dumps(dict(digest, schema="other")), encoding="utf-8")
    assert reader.block(file) == ""


def test_another_save_scope_replaces_the_file_before_any_page(tmp_path):
    from types import SimpleNamespace as NS
    synthetic_record(tmp_path)
    digest_text(tmp_path)
    journal = []
    campaign = NS(date=(1, "spring", 5), days=[], live=NS(history=[]))
    runtime = NS(_config={D.SETTING: True}, _live7_mind_base=lambda: tmp_path, _journal=journal.append,
                 _live7_discovery_scope="other-save:7")
    D.live7_scope(runtime, NS(campaign=campaign), "other-save:7")
    now = json.loads(D.path(tmp_path).read_text(encoding="utf-8"))
    assert now["scope_hash"] == hashlib.sha256(b"other-save:7").hexdigest()
    assert "Lewis" not in now["text"] and not journal
    runtime._config = {}
    D.path(tmp_path).unlink()
    D.live7_write(runtime, NS(campaign=campaign))  # off: the game lane writes nothing
    assert not D.path(tmp_path).exists()


def turn(monkeypatch, tmp_path, speech, *, enabled):
    """Run the real _handle_voice_trigger prompt assembly with fakes for devices and providers."""
    import arti_bridge as B
    calls = []
    monkeypatch.setitem(B.CONFIG, "bridge_test_mode", False)
    monkeypatch.setitem(B.CONFIG, "arti_mind_dir", str(tmp_path))
    monkeypatch.setitem(B.CONFIG, "voice_game_memory_enabled", enabled)
    monkeypatch.setattr(B, "_prepare_turn_start", AsyncMock())
    monkeypatch.setattr(B, "_pending_turn_id", None)
    monkeypatch.setattr(B, "PipelineTimer", Mock(return_value=Mock(stages_ms=lambda: {})))
    monkeypatch.setattr(B, "refresh_vision_for_turn", lambda *_: None)
    for name in ("mood_block_now", "build_today_block", "discord_stories_block_now", "stardew_role_block_now",
                 "mind_intent_block_now", "_live7_status_block_now"):
        monkeypatch.setattr(B, name, lambda: "")
    monkeypatch.setattr(B, "viewer_block_for", lambda _: "")
    monkeypatch.setattr(B, "get_categorized_history", lambda: "")
    monkeypatch.setattr(B, "trim_system_prompt_for_llm", lambda text, _cfg: text)
    monkeypatch.setattr(B, "_append_live_context", lambda text: text)
    monkeypatch.setattr(B, "_try_stardew_owner_generation", lambda _: (None, False))
    monkeypatch.setattr(B, "_should_route_to_cursor", lambda _: True)
    monkeypatch.setattr(B.arti_expression_runtime, "apply_turn_end", AsyncMock())
    monkeypatch.setattr(B, "vts", None)
    monkeypatch.setattr(B, "add_to_history", lambda *a, **k: None)
    monkeypatch.setattr(B.screen_privacy, "current", lambda _: True)

    async def reply(system, prompt, user_speech, **_kw):
        calls.append(dict(system=system, prompt=prompt, speech=user_speech))
        return "[diam]", [], "codex:fake"

    monkeypatch.setattr(B, "_cursor_reply_with_fallback", reply)
    asyncio.run(B._handle_voice_trigger(B.VoiceTrigger(speech, "ptt", None, time.time()), [], "system"))
    assert len(calls) == 1
    return calls[0]


@pytest.mark.parametrize("speech", [QUESTION, YESTERDAY])
def test_real_turn_prompt_carries_the_record_and_is_byte_identical_when_off(tmp_path, monkeypatch, speech):
    synthetic_record(tmp_path)
    digest_text(tmp_path)
    off = turn(monkeypatch, tmp_path, speech, enabled=False)
    on = turn(monkeypatch, tmp_path, speech, enabled=True)
    assert "[INGATAN GAME]" not in off["system"] and speech in on["prompt"]
    text = json.loads(D.path(tmp_path).read_text(encoding="utf-8"))["text"]
    assert "\n" + text + "\n" in on["system"]
    assert on["system"].replace("\n" + text + "\n", "", 1) == off["system"]  # the only difference
    block = next(line for line in on["system"].splitlines() if line.startswith("[INGATAN GAME]"))
    assert len(block.encode("utf-8")) <= 1500
    if speech == QUESTION:
        assert "Lewis" in block and "Robin" in block and "Selamat datang di Pelican Town" in block
        assert "Kent" not in on["system"] and "Penny" not in on["system"]
    else:
        assert "kemarin (Y1 spring 4)" in block and "Aku mancing di pantai sampai sore" in block


def test_off_prompt_matches_main_assembly_without_reading_the_file(tmp_path, monkeypatch):
    import arti_bridge as B
    synthetic_record(tmp_path)
    digest_text(tmp_path)
    monkeypatch.setattr(D, "voice_block", Mock(side_effect=AssertionError("read while off")))
    monkeypatch.setattr(D, "bridge_block", Mock(side_effect=AssertionError("read while off")))
    off = turn(monkeypatch, tmp_path, QUESTION, enabled=False)
    assert "[INGATAN GAME]" not in off["system"]
    monkeypatch.delitem(B.CONFIG, "voice_game_memory_enabled")
    assert "[INGATAN GAME]" not in turn(monkeypatch, tmp_path, QUESTION, enabled=False)["system"]


def test_every_digest_reference_in_the_reactor_files_sits_under_the_setting():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("arti_bridge.py", "arti_stardew_live7.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8-sig"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        seams = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id == "experience_digest"
                 or isinstance(n, ast.alias) and n.name == "experience_digest"]
        assert seams, name
        for seam in seams:
            node = seam
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.If) and "voice_game_memory_enabled" in ast.unparse(node.test):
                    break
            else:
                pytest.fail("experience digest outside its setting in " + name)


def test_the_block_survives_the_status_preserving_trim(tmp_path, monkeypatch):
    import arti_bridge as B
    synthetic_record(tmp_path)
    digest = digest_text(tmp_path)
    monkeypatch.setitem(B.CONFIG, "arti_mind_dir", str(tmp_path))
    block = D.bridge_block(B)
    assert block == "\n" + digest["text"] + "\n"
    monkeypatch.setitem(B.CONFIG, "voice_game_memory_enabled", True)
    trimmed = B.trim_system_prompt_for_llm("x" * 9000 + block, {"llm_system_prompt_max_chars": 3000})
    assert digest["text"] in trimmed
    monkeypatch.setitem(B.CONFIG, "voice_game_memory_enabled", False)
    assert B._split_stardew_status("x\n" + digest["text"]) == ("x\n" + digest["text"], "")  # off: main's pattern


def test_privacy_boundary_drops_the_block_until_the_game_lane_writes_again(tmp_path, monkeypatch):
    import arti_bridge as B
    from arti_screen_privacy import ScreenPrivacy
    synthetic_record(tmp_path)
    digest_text(tmp_path, now=time.time() - 5)
    privacy = ScreenPrivacy()
    monkeypatch.setattr(B, "screen_privacy", privacy)
    monkeypatch.setitem(B.CONFIG, "arti_mind_dir", str(tmp_path))
    assert D.bridge_block(B)
    privacy.reset_session()  # a new bridge session is a new privacy scope
    assert D.bridge_block(B) == ""
    digest_text(tmp_path)
    assert D.bridge_block(B)
    assert privacy.apply_streamer_text("jangan lihat layar")
    assert D.bridge_block(B) == ""


def test_turn_path_cost_with_and_without_the_block(tmp_path, monkeypatch):
    """The prompt assembly line as the bridge runs it: off adds nothing, on adds one stat and a cached read."""
    import arti_bridge as B
    synthetic_record(tmp_path)
    digest_text(tmp_path)
    monkeypatch.setitem(B.CONFIG, "arti_mind_dir", str(tmp_path))
    parts = ["system", "mood", "today", "viewer", "discord", "role", "status", "alert", "niatku"]

    def build(flag, n=1000):
        monkeypatch.setitem(B.CONFIG, "voice_game_memory_enabled", flag)
        out = []
        for _ in range(n):
            start = time.perf_counter()
            prompt = "".join(parts)
            if B.CONFIG.get("voice_game_memory_enabled"):
                prompt += D.bridge_block(B)
            out.append((time.perf_counter() - start) * 1000.0)
        out.sort()
        return statistics.median(out), out[int(len(out) * 0.95) - 1], prompt

    build(True, 50)
    off_median, off_p95, off_prompt = build(False)
    on_median, on_p95, on_prompt = build(True)
    print("turn-path ms off median=%.4f p95=%.4f on median=%.4f p95=%.4f block_bytes=%d" % (
        off_median, off_p95, on_median, on_p95, len(on_prompt.encode()) - len(off_prompt.encode()) - 2))
    assert on_median - off_median < 1.0 and "[INGATAN GAME]" in on_prompt and off_prompt == "".join(parts)
