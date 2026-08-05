"""Fitur E (v0.7): Arti mengerti video — helper murni + wiring.

Terukur spike 2026-08-02: Gemini URL YouTube 2,6 dtk (0 bandwidth lokal),
metadata 2,5 dtk, transkrip 0,6 dtk. Semua test tanpa jaringan.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_donations as dn
import arti_video_watcher as vw


# --- helper murni ---------------------------------------------------------------


def test_extract_youtube_ids_all_forms():
    text = ("cek https://www.youtube.com/watch?v=dQw4w9WgXcQ dan "
            "https://youtu.be/jNQXAC9IVRw juga youtube.com/shorts/abc-DEF_123 "
            "dan duplikat https://youtu.be/dQw4w9WgXcQ")
    assert vw.extract_youtube_ids(text) == [
        "dQw4w9WgXcQ", "jNQXAC9IVRw", "abc-DEF_123"
    ]
    assert vw.extract_youtube_ids("halo tanpa link") == []


def test_frame_times_matches_bohan_example():
    """'kalo videonya 59 detik yaaa ambil frame ke detik 6 12 etc'."""
    t = vw.frame_times(59)
    assert t[:3] == [6, 12, 18]
    assert all(0 < x < 59 for x in t)
    assert vw.frame_times(10) == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_parse_json3_and_compress():
    data = {"events": [
        {"tStartMs": 0, "segs": [{"utf8": "halo "}, {"utf8": "dunia"}]},
        {"tStartMs": 65000, "segs": [{"utf8": "menit satu"}]},
        {"tStartMs": 1000},  # tanpa segs -> dilewati
    ]}
    segs = vw.parse_json3_captions(data)
    assert segs == [(0.0, "halo dunia"), (65.0, "menit satu")]
    txt = vw.compress_transcript(segs)
    assert "[00:00] halo dunia" in txt and "[01:05] menit satu" in txt
    assert vw.parse_json3_captions({}) == []


def test_build_timeline_doc_format():
    doc = vw.build_timeline_doc(
        "abc123def45", "Judul Keren", "ChannelX", 125,
        [(0, "Pembukaan seru"), (65, "Klimaks di menit satu")],
        submitted_by="@budi", source="saweria",
    )
    assert "## [00:00] Pembukaan seru" in doc
    assert "## [01:05] Klimaks di menit satu" in doc
    assert "Dikirim oleh: @budi (saweria)" in doc
    assert "Durasi: 02:05" in doc


def test_submit_guards():
    cfg = {"video_queue_max": 2, "video_rate_limit_sec": 300}
    job = vw.VideoJob(video_id="x" * 11, source="chat", viewer="@a")
    ok, _ = vw.check_submit_allowed(job, now=1000.0, last_by_viewer={},
                                    queue_depth=0, config=cfg)
    assert ok
    ok, why = vw.check_submit_allowed(job, now=1000.0, last_by_viewer={},
                                      queue_depth=2, config=cfg)
    assert not ok and "penuh" in why
    ok, why = vw.check_submit_allowed(job, now=1100.0, last_by_viewer={"@a": 1000.0},
                                      queue_depth=0, config=cfg)
    assert not ok and "rate limit" in why
    # saweria = bayar -> bebas rate limit viewer
    paid = vw.VideoJob(video_id="y" * 11, source="saweria", viewer="@a",
                       donation_label="Rp 20.000")
    ok, _ = vw.check_submit_allowed(paid, now=1100.0, last_by_viewer={"@a": 1000.0},
                                    queue_depth=0, config=cfg)
    assert ok


def test_reaction_tone_per_source():
    saweria = vw.VideoJob(video_id="a" * 11, source="saweria", viewer="@budi",
                          donation_label="Rp 50.000", message="tonton ya")
    t1 = vw.format_reaction_trigger(saweria, "isi video", "Judul A")
    assert "Rp 50.000" in t1 and "terima kasih" in t1.lower()
    assert "tonton ya" in t1

    points = vw.VideoJob(video_id="b" * 11, source="streamlabs", viewer="@eko")
    t2 = vw.format_reaction_trigger(points, "isi video", "Judul B")
    assert "tanpa upacara" in t2
    assert "Rp" not in t2


def test_clip_seconds_and_url():
    j = vw.VideoJob(video_id="jNQXAC9IVRw", clip_start=10, clip_end=40)
    assert j.clip_seconds == 30
    assert j.url.endswith("watch?v=jNQXAC9IVRw")


# --- media share Streamlabs (parser defensif + perekam) --------------------------


def test_streamlabs_mediashare_defensive_parse():
    payload = {"type": "mediaShare", "message": [{
        "name": "eko", "media": {"url": "https://youtu.be/dQw4w9WgXcQ"},
        "media_title": "lagu legend",
    }]}
    evs = dn.parse_streamlabs_mediashare(payload)
    assert len(evs) == 1
    ev = evs[0]
    assert ev.kind == "media_points"
    assert ev.media_url == "https://youtu.be/dQw4w9WgXcQ"
    assert ev.name == "eko"
    # type donation TIDAK ditangkap parser mediashare
    assert dn.parse_streamlabs_mediashare({"type": "donation", "message": []}) == []


def test_streamlabs_unknown_recorder(tmp_path, monkeypatch):
    sample = tmp_path / "sample.jsonl"
    monkeypatch.setattr(dn, "_SL_SAMPLE_PATH", str(sample))
    dn.record_unknown_streamlabs({"type": "alienEvent", "foo": 1})
    dn.record_unknown_streamlabs({"type": "donation"})  # dikenal -> tidak direkam
    lines = sample.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "alienEvent" in lines[0]


def test_find_youtube_ref_shapes():
    assert dn._find_youtube_ref({"media": {"id": "dQw4w9WgXcQ"}}) == "dQw4w9WgXcQ"
    assert dn._find_youtube_ref(
        {"x": [{"link": "youtube.com/watch?v=jNQXAC9IVRw"}]}) == "jNQXAC9IVRw"
    assert dn._find_youtube_ref({"pesan": "halo"}) == ""


# --- wiring bridge ----------------------------------------------------------------


def test_bridge_video_wiring():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"video_enabled": False,' in src, "kill switch wajib default OFF"
    for key in ("video_max_duration_sec", "video_qa_window_sec",
                "video_rate_limit_sec", "video_queue_max", "mediashare_hold_sec"):
        assert f'"{key}"' in src
    # hold playback: potong TTS + tahan konsumsi trigger + tahan inisiatif
    assert "def hold_media_playback" in src
    assert "if time.time() < _media_playback_until:" in src
    assert "time.time() >= _media_playback_until" in src
    # drain-newest tidak boleh menimpa donation/video
    assert '("donation", "video")' in src
    # komando console + deteksi link + navigationEndpoint
    assert '"video skip"' in src or "'video skip'" in src.lower() or "video skip" in src
    assert "navigationEndpoint" in src
    assert 'source="chat"' in src


def test_voice_queue_video_priority():
    import arti_voice_queue as avq

    assert avq._TRIGGER_PRIORITY["donation"] < avq._TRIGGER_PRIORITY["yt_chat"]
    assert avq._TRIGGER_PRIORITY["yt_chat"] < avq._TRIGGER_PRIORITY["video"]
    assert avq._TRIGGER_PRIORITY["video"] < avq._TRIGGER_PRIORITY["mic"]


def test_pipeline_video_branch():
    import asyncio

    import arti_voice_pipeline as vp

    ctx = asyncio.run(
        vp.prepare_turn_context(
            "[VIDEO SELESAI DITONTON — dikirim @eko] Judul: X.\nisi\nKomentari...",
            [],
            "system base",
            {"vault_rag_live_enabled": False},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[history]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "nonton bareng" in ctx.target_instruction
    assert "2-3 kalimat penuh" not in ctx.prompt_content, "bukan template streamer"


# --- crosscheck pra-live (3 temuan) ---------------------------------------------


def test_reaction_message_never_contains_url():
    job = vw.VideoJob(video_id="dQw4w9WgXcQ", source="chat", viewer="@eko",
                      message="tonton dong https://youtu.be/dQw4w9WgXcQ keren")
    t = vw.format_reaction_trigger(job, "isi", "Judul")
    assert "youtu" not in t.lower(), "TTS jangan pernah mengeja URL"
    assert "tonton dong" in t and "keren" in t


def test_video_trigger_never_dropped_when_busy(monkeypatch):
    import queue as _q

    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b.session_transcript, "log_trigger", lambda *a, **k: None)
    monkeypatch.setattr(b, "_brain_busy", True)
    while True:
        try:
            b.voice_trigger_queue.get_nowait()
        except _q.Empty:
            break
    b.queue_voice_trigger("[VIDEO SELESAI DITONTON...]", trigger_type="video",
                          viewer_name="@eko")
    assert b.voice_trigger_queue.get_nowait().trigger_type == "video", (
        "reaksi video tidak boleh di-drop saat sibuk — penonton nunggu playback"
    )


def test_saweria_media_hold_includes_alert_base():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    i = src.index("def _submit_media_job")
    seg = src[i:i + 2200]
    assert "donation_alert_base_sec" in seg, (
        "hold wajib mencakup durasi alert (main duluan sebelum klip)"
    )


# --- rantai Saweria media share DIEKSEKUSI beneran (crosscheck ronde 3) ----------


class _FakeWatcher:
    def __init__(self, accept=True):
        self.accept = accept
        self.jobs = []
        self.runtime_enabled = True

    def submit(self, job):
        self.jobs.append(job)
        return self.accept


def _media_ev(**over):
    kw = dict(platform="saweria", name="Budi", amount=20000.0,
              message="tonton dong", media_url="https://youtu.be/dQw4w9WgXcQ",
              media_start=10, media_end=40)
    kw.update(over)
    return dn.DonationEvent(**kw)


def _run_flow(monkeypatch, *, watcher, video_enabled=True, ev=None):
    import hermes_vtuber_bridge as b

    calls = {"thanks": [], "hold": []}
    monkeypatch.setattr(b.session_transcript, "append_from_history", lambda *a, **k: None)
    monkeypatch.setattr(b, "schedule_donation_trigger",
                        lambda text, viewer, msg: calls["thanks"].append(text))
    monkeypatch.setattr(b, "hold_media_playback",
                        lambda s: calls["hold"].append(s))
    monkeypatch.setattr(b, "_video_watcher", watcher)
    monkeypatch.setitem(b.CONFIG, "video_enabled", video_enabled)
    b._on_donation(ev or _media_ev())
    return calls


def test_media_flow_accepted_holds_and_merges_thanks(monkeypatch):
    w = _FakeWatcher(accept=True)
    calls = _run_flow(monkeypatch, watcher=w)
    assert len(w.jobs) == 1 and w.jobs[0].donation_label == "Rp 20.000"
    assert calls["hold"], "playback hold wajib jalan saat job diterima"
    assert calls["hold"][0] >= 30 + 5, "hold = klip (30s) + alert base"
    assert calls["thanks"] == [], "terima kasih DIGABUNG ke reaksi video"


def test_media_flow_rejected_falls_back_to_thanks_without_hold(monkeypatch):
    """Bug crosscheck ronde 3: dulu job ditolak = donatur BISU + Arti bengong
    60 dtk (hold jalan duluan sebelum guard)."""
    w = _FakeWatcher(accept=False)
    calls = _run_flow(monkeypatch, watcher=w)
    assert calls["thanks"], "donatur bayar WAJIB tetap dapat terima kasih"
    assert calls["hold"] == [], "JANGAN hold untuk job yang ditolak"


def test_media_flow_video_off_falls_back_to_thanks(monkeypatch):
    calls = _run_flow(monkeypatch, watcher=None, video_enabled=False)
    assert calls["thanks"] and calls["hold"] == []


def test_media_points_rejected_stays_silent(monkeypatch):
    """Streamlabs points gagal submit = diam (bukan donasi berbayar)."""
    w = _FakeWatcher(accept=False)
    calls = _run_flow(monkeypatch, watcher=w,
                      ev=_media_ev(platform="streamlabs", amount=0.0,
                                   kind="media_points"))
    assert calls["thanks"] == [] and calls["hold"] == []


# --- fix audit adversarial (subagent, 2026-08-02 sore) ---------------------------


def test_hold_never_shortens(monkeypatch):
    """Dua media share beruntun: share #2 pendek tidak boleh MEMENDEKKAN hold #1."""
    import time as _t

    import hermes_vtuber_bridge as b

    monkeypatch.setattr(b, "tts_is_playing", False)
    monkeypatch.setattr(b, "_media_playback_until", _t.time() + 68.0)
    b.hold_media_playback(15.0)
    assert b._media_playback_until >= _t.time() + 60.0, "hold memendek — bug audit A"
    monkeypatch.setattr(b, "_media_playback_until", 0.0)


def test_find_youtube_ref_rejects_random_11char_strings():
    """'medialover1' (nama viewer) BUKAN ID video — bug job hantu + hold 68 dtk."""
    assert dn._find_youtube_ref({"name": "medialover1", "message": "gass bang"}) == ""
    assert dn._find_youtube_ref({"media": {"id": "dQw4w9WgXcQ"}}) == "dQw4w9WgXcQ"
    assert dn._find_youtube_ref({"apapun": "https://youtu.be/dQw4w9WgXcQ"}) == "dQw4w9WgXcQ"


def test_buffer_ttl_never_purges_donation_video():
    import time as _t

    import arti_voice_queue as avq

    buf = avq.VoiceTriggerQueue(ttl_sec=60.0)
    old = avq.QueuedVoiceTrigger(text="d", trigger_type="donation", viewer_name="@a")
    old.enqueued_at = _t.time() - 70.0
    buf.enqueue(avq.QueuedVoiceTrigger(text="v", trigger_type="video", viewer_name="@b"))
    buf._items.insert(0, old)
    got = buf.dequeue()
    assert got is not None and got.trigger_type == "donation", (
        "donation kadaluarsa TTL = drop diam-diam — kontradiksi kebijakan"
    )


def test_video_trigger_skips_web_lookup(monkeypatch):
    import asyncio

    import arti_voice_pipeline as vp
    import arti_web_lookup as wl

    def _boom(*a, **k):
        raise AssertionError("lookup TIDAK boleh jalan untuk trigger video")

    monkeypatch.setattr(wl, "lookup_block", _boom)
    ctx = asyncio.run(
        vp.prepare_turn_context(
            "[VIDEO SELESAI DITONTON — dikirim @x] Judul: Berita Harga Bitcoin Meledak.",
            [], "system base",
            {"vault_rag_live_enabled": False, "web_lookup_enabled": True},
            trim_system_prompt=lambda s, c: s,
            append_watch_party_context=lambda s: s,
            get_categorized_history=lambda: "[h]",
            extract_trigger_message=lambda s: s,
        )
    )
    assert "[INFO INTERNET" not in ctx.llm_system


def test_post_process_strips_urls():
    import hermes_vtuber_bridge as b

    out = b.post_process_response(
        "Cek deh https://youtu.be/dQw4w9WgXcQ seru banget", "hai")
    assert "http" not in out and "youtu" not in out
    assert "seru banget" in out


def test_speak_path_waits_for_playback_and_misc_wiring():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert "klip masih main" in src, (
        "turn yang sudah diproses harus menahan TTS sampai playback usai"
    )
    i = src.index("def hold_media_playback")
    assert "max(" in src[i:i + 800], "hold wajib max() — tidak boleh memendek"
    assert "voice_trigger_queue.empty()" in src, (
        "inisiatif harus menunggu antrean trigger kosong"
    )
    # video off membebaskan hold
    j = src.index('if low == "video off":')
    assert "_media_playback_until = 0.0" in src[j:j + 400]


def test_worker_threads_survive_is_alive_after_stop():
    w = vw.VideoWatcher({}, {"queue_reaction": lambda t, v: None})
    w.start()
    w.stop()
    import time as _t
    _t.sleep(1.2)
    assert w.is_alive() is False  # dulu TypeError: 'Event' object is not callable


def test_qa_window_turn_prep_executes_without_nameerror(monkeypatch):
    """Regresi live 2026-08-02 sore: jendela Q&A menyalakan watch_party_enabled,
    jalur _append_watch_party_context memanggil arti_desktop_audio yang saat itu
    TIDAK di-import bridge -> NameError di SETIAP turn selama 300 dtk (180x crash,
    komentar video buat donatur ikut hangus). Test ini mengeksekusi jalurnya."""
    import hermes_vtuber_bridge as b

    monkeypatch.setitem(b.CONFIG, "watch_party_enabled", True)
    monkeypatch.setitem(b.CONFIG, "watch_party_event_id", "video-testvid1234")
    out = b._append_watch_party_context("SYSTEM-BASE")
    assert out.startswith("SYSTEM-BASE")
    assert "watch-party / video-testvid1234" in out


# --- media share Streamlabs: bentuk PASTI (sampel asli live 2026-08-02) ----------
# type "mediaShareEvent"; "newMedia"=antre, "play"=mulai diputar (momen hold),
# media.media = ID YouTube, duration MILIDETIK, "play" tanpa media = kontrol.


def _sl_play_payload(req_id=5058238, event="play", media_present=True):
    media = {
        "id": req_id, "action_by": "@bohanyt", "media_type": "youtube",
        "media": "8ifqmFiFWBw", "media_title": "GEt oUt!!!",
        "duration": 9000, "start_time": 0, "request_type": "MediaShare",
        "requester_name": "@bohanyt", "requester_platform": "Youtube",
    } if media_present else None
    return {"type": "mediaShareEvent", "event_id": "evt_x",
            "message": {"event": event, "media": media}}


def test_streamlabs_mediasharevent_play_locked_shape():
    dn._sl_media_req_seen.clear()
    evs = dn.parse_streamlabs_mediashare(_sl_play_payload())
    assert len(evs) == 1
    ev = evs[0]
    assert ev.kind == "media_points" and ev.platform == "streamlabs"
    assert ev.media_url == "https://youtu.be/8ifqmFiFWBw"
    assert ev.name == "@bohanyt"
    assert ev.media_start == 0 and ev.media_end == 9, "duration 9000 = MILIDETIK"
    assert ev.message == "GEt oUt!!!"


def test_streamlabs_mediasharevent_newmedia_is_queue_only(capsys):
    dn._sl_media_req_seen.clear()
    assert dn.parse_streamlabs_mediashare(_sl_play_payload(event="newMedia")) == []
    assert "nunggu play" in capsys.readouterr().out


def test_streamlabs_mediasharevent_player_control_skipped():
    dn._sl_media_req_seen.clear()
    payload = {"type": "mediaShareEvent",
               "message": {"event": "play", "media": None, "modMoveToNext": True}}
    assert dn.parse_streamlabs_mediashare(payload) == []


def test_streamlabs_mediasharevent_resume_play_deduped():
    dn._sl_media_req_seen.clear()
    assert len(dn.parse_streamlabs_mediashare(_sl_play_payload())) == 1
    assert dn.parse_streamlabs_mediashare(_sl_play_payload()) == [], (
        "resume/seek nembak 'play' lagi — share yang sama jangan diproses dobel"
    )
    assert len(dn.parse_streamlabs_mediashare(_sl_play_payload(req_id=999))) == 1


def test_streamlabs_mediasharevent_no_longer_recorded_as_unknown(tmp_path, monkeypatch):
    sample = tmp_path / "sample.jsonl"
    monkeypatch.setattr(dn, "_SL_SAMPLE_PATH", str(sample))
    dn.record_unknown_streamlabs({"type": "mediaShareEvent", "message": {}})
    assert not sample.exists(), "type sudah dikenal — stop merekam (anti spam berkas)"


def test_qa_window_keeps_general_rag_alive():
    """Live sore2 2026-08-02: 5 jendela Q&A beruntun mematikan RAG umum ~separuh
    sesi (20x skip) — padahal doc video justru dicari lewat RAG umum. Jendela
    klip media share wajib menyalakan watch_party_allow_general_rag."""
    import time as _t

    import arti_vault_rag as rag
    import hermes_vtuber_bridge as b

    b._video_set_qa_window("video-ragtest1234", 0.05)
    assert rag.should_skip_general_live_rag(b.CONFIG) is False, (
        "RAG umum harus tetap hidup selama Q&A klip"
    )
    _t.sleep(0.5)
    assert b.CONFIG.get("watch_party_enabled") is False
    assert b.CONFIG.get("watch_party_allow_general_rag") is False
