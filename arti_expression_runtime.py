"""Expression + emotion overlay runtime — VTS expression files only (no FaceAngle inject)."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

EMOTION_MAP: dict[str, str | None] = {
    # "senang" SENGAJA tanpa berkas overlay (keputusan Bohan 2026-08-01): wajah
    # default Arti sudah senyum content — itulah ekspresi senangnya. Dua percobaan
    # overlay senyum (halus & lebar) dua-duanya kelihatan janggal di mata Bohan dan
    # sudah dihapus dari folder model. None = tag [EMOTION:senang] tetap dikenali
    # (nod tetap jalan), cuma tidak ada overlay wajah.
    "senang": None,
    "sedih": "ArtiSedih.exp3.json",
    "marah": "ArtiMarah.exp3.json",
    "bingung": "ArtiBingung.exp3.json",
    "neutral": None,
}

_EXPR_BICARA = "ArtiBicara.exp3.json"
_VTS_MOOD_DIR = os.environ.get(
    "VTS_MODEL_DIR",
    r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
    r"\VTube Studio_Data\StreamingAssets\Live2DModels\YOUR_MODEL",
)


def set_vts_mood_dir(path: str) -> None:
    """Arahkan audit ekspresi ke folder model VTS yang benar.

    Dipanggil bridge saat resolve CONFIG["vts_model_dir"]. Tanpa ini, audit membaca
    fallback placeholder YOUR_MODEL (sisa scrub publik) — folder yang tidak ada —
    sehingga `exists: False` untuk SEMUA mood dan WARN "tidak punya param alis/mata"
    menyala palsu di tiap jawaban, padahal ekspresinya sehat. Saudara kembar bug
    idle poses yang diperbaiki df074d1; jalur audit ini yang waktu itu kelewat.
    """
    global _VTS_MOOD_DIR
    if path and "YOUR_MODEL" not in path:
        _VTS_MOOD_DIR = path

# Params mood overlay must not touch (lip-sync, lampu, deformasi mulut)
_MOOD_STRIP_IDS = frozenset({
    "ParamMouthOpenY",
    "ParamMouthForm",
    "Param48",
    "Param122",
    "Param125",
    "Param183",
    "Param186",
    "Param130",
    "Param96",
    "Param97",
    "Param2",
})

EMOTION_TAG_RE = re.compile(r"\[EMOTION:(\w+)\]\s*", re.IGNORECASE)

EMOTION_PROMPT_SUFFIX = (
    "\n[EMOSI] Akhiri jawaban dengan tag tersembunyi "
    "[EMOTION:senang|sedih|marah|bingung|neutral] — tag tidak diucapkan. "
    "Kalau viewer minta ekspresi (mis. muka sedih/senang), pakai tag yang cocok."
)

# Fallback kalau LLM lupa tag tapi user jelas minta mood
_USER_EMOTION_HINTS: dict[str, tuple[str, ...]] = {
    "sedih": ("muka sedih", "ekspresi sedih", "wajah sedih", "sedih"),
    "senang": ("muka senang", "senyum", "senang", "bahagia"),
    "marah": ("muka marah", "marah", "kesel", "ngamuk"),
    "bingung": ("bingung", "muka bingung", "confused"),
}

# Nod saat TTS per mood (sedih = pose kepala turun, tanpa ngangguk)
EMOTION_NOD_ENABLED: dict[str, bool] = {
    "neutral": True,
    "senang": True,
    "marah": True,
    "bingung": True,
    "sedih": False,
}


def should_nod_for_emotion(emotion: str, config: dict) -> bool:
    if not config.get("expression_nod_enabled"):
        return False
    return EMOTION_NOD_ENABLED.get(emotion, True)


def audit_mood_exp_on_disk(mood_file: str) -> dict[str, Any]:
    """Cek param bermasalah yang masih ada di file ekspresi VTS."""
    path = os.path.join(_VTS_MOOD_DIR, mood_file)
    out: dict[str, Any] = {"mood_file": mood_file, "path": path, "exists": os.path.isfile(path)}
    if not out["exists"]:
        return out
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        ids = {p.get("Id") for p in data.get("Parameters", []) if p.get("Id")}
        out["param_count"] = len(ids)
        out["blocked_still_present"] = sorted(ids & _MOOD_STRIP_IDS)
        out["has_param_angle_y"] = "ParamAngleY" in ids
        out["has_eye_deform"] = (
            "ParamEyeLOpen" in ids or "ParamEyeROpen" in ids
        )
        out["has_brow_deform"] = (
            "ParamBrowLForm" in ids or "ParamBrowRForm" in ids
        )
    except Exception as e:
        out["error"] = type(e).__name__
    return out


# Permintaan ekspresi EKSPLISIT ("pasang muka marah") — menang atas tag LLM.
# Live 2026-08-02: "kalo kamu pasang muka marah?" ditandai LLM [EMOTION:senang]
# -> wajah tetap default; Bohan harus maksa dua kali baru marah tampil.
_EXPLICIT_FACE_RE = re.compile(
    r"(?:pasang|coba|tunjuk(?:in|kan)?|kasih|bikin)\s+(?:aja\s+)?"
    r"(?:muka|wajah|ekspresi)\s+(senang|sedih|marah|bingung)",
    re.IGNORECASE,
)


def resolve_turn_emotion(user_speech: str, reply_emotion: str) -> str:
    """Permintaan eksplisit menang; lalu tag LLM; kalau neutral, hint lembut."""
    t = (user_speech or "").lower()
    m = _EXPLICIT_FACE_RE.search(t)
    if m:
        return m.group(1).lower()
    if reply_emotion and reply_emotion != "neutral":
        return reply_emotion
    for emo, hints in _USER_EMOTION_HINTS.items():
        if any(h in t for h in hints):
            return emo
    return reply_emotion or "neutral"

_ALL_MOOD_FILES = tuple(f for f in EMOTION_MAP.values() if f)


def parse_reply_emotion(text: str) -> tuple[str, str]:
    """Strip [EMOTION:...] tag; return (clean_text, emotion_key)."""
    if not text:
        return "", "neutral"
    match = EMOTION_TAG_RE.search(text)
    emotion = "neutral"
    if match:
        key = match.group(1).lower()
        if key in EMOTION_MAP:
            emotion = key
    cleaned = EMOTION_TAG_RE.sub("", text).strip()
    return cleaned, emotion


def emotion_prompt_for_system(system_prompt: str, config: dict) -> str:
    if not config.get("expression_emotion_enabled"):
        return system_prompt
    return system_prompt + EMOTION_PROMPT_SUFFIX


async def apply_turn_start(vts: Any, stop_idle_fn: Callable[[], None], config: dict) -> None:
    """stop_idle + aware before mikir (CONFIG gated)."""
    if not config.get("expression_emotion_enabled"):
        return
    stop_idle_fn()
    await vts.trigger_expression_state("aware")


async def apply_speaking(vts: Any, emotion: str, config: dict) -> None:
    """bicara + optional mood overlay; re-assert bicara after mood (H-B lamp/mouth)."""
    await vts.trigger_expression_state("bicara")
    mood_file = None
    if config.get("expression_emotion_enabled"):
        mood_file = EMOTION_MAP.get(emotion)
    audit = audit_mood_exp_on_disk(mood_file) if mood_file else {}
    if mood_file:
        await vts.send_expression(mood_file, True)
        # Mood overlay can deactivate ArtiBicara in VTS — re-assert lamp, then mood again
        await vts.send_expression(_EXPR_BICARA, True, confirm=True)
        await vts.send_expression(mood_file, True)
        # Dua kegagalan BERBEDA — jangan dilebur jadi satu pesan (dulu dilebur, dan
        # WARN "tidak punya param alis/mata" menutupi fakta bahwa ArtiSenyum.exp3.json
        # tidak ada sama sekali di model — mood senang gagal diam-diam berbulan-bulan).
        if not audit.get("exists", True):
            print(
                f"[Expr] WARN: {mood_file} TIDAK ADA di folder model VTS — "
                "mood ini tidak akan pernah kelihatan. Buat ekspresinya atau "
                "perbaiki vts_model_dir."
            )
        elif not audit.get("has_brow_deform") and not audit.get("has_eye_deform"):
            print(
                f"[Expr] WARN: {mood_file} tidak punya param alis/mata — "
                "mood mungkin tidak kelihatan (cek folder model VTS)."
            )


async def apply_turn_end(vts: Any, config: dict) -> None:
    """Kembali ke default; matikan mood overlay tanpa frame kosong."""
    await vts.trigger_expression_state("default")
    if config.get("expression_emotion_enabled"):
        for mood_file in _ALL_MOOD_FILES:
            await vts.send_expression(mood_file, False)
