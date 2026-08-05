"""D1 (v0.7): listener donasi Saweria — normalisasi payload & wiring.

Protokol diverifikasi dari source wrapper Node (endpoints/Client/interfaces.ts):
wss://events.saweria.co/stream?streamKey=X, pesan {"type":"donation","data":[...]},
media share membawa ID video YouTube + start/end (jembatan Fitur E).
Semua test tanpa jaringan.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_donations as dn


def _msg(data):
    return json.dumps({"type": "donation", "data": data})


def test_parse_normal_donation():
    evs = dn.parse_saweria_event(_msg([{
        "amount": "20000", "donator": "Budi", "message": "semangat arti!",
        "tts": "", "sound": None,
    }]))
    assert len(evs) == 1
    ev = evs[0]
    assert (ev.platform, ev.name, ev.amount) == ("saweria", "Budi", 20000.0)
    assert ev.amount_label == "Rp 20.000"
    assert ev.message == "semangat arti!"
    assert ev.media_url == ""


def test_parse_media_share_carries_youtube_id():
    evs = dn.parse_saweria_event(_msg([{
        "amount": 50000, "donator": "Sinta", "message": "tonton ini dong",
        "media": {"id": "dQw4w9WgXcQ", "start": 10, "end": 40, "type": "youtube"},
    }]))
    ev = evs[0]
    assert ev.media_url == "https://youtu.be/dQw4w9WgXcQ"
    assert (ev.media_start, ev.media_end) == (10, 40)


def test_parse_gif_media_src_is_not_video_share():
    """Donasi normal bisa bawa media.src (gif) — TIDAK dianggap video share."""
    evs = dn.parse_saweria_event(_msg([{
        "amount": 10000, "donator": "X", "message": "",
        "tts": "", "media": {"src": ["https://.../a.gif"], "tag": "gif"},
    }]))
    assert evs[0].media_url == ""


def test_parse_batch_and_garbage():
    evs = dn.parse_saweria_event(_msg([
        {"amount": 1000, "donator": "A", "message": ""},
        {"amount": 2000, "donator": "B", "message": "hai"},
    ]))
    assert [e.name for e in evs] == ["A", "B"]
    assert dn.parse_saweria_event('{"type":"ping"}') == []
    assert dn.parse_saweria_event("bukan json {{{") == []
    assert dn.parse_saweria_event(None) == []


def test_format_trigger_matches_d0_wrapper_shape():
    ev = dn.DonationEvent(platform="saweria", name="Budi", amount=20000,
                          message="semangat!")
    t = dn.format_donation_trigger(ev)
    assert t.startswith("[DONASI Saweria Rp 20.000 dari Budi]:")
    assert "semangat!" in t

    ev2 = dn.DonationEvent(platform="saweria", name="Sinta", amount=50000,
                           media_url="https://youtu.be/x")
    t2 = dn.format_donation_trigger(ev2)
    assert "tanpa pesan" in t2
    assert "video" in t2, "media share harus disebut supaya Arti merespon videonya"


def test_start_listeners_gated(monkeypatch):
    # Mesin Bohan PUNYA key/token di env — tanpa delenv, test ini menyalakan
    # listener jaringan sungguhan (pelajaran machine-dependence).
    monkeypatch.delenv("SAWERIA_STREAM_KEY", raising=False)
    monkeypatch.delenv("STREAMLABS_SOCKET_TOKEN", raising=False)
    assert dn.start_donation_listeners({"donation_enabled": False}, lambda e: None) == []
    started = dn.start_donation_listeners(
        {"donation_enabled": True, "saweria_stream_key": ""}, lambda e: None
    )
    assert started == [], "tanpa key: dilewati dengan log, bukan crash"


def test_bridge_wiring_history_media_and_delay():
    src = (ROOT / "hermes_vtuber_bridge.py").read_text(encoding="utf-8")
    assert '"donation_enabled": False,' in src, "kill switch wajib default OFF"
    assert "start_donation_listeners(CONFIG, _on_donation)" in src
    i_cb = src.index("def _on_donation")
    seg = src[i_cb:i_cb + 2800]  # _on_donation tumbuh (branch media_points Fitur E)
    assert "add_to_history" in seg, "donasi harus tercatat di history untuk konteks LLM"
    assert "pending_media.append" in seg, "media share ditampung untuk Fitur E"
    assert "schedule_donation_trigger(" in seg, (
        "reaksi wajib lewat tundaan alert overlay yang sama dengan Super Chat"
    )


# --- D2: Streamlabs ------------------------------------------------------------


def test_parse_streamlabs_donation():
    evs = dn.parse_streamlabs_event({
        "type": "donation",
        "message": [{"name": "Kevin", "amount": "5.00",
                     "formatted_amount": "$5.00", "message": "keep it up!",
                     "currency": "USD"}],
    })
    assert len(evs) == 1
    ev = evs[0]
    assert (ev.platform, ev.name, ev.amount) == ("streamlabs", "Kevin", 5.0)
    assert ev.amount_label == "$5.00", "pakai formatted_amount dari platform"
    t = dn.format_donation_trigger(ev)
    assert t.startswith("[DONASI Streamlabs $5.00 dari Kevin]:")


def test_parse_streamlabs_ignores_relayed_superchat():
    """ANTI DOBEL: Super Chat YouTube yang di-relay Streamlabs diabaikan —
    D0 sudah menangkapnya langsung dari innertube."""
    for typ in ("superchat", "follow", "subscription", "membershipGift"):
        assert dn.parse_streamlabs_event({"type": typ, "message": [{"name": "X"}]}) == []
    assert dn.parse_streamlabs_event("bukan dict") == []


def test_streamlabs_token_resolution(monkeypatch):
    monkeypatch.delenv("STREAMLABS_SOCKET_TOKEN", raising=False)
    assert dn.resolve_streamlabs_token({}) == ""
    assert dn.resolve_streamlabs_token({"streamlabs_socket_token": "abc"}) == "abc"


def test_start_listeners_streamlabs_gated(monkeypatch):
    monkeypatch.delenv("SAWERIA_STREAM_KEY", raising=False)
    monkeypatch.delenv("STREAMLABS_SOCKET_TOKEN", raising=False)
    started = dn.start_donation_listeners({"donation_enabled": True}, lambda e: None)
    assert started == [], "tanpa key/token semua platform dilewati"
