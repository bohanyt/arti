"""Curious proactive trigger — comment on screen when idle."""

from __future__ import annotations

import time

import arti_screen_context as sc
import arti_vision_client


_last_curious_ts = 0.0
_last_interval_check_ts = 0.0


def reset_session() -> None:
    global _last_curious_ts, _last_interval_check_ts
    _last_curious_ts = 0.0
    _last_interval_check_ts = 0.0


def _vision_effective(config: dict) -> bool:
    """Manual toggle OR scouter auto-window (mirrors bridge is_vision_active)."""
    if not config.get("vision_enabled", config.get("screen_context_enabled", False)):
        return False
    if config.get("vision_runtime_on", False):
        return True
    return time.time() < float(config.get("vision_auto_until", 0))


def should_fire(
    config: dict,
    *,
    brain_busy: bool,
    tts_playing: bool,
    ptt_active: bool,
    yt_cooling: bool = False,
    yt_queue_pending: bool = False,
    streamer_recent: bool = False,
) -> bool:
    """True if curious may queue a proactive turn."""
    if not config.get("curious_enabled", False):
        return False
    if not _vision_effective(config):
        return False
    if brain_busy or tts_playing or ptt_active or yt_cooling:
        return False
    if yt_queue_pending:
        return False
    if streamer_recent:
        return False

    now = time.time()
    interval = float(config.get("curious_interval_sec", 75))
    cooldown = float(config.get("curious_cooldown_sec", 120))

    global _last_interval_check_ts
    if now - _last_interval_check_ts < interval:
        return False

    if _last_curious_ts and (now - _last_curious_ts) < cooldown:
        return False

    manual_vision = bool(config.get("vision_runtime_on", False))
    scouter = config.get("scouter_last_result") or {}
    curious_worthy = bool(scouter.get("curious_worthy", False))

    if not manual_vision and not curious_worthy:
        return False

    if config.get("curious_requires_fresh_screen", True):
        if not arti_vision_client.is_vision_fresh(config):
            return False

    latest = sc.screen_ring.latest()
    if not latest or not latest.scene.strip():
        return False

    _last_interval_check_ts = now
    return True


def mark_fired() -> None:
    global _last_curious_ts
    _last_curious_ts = time.time()


def build_prompt(config: dict) -> str:
    """User message for curious LLM turn."""
    latest = sc.screen_ring.latest()
    scene = (latest.scene if latest else "").strip()
    playback = latest.playback_mmss if latest else None
    scouter = config.get("scouter_last_result") or {}
    hook = (scouter.get("curious_hook") or "").strip()

    parts = ["[Curious — reaksi proaktif]"]
    if hook:
        parts.append(f"Sudut komentar: {hook}")
    parts.append(f"Layar saat ini: {scene}")
    if playback:
        parts.append(f"Posisi video: {playback}")
    parts.append(
        "Komentari apa yang terlihat di layar secara singkat dan natural sebagai Arti. "
        "Jangan ulang pertanyaan ke streamer. 2-3 kalimat Bahasa Indonesia."
    )
    return "\n".join(parts)


def prepare_for_fire(config: dict) -> bool:
    """Refresh vision if stale; return True if ready."""
    if not _vision_effective(config):
        return False
    if config.get("curious_requires_fresh_screen", True):
        if not arti_vision_client.is_vision_fresh(config):
            snap, provider = arti_vision_client.refresh_if_stale(config)
            if snap and snap.scene:
                sc.update_watch_state_from_snapshot(
                    snap,
                    event_id=str(config.get("watch_party_event_id") or ""),
                )
                print(f"[Curious] Refreshed vision via {provider}")
            if not arti_vision_client.is_vision_fresh(config):
                return False
    return bool(sc.screen_ring.latest() and sc.screen_ring.latest().scene.strip())
