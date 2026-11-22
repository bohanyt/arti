"""F6 (an internal design note): Stardew status/verification lines are never voiced.

The bridge's only Stardew->TTS boundary is ``sanitize_trusted_speech(result.speech)`` in
``_try_stardew_owner_generation``; a ``StatusSpeech`` line becomes silence there, while
model speech, owner questions and the short reaction lines are voiced as before.
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_stardew_runtime as rt_mod  # noqa: E402
from arti_stardew_runtime import OwnerTurnResult, StatusSpeech, reaction_line, sanitize_trusted_speech  # noqa: E402


def test_status_speech_is_silent_at_the_voice_boundary_but_kept_as_record():
    line = StatusSpeech("Kriteria tujuan sudah terverifikasi dari pengamatan terbaru.")
    assert line == "Kriteria tujuan sudah terverifikasi dari pengamatan terbaru."
    assert sanitize_trusted_speech(line, {}) == ""
    result = OwnerTurnResult(admitted=True, applied=True, skip_reply_tags=True, reason="finish", speech=line)
    assert asdict(result)["speech"] == line
    assert json.loads(json.dumps(asdict(result)))["speech"] == line


def test_plain_speech_and_reactions_are_still_voiced():
    assert sanitize_trusted_speech("Halo kebun [MODE: host]", {}).strip() == "Halo kebun"
    for kind in ("stopped", "achieved", "unsure"):
        line = reaction_line(kind)
        assert type(line) is str and line in rt_mod._REACTIONS[kind]
        assert sanitize_trusted_speech(line, {}) == line
        assert "terverifikasi" not in line.lower() and "kriteria" not in line.lower()


def _speech_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # Only what an OwnerTurnResult carries: speech=<plain literal> (a local `speech` variable is
        # wrapped where it is passed, speech=StatusSpeech(speech)).
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"OwnerTurnResult", "_R"}):
            continue
        for kw in node.keywords:
            if kw.arg != "speech":
                continue
            if isinstance(kw.value, ast.Call) and getattr(kw.value.func, "id", None) == "StatusSpeech":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield node.lineno, sub.value


def test_no_plain_verification_narration_reaches_owner_turn_speech():
    hits = []
    for path in sorted(ROOT.glob("arti_stardew*.py")):
        for lineno, text in _speech_literals(path):
            low = text.lower()
            if "terverifikasi" in low or "kriteria" in low or "waktu di host" in low:
                hits.append(f"{path.name}:{lineno}: {text}")
    assert not hits, "wrap status lines in StatusSpeech (F6):\n" + "\n".join(hits)


def test_help_relay_prompt_keeps_prefix_and_asks_for_natural_words():
    import arti_help_relay

    src = Path(arti_help_relay.__file__).read_text(encoding="utf-8")
    assert arti_help_relay.HELP_TRIGGER_PREFIX == "[Stardew] Aku buntu:"
    assert "pakai kata-katamu sendiri" in src and "jangan bacain kode" in src and "!bantu" in src
