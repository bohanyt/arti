"""ADR 196: lawan_mine ends with native progress facts, not a blanket failure."""

from arti_runtime import wait_ledger as W
from collections.abc import Mapping

PROGRESS_REASONS = (
    "floor_exhausted", "exited_mines", "skill_floor_five", "no_effect_progress",
    "alive_exit_unverified", "partial:mines_floor_kill_alive_exit_unverified",
    "under_attack", "curfew", "partial:mines_combat_disengaged",
    "partial:mines_curfew_exit", "partial:mines_energy_exit", "partial:mines_budget_exit",
)
REAL_FAIL_MARKERS = (
    "knock", "hp_critical", "mines_vitals",
    "unsupported", "mines_time_window", "weapon", "player_cannot_move",
)


def _int(value, default=0):
    return value if type(value) is int else default


def facts(run):
    state = getattr(run, "state", None) or {}
    batch = state.get("mines_last_batch") or {}
    if not isinstance(batch, Mapping):
        batch = {}
    rocks = _int(state.get("wave1_count"))
    kills = _int(state.get("wave1_kills"))
    descents = _int(state.get("wave1_descents"))
    floor = _int(state.get("wave1_floor"))
    entry = _int(state.get("wave1_entry_floor"), 1 if floor else 0)
    floors = max(descents, max(0, floor - entry) if floor else 0)
    swings = _int(batch.get("swings"), _int(state.get("mines_swings")))
    hits = batch.get("hits") if type(batch.get("hits")) is int else None
    damage = _int(batch.get("damage_dealt"))
    items = _int(state.get("wave1_items"))
    hp_lost = state.get("wave1_hp_lost")
    weapon = state.get("wave1_weapon_in_hand")
    if weapon is None and type(batch.get("weapon_held")) is bool:
        weapon = batch.get("weapon_held")
    return dict(floors_gained=floors, rocks=rocks, kills=kills, items=items,
                hp_lost=hp_lost if type(hp_lost) is int else None,
                swings=swings, hits=hits, damage_dealt=damage,
                weapon_in_hand=weapon, alive_exit=state.get("alive_exit"),
                hold=state.get("wave1_hold"), floor=floor or None,
                stop_reason=state.get("mines_stop_reason"))


def has_progress(row):
    return bool(row.get("floors_gained") or row.get("kills") or row.get("rocks") or row.get("items"))


def _progress_hold(reason):
    text = str(reason or "")
    return any(text == token or text.startswith(token + ":") for token in PROGRESS_REASONS)


def _real_failure(reason):
    text = str(reason or "")
    return any(token in text for token in REAL_FAIL_MARKERS)


@W.span('validation', 79, runtime=False)
def close(run, kind, reason):
    state = getattr(run, "state", None)
    if isinstance(state, dict) and reason != "observed_mines_progress":
        state.setdefault("mines_stop_reason", reason)
    row = facts(run)
    if isinstance(state, dict):
        state["mines_progress"] = row
    if kind != "fail" or (state or {}).get("mines_death") or _real_failure(reason) or not has_progress(row):
        return kind, reason
    if not _progress_hold(reason) and reason not in ("exited_mines",):
        return kind, reason
    return "done", "observed_mines_progress"


@W.span('validation', 80, runtime=False)
def completion(run, outcome, cleanup, last):
    """ADR 208: proof of Mines work/exit is distinct from readiness for the next input.

    The caller still applies STOP, identity, mutation, lifecycle and release fences.
    No preparatory press/approach receipt proves a descent or a completed exit.
    """
    from arti_runtime.action_outcomes import get
    if outcome[0] != "done" or not isinstance(cleanup, Mapping):
        return False
    if (cleanup.get("input_released") is not True or cleanup.get("menu") is not None
            or cleanup.get("free") is not True or cleanup.get("food_clear") is False):
        return False
    if run.call.name == "lawan_mine" and outcome[1] == "observed_mines_progress":
        return not run.state.get("mines_death") and has_progress(facts(run))
    if outcome[1] != "exited_mines" or get(last, "status") != "achieved":
        return False
    transition = (get(last, "observed") or {}).get("exited_mines")
    if (not isinstance(transition, str) or not transition.startswith("UndergroundMine")
            or not transition.endswith("->Mine") or cleanup.get("location") != "Mine"):
        return False
    origin = transition.split("->")[0]
    return (run.call.name == "lawan_mine"
            or run.call.name == "aksi" and str(run.call.arg).startswith("xt:mine:" + origin + ":")
            or run.call.name == "pergi_ke" and run.call.arg == "mine")
