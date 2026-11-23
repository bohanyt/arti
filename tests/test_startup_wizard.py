"""Test the startup wizard with deterministic input, probes, and temp files."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from types import SimpleNamespace
from unittest.mock import patch


def _wizard_case(bridge, root, *, bridge_file):
    config = {
        "youtube_video_id": "previous-ID",
        "youtube_chat_enabled": False,
        "vts_api_port": 8002,
        "subtitle_port": 9988,
        "tts_engine": "supertone",
        "api_provider": "groq",
        "openrouter_api_key": "synthetic",
    }
    answers = iter(("https://www.youtube.com/watch?v=aBcDeFgHiJk", "8012", "9992", "4", "9993"))
    events = []

    def ask(prompt):
        answer = next(answers)
        print(prompt + answer)
        return answer

    def select_mic(cfg, *, resolve_mic_fn, ask_input):
        assert resolve_mic_fn() == (2, "Synthetic mic")
        cfg["asr_input_device"] = int(ask_input("  >> Nomor mic: "))
        events.append(("mic", cfg["asr_input_device"]))
        return True

    def bind(address):
        events.append(("bind", address))
        raise OSError("occupied")

    def token_path(path, port):
        events.append(("token_path", path, port))
        return root / f"vts_token_{port}.txt"

    def read_token(path, port, *, main_port):
        events.append(("read_token", path, port, main_port))
        return None

    sock = SimpleNamespace(
        settimeout=lambda timeout: None,
        connect_ex=lambda address: events.append(("vts", address)) or 1,
        close=lambda: None,
        setsockopt=lambda *args: None,
        bind=bind,
    )
    sockets = SimpleNamespace(AF_INET=2, SOCK_STREAM=1, SOL_SOCKET=1, SO_REUSEADDR=2, socket=lambda *args: sock)
    auth = SimpleNamespace(token_path=token_path, read_token=read_token)
    stdout = StringIO()
    with (
        patch.multiple(
            bridge, CONFIG=config, __file__=bridge_file, sys=SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
            bridge_health=SimpleNamespace(prompt_mic_selection=select_mic),
            arti_vault_rag=SimpleNamespace(prewarm_embedding=lambda cfg: events.append(("prewarm", cfg is config))),
            resolve_asr_input_device=lambda: (2, "Synthetic mic"),
            socket=sockets, arti_vts_auth=auth,
            _supertone_preflight=lambda: events.append(("preflight",)),
            save_live_session=lambda: events.append(("save", _selected_config(config))),
        ),
        patch.object(bridge, "input", ask, create=True),
        patch("subtitle_server.bind_host", return_value="127.0.0.1"),
        redirect_stdout(stdout),
    ):
        changed = bridge.startup_wizard()
    assert next(answers, None) is None
    return changed, _selected_config(config), stdout.getvalue(), events


def _selected_config(config):
    return tuple(config[key] for key in (
        "youtube_video_id", "youtube_chat_enabled", "vts_api_port", "subtitle_port", "asr_input_device", "tts_voice",
    ))


def _save_case(bridge, root):
    path = root / "config_local.json"
    path.write_text(json.dumps({"unrelated": "café", "vts_api_port": 8002}), encoding="utf-8")
    config = {
        "youtube_video_id": "aBcDeFgHiJk",
        "youtube_chat_enabled": True,
        "vts_api_port": 8012,
        "subtitle_port": 9993,
        "tts_voice": "skip me",
    }
    stdout = StringIO()
    with patch.multiple(bridge, CONFIG=config, _SCRIPT_DIR=str(root)), redirect_stdout(stdout):
        bridge._save_config_to_file()
    return sorted(file.name for file in root.iterdir()), path.read_bytes(), stdout.getvalue()


_WIZARD_TRANSCRIPT = """
============================================================
  ARTI BRIDGE — Startup Checklist
============================================================

============================================================
  LIVE SESSION SETUP  (Enter = keep current values)
============================================================
  YouTube : previous-ID  chat=OFF
  VTS port: 8002
  Subtitle: 9988  |  Profil: default
  Mic     : #2 Synthetic mic
------------------------------------------------------------
  >> YouTube URL/ID (off=matikan chat, Enter=keep): https://www.youtube.com/watch?v=aBcDeFgHiJk
  [OK] YouTube -> aBcDeFgHiJk (chat ON)
  >> VTS port Arti (Enter=8002): 8012
  [OK] VTS port -> 8012
  >> Subtitle port (Enter=9988): 9992
  [OK] Subtitle port -> 9992
  >> Nomor mic: 4

  [WARN] VTS di port 8012 tidak terdeteksi!
  Tips: Dua instance VTS? Biasanya port 8001 (instance pertama) dan 8002 (instance kedua).
  [INFO] Jalankan VTS + Start API di port 8012, atau restart wizard.

  [OK] YouTube Video ID: aBcDeFgHiJk

  [WARN] Port subtitle 9992 bentrok (TIME_WAIT atau dipakai proses lain).
  >> Ketik port subtitle baru (Enter = tetap 9992): 9993
  [OK] Subtitle port diganti ke 9993

  [WARN] TTS voice belum diset.
  [OK] Default voice: id-ID-GadisNeural

  [INFO] Virtual cable akan dicari otomatis saat TTS init.

  [INFO] VTS token tidak ditemukan — akan minta ALLOW saat connect.

  [OK] API provider: groq
  [WARN] Groq API key belum valid!

  [INFO] OpenRouter key: SET

============================================================
  Checklist selesai! Bridge siap start.
============================================================

"""

_SAVED_CONFIG = """{
  "unrelated": "café",
  "vts_api_port": 8012,
  "youtube_video_id": "aBcDeFgHiJk",
  "youtube_chat_enabled": true,
  "subtitle_port": 9993
}
"""


def test_wizard_full_transcript_matches_base(tmp_path):
    import arti_bridge

    changed, selected, transcript, events = _wizard_case(arti_bridge, tmp_path, bridge_file="explicit-bridge.py")
    assert changed is True
    assert selected == ("aBcDeFgHiJk", True, 8012, 9993, 4, "id-ID-GadisNeural")
    assert transcript == _WIZARD_TRANSCRIPT
    assert events == [
        ("prewarm", True), ("mic", 4), ("vts", ("localhost", 8012)), ("bind", ("127.0.0.1", 9992)),
        ("preflight",), ("token_path", "explicit-bridge.py", 8012), ("read_token", "explicit-bridge.py", 8012, 8012),
        ("save", selected),
    ]


def test_wizard_save_path_and_bytes_match_base(tmp_path):
    import arti_bridge

    files, content, transcript = _save_case(arti_bridge, tmp_path)
    assert files == ["config_local.json"]
    assert content == _SAVED_CONFIG.replace("\n", os.linesep).encode("utf-8")
    assert transcript == "[CONFIG] Tersimpan ke config_local.json (4 key per-mesin)\n"
