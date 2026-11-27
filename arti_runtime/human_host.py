"""Read-only owner-hosted session gate and native night-wait facts.

Facts only: never sends input, grants authority, restarts a process, or touches the HOST game.
All human-host wait rules apply ONLY in a run a person hosts (`session_kind(run) == "owner"`,
no ArtiHostHelper); solo runs and unknown layouts fail closed to False so solo is byte-identical.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from scripts.live7.ready_checks import ready_check_waiting_for_peer

HUMAN_NIGHT_WAIT_MAX_S = 3600.0  # up to 60 minutes waiting for a person's end-of-night work
PEER_NO_SIGHT_FACT = "Di data game sekarang aku tidak melihat lokasi pemain lain."
_NIGHT_DONE_STATUSES = frozenset({"", "ready", "done", "complete", "completed", "hidden", "none"})
_OWN_NIGHT_MENUS = frozenset({"ShippingMenu", "LevelUpMenu"})


def _normalize_run_dir(path) -> Path | None:
    if not path:
        return None
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return None
    return p.parent if p.name == "runtime" else p


def run_dir_from_paths(paths) -> Path | None:
    """Resolve the run root from a Live7 backend's paths object (or a path directly)."""
    if paths is None:
        return None
    raw = getattr(paths, "RUN", None) if not isinstance(paths, (str, Path)) else paths
    return _normalize_run_dir(raw)


def run_dir_from_config(config) -> Path | None:
    """Resolve the run root carrying `trial-config.json` from a runtime/bridge config mapping."""
    if not isinstance(config, Mapping):
        return None
    candidates = []
    for key, parent_only in (
        ("stardew_live7_help_dir", False),
        ("arti_state_dir", True),
        ("host_story_relay_file", True),
        ("live7_help_dir", False),
    ):
        raw = config.get(key)
        if not raw:
            continue
        try:
            p = Path(raw)
        except (TypeError, ValueError):
            continue
        candidate = p.parent if parent_only else _normalize_run_dir(p)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        try:
            if (candidate / "trial-config.json").is_file():
                return candidate
        except OSError:
            continue
    return None


def is_owner_hosted_run(run) -> bool:
    """True iff `run` is canonically an owner-hosted run (`session_kind == 'owner'`, no helper)."""
    root = _normalize_run_dir(run)
    if root is None:
        return False
    try:
        from scripts.livepath.halt_authority import session_kind
        return session_kind(root) == "owner"
    except Exception:  # noqa: BLE001 - fail closed: unknown layout never relaxes solo bounds
        return False


def is_owner_hosted_config(config) -> bool:
    """True iff `config` belongs to an owner-hosted run (`session_kind == 'owner'`, no helper)."""
    if not isinstance(config, Mapping):
        return False
    if "stardew_host_controller" in config and config.get("stardew_host_controller") != "owner":
        return False
    run = run_dir_from_config(config)
    return is_owner_hosted_run(run) if run is not None else False


def _classify_peer_night_status(status_text: str) -> str | None:
    low = status_text.strip().casefold()
    if low in _NIGHT_DONE_STATUSES:
        return None
    if "ship" in low or "money" in low or "sell" in low:
        return "end_of_night_status:shipping"
    if "level" in low or "skill" in low or "profession" in low:
        return "end_of_night_status:level_up"
    if "save" in low or "saving" in low:
        return "end_of_night_status:save_barrier"
    if "bed" in low or "sleep" in low:
        return "end_of_night_status:bed"
    return "end_of_night_status:" + low[:48]


def _peer_bed_pending(peer_sleep) -> bool:
    if not isinstance(peer_sleep, Mapping) or peer_sleep.get("self_bed") is not True:
        return False
    if peer_sleep.get("peer_bed") is False:
        return True
    players = peer_sleep.get("players")
    if isinstance(players, list):
        return any(isinstance(p, Mapping) and p.get("self") is False and p.get("in_bed") is False for p in players)
    return False


def human_night_wait_fact(inspect_ctx=None, pose=None, *, self_player_id=None) -> str | None:
    """Return the native fact proving another player's end-of-night work is pending, or None.

    Reads native observations only (end_of_night_status, sleep/night ready_check, peer_sleep,
    SaveGameMenu/is_saving/new_day_sync_pending/end-of-night sync). Never infers from a timer.
    """
    ctx = inspect_ctx if isinstance(inspect_ctx, Mapping) else {}
    pose_map = pose if isinstance(pose, Mapping) else {}
    ui_pad = ctx.get("ui_pad") if isinstance(ctx.get("ui_pad"), Mapping) else {}
    interactive = ctx.get("interactive") if isinstance(ctx.get("interactive"), Mapping) else {}
    day_tr = ctx.get("day_transition") if isinstance(ctx.get("day_transition"), Mapping) else {}
    ui_key = ctx.get("ui_key") if isinstance(ctx.get("ui_key"), Mapping) else {}

    # 1. Native per-player end-of-night status (`Game1.player.team.endOfNightStatus`).
    statuses = ui_pad.get("end_of_night_status")
    if not isinstance(statuses, list):
        statuses = ctx.get("end_of_night_status")
    if isinstance(statuses, list):
        self_id = None if self_player_id is None else str(self_player_id)
        for entry in statuses:
            if not isinstance(entry, Mapping):
                continue
            pid = entry.get("player_id")
            if pid is None or (self_id is not None and str(pid) == self_id):
                continue
            raw_status = entry.get("status")
            if isinstance(raw_status, str):
                fact = _classify_peer_night_status(raw_status)
                if fact is not None:
                    return fact

    # 2. Native ready check where she is ready and another required player is not (sleep / night).
    for ready in (ui_pad.get("ready_check"), ctx.get("ready_check")):
        if ready_check_waiting_for_peer(ready):
            check = ready["check"]
            if check == "sleep" or day_tr.get("showing_end_of_night_stuff") is True or "night" in check.casefold() or "save" in check.casefold() or "day" in check.casefold():
                return "ready_check:" + check[:48]

    # 3. Native peer_sleep: she is in bed and the other player is not yet in bed.
    for ps in (pose_map.get("peer_sleep"), ctx.get("peer_sleep")):
        if _peer_bed_pending(ps):
            return "peer_sleep:host_not_in_bed"

    # 4. Native save barrier / end-of-night sync (never her own un-cleared ShippingMenu/LevelUpMenu).
    menu = interactive.get("menu_type") or ui_pad.get("menu_type") or pose_map.get("active_menu")
    if menu == "SaveGameMenu":
        return "save_barrier:SaveGameMenu"
    if ui_pad.get("is_saving") is True:
        return "save_barrier:is_saving"
    if day_tr.get("new_day_sync_pending") is True:
        return "save_barrier:new_day_sync_pending"
    if day_tr.get("showing_end_of_night_stuff") is True:
        ui_safety = ui_pad.get("ui_safety") if isinstance(ui_pad.get("ui_safety"), Mapping) else {}
        if menu == "ShippingMenu" and (
            ui_key.get("not_ready_reason") in ("saving", "outro")
            or (ui_safety.get("state") == "NIGHT_SUMMARY" and ui_safety.get("advance_ready") is False)
        ):
            return "end_of_night:shipping_sync"
        pending_own = day_tr.get("end_of_night_menus_pending")
        if menu not in _OWN_NIGHT_MENUS and not (type(pending_own) is int and pending_own > 0):
            return "end_of_night:showing_end_of_night_stuff"
    return None


def is_solo_run(run) -> bool:
    """True iff `run` is a helper-hosted solo run (`session_kind == 'solo'`)."""
    root = _normalize_run_dir(run)
    try:
        from scripts.livepath.halt_authority import session_kind
        return root is not None and session_kind(root) == "solo"
    except Exception:  # noqa: BLE001
        return False


def _peer_sleep_val(raw):
    inner = raw.get("value") if isinstance(raw, Mapping) else None
    return inner if isinstance(inner, Mapping) else (raw if isinstance(raw, Mapping) else None)


def waiting_for_peer_sleep(offer) -> bool:
    """True iff she is in bed waiting for another player (co-op waiting night)."""
    val = _peer_sleep_val(offer.get("peer_sleep")) if isinstance(offer, Mapping) else None
    if val is None: return False
    if _peer_bed_pending(val): return True
    ready = offer["unknown_ui"].get("ready_check") if isinstance(offer.get("unknown_ui"), Mapping) else None
    return val.get("self_bed") is True and ready_check_waiting_for_peer(ready) and ready.get("check") == "sleep"


def bounded_peer_sleep(raw) -> dict:
    """Bounded native peer_sleep: bed flags, names (<=40, <=8), optional place (<=64)."""
    val = _peer_sleep_val(raw)
    if val is None: return {}
    out = {k: val[k] for k in ("self_bed", "peer_bed") if isinstance(val.get(k), bool)}
    if isinstance(val.get("players"), list):
        out["players"] = [row for p in val["players"][:8] if isinstance(p, Mapping) and (row := {
            **{k: p[k] for k in ("self", "in_bed") if isinstance(p.get(k), bool)},
            **{k: p[k][:cap] for k, cap in (("name", 40), ("role", 24)) if isinstance(p.get(k), str)},
            **({"place": p["place"].strip()[:64]} if isinstance(p.get("place"), str) and p["place"].strip() else {}),
        })]
    return out


def peer_sight_fact(peer_sleep, *, now: float, max_age: float = 45.0,
                    owner_hosted: bool = False, native_fresh: bool = False) -> str:
    """One <=120-char voice fact from fresh native peer_sleep, or '' when solo/stale/inactive."""
    stamp, val = (peer_sleep.get("observed_ts"), peer_sleep.get("value")) if isinstance(peer_sleep, Mapping) else (None, None)
    if type(stamp) in (int, float) and 0 <= now - stamp <= max_age and isinstance(val, Mapping):
        others = sorted((p for p in (val.get("players") or ()) if isinstance(p, Mapping)
                         and p.get("self") is False and p.get("controller") != "harness"
                         and isinstance(p.get("place"), str) and p["place"].strip()),
                        key=lambda p: 0 if p.get("role") == "host farmer" else 1)
        if others:
            from arti_help_bus import sanitize_hint_text
            from arti_runtime.place_names import place_label
            p = others[0]
            who = (sanitize_hint_text(p["name"].strip())[:40] if isinstance(p.get("name"), str) and p["name"].strip() else "") or "pemain lain"
            raw_p = p["place"].strip()[:64]
            if label := sanitize_hint_text(place_label(raw_p) or raw_p):
                secs = max(0, int(now - stamp))
                prefix, suffix = f"Lokasi {who}: ", f" ({secs} dtk lalu)." if secs < 60 else f" ({secs // 60} mnt lalu)."
                return f"{prefix}{label[:max(1, 120 - len(prefix) - len(suffix))].rstrip()}{suffix}"[:120]
    return PEER_NO_SIGHT_FACT if owner_hosted and native_fresh else ""
