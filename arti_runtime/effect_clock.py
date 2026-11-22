"""ADR 241 A: the effect clock, one number kept by the runtime above every skill.

The number is the seconds since something she did last changed the game. "Changed" is read from the game:
her map or tile, an item count, money, a tool effect on a tile, a menu opened or closed by her choice, a
dialogue she advanced, the game day. A page rendered, a re-observation, a refused or zero-input choice, a
program recovery row, her speech and the game clock alone do not reset it. A menu she reopens while nothing
else changed is a re-read of the same thing, not a change. A choice of hers to wait or to watch is honoured for
WAIT_HONOUR_S (at most WAIT_HONOUR_MAX_S per stretch without effect); the clock keeps counting, the ladder
(ADR 241 B) reads `ladder_age`, which leaves the honoured seconds out.

Observe-only: nothing here sends input, grants permission or ends anything. The same `effect_of` classifies a
journal row for the live runtime and for the archive replay, so the replay table and the soak's proof mark
read the same rule.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import time

ROW_INTERVAL_S = 30.0  # one steady row this often, plus one row on every reset
# Her own wait/watch choice. The healthy 2026-11-18 21:38 run (10 days, 2571 effects) had no stretch without effect
# longer than 41 s (median 2.3 s, p90 7.1 s); 120 s is three times that and two rung-1 bounds.
WAIT_HONOUR_S = 120.0
WAIT_HONOUR_MAX_S = 300.0
QUIET_CHANGES = frozenset({"pixel", "facing"})  # a pixel nudge or a turn on the spot is no change of tile
_WORLD = (("location", "map"), ("tile", "tile"), ("inventory", "items"), ("money", "money"),
          ("crops", "tile_effect"), ("world", "tile_effect"))
EFFECT_ROWS = {"tool_work_effect": "tile_effect", "extras_effect": "tile_effect", "interaction_effect": "interaction",
               "item_move_outcome": "items"}
# fix144A (ADR 249 C14; review R4 SF4): verdicts that say the game ANSWERED a plain press, a step, a turn, a slot pick or
# a menu opening (`arti_stardew_generic_world.py`), not that anything changed: a raw A into empty space finishes
# `observed_generic_readback` on WorldUnchanged, a step into a wall `observed_movement`. Their real changes (tile,
# items, money, a menu) are read from the game by `observe` and the action rows; the finish row adds no change.
ANSWER_ONLY = frozenset({"observed_generic_readback", "observed_movement", "observed_facing", "observed_equipped_item",
                         "observed_menu_open"})


def effect_of(row) -> str | None:
    """What a journal row shows her action changed in the game, or None (program activity, a read, a refusal)."""
    if not isinstance(row, Mapping):
        return None
    ev = row.get("ev")
    if ev == "action_result":
        action = row.get("action") if isinstance(row.get("action"), Mapping) else {}
        changes = sorted(set(action.get("observed_changes") or ()) - QUIET_CHANGES)
        if action.get("world_effect_status") == "observed_change" and changes:
            return "act:" + changes[0]
        return None
    if ev == "part_live_row":
        if (row.get("verified") is True and row.get("outcome_class") == "VERIFIED"
                and row.get("reason") not in ANSWER_ONLY):
            return "verified:" + str(row.get("skill"))
        return None
    if ev in EFFECT_ROWS:
        status = row.get("world_effect_status")
        if status is not None and "change" not in str(status):
            return None
        return EFFECT_ROWS[ev]
    if ev == "live7_ui_choice":
        # Her own press inside a menu that the game took (a page turned, a dialogue advanced, an answer given).
        # A controller row (menu deadline, recovery close, the ladder) carries `controller` and is never hers.
        if row.get("verified") is True and "controller" not in row and row.get("pick") not in (None, "wait"):
            return "her_press"
        return None
    return None


def _world_diff(before, after):
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    for key, cause in _WORLD:
        if key in before and key in after and after[key] is not None and before[key] != after[key]:
            return cause
    return None


@dataclass
class EffectClock:
    since: float
    reset_by: str = "start"
    since_wall: float = field(default_factory=time.time)
    evidence: dict | None = None
    date: tuple | None = None
    menu: str | None = None
    menu_seen: bool = False  # the first reading of a menu is a baseline, not her choice
    menu_counted: bool = False
    menu_world: dict = field(default_factory=dict)  # menu kind -> world serial at its last counted open
    serial: int = 0  # bumps on every change that is not a menu: the world state she is in
    resets: int = 0
    honoured: float = 0.0
    wait_from: float | None = None
    wait_until: float | None = None
    wait_why: str | None = None
    last_row: float | None = None
    held: float = 0.0
    held_by: dict = field(default_factory=dict)
    seen_at: float | None = None
    program_seen: int = 0
    wait_seen: object = None
    ladder_origin: float = 0.0
    ladder_honoured: float = 0.0

    def age(self, now) -> float:
        return max(0.0, now - self.since)

    def honoured_now(self, now) -> float:
        live = 0.0
        if self.wait_from is not None and self.wait_until is not None:
            live = max(0.0, min(now, self.wait_until) - self.wait_from)
        return min(WAIT_HONOUR_MAX_S, self.honoured + live)

    def ladder_age(self, now) -> float:
        """Seconds the ladder may count: the age without the seconds her own wait/watch choice is honoured."""
        return max(0.0, self.age(now) - self.ladder_origin
                   - max(0.0, self.honoured_now(now) - self.ladder_honoured))

    def restart_ladder(self, now):
        """Start a fresh climb without crediting a game effect or changing the world serial."""
        self.ladder_origin = self.age(now)
        self.ladder_honoured = self.honoured_now(now)

    def waiting(self, now) -> bool:
        return self.wait_until is not None and now < self.wait_until and self.honoured_now(now) < WAIT_HONOUR_MAX_S

    def reset(self, now, cause, *, menu=False):
        self.since, self.reset_by, self.since_wall = now, str(cause), time.time()
        self.resets += 1
        self.honoured, self.wait_from, self.wait_until, self.wait_why = 0.0, None, None, None
        self.ladder_origin = self.ladder_honoured = 0.0
        if not menu:
            self.serial += 1
        return self.reset_by

    def hold(self, seconds, why="machine_stall"):
        """Seconds that are not hers to change anything: a machine stall (ADR 128) or a native bed wait (ADR 154)."""
        if seconds > 0:
            self.since += seconds
            self.held += seconds
            self.held_by[why] = round(self.held_by.get(why, 0.0) + seconds, 3)

    def seen(self, now, *, hold_why=None):
        """One boundary seen; with `hold_why`, the seconds since the last one are held (her bed wait)."""
        if hold_why and self.seen_at is not None:
            self.hold(min(max(0.0, now - self.seen_at), self.age(now)), hold_why)
        self.seen_at = now

    def honour_wait(self, now, why="wait"):
        """Her own choice to wait or to watch: the ladder holds up to WAIT_HONOUR_S from now (bounded per stretch)."""
        if self.wait_from is not None and self.wait_until is not None:
            self.honoured += max(0.0, min(now, self.wait_until) - self.wait_from)
        self.wait_from, self.wait_until, self.wait_why = now, now + WAIT_HONOUR_S, str(why)

    def note(self, now, cause, *, program=False):
        """A journal-row effect (`effect_of`). A change the program made (a recovery or a rung) is no effect of hers."""
        if not cause or program:
            return None
        return self.reset(now, cause)

    def observe(self, now, *, evidence=None, menu=None, menu_known=True, date=None, program=False):
        """One boundary's game reading. Returns what reset the clock, or None. The first reading is a baseline."""
        cause = None
        if date is not None:
            if self.date is not None and tuple(date) != tuple(self.date):
                cause = "day"
            self.date = tuple(date)
        if evidence is not None:
            if cause is None and self.evidence is not None:
                cause = _world_diff(self.evidence, evidence)
            self.evidence = {k: (v if v is not None or self.evidence is None else self.evidence.get(k))
                             for k, v in evidence.items()}
        if cause is not None and not program:
            self.reset(now, cause)
        elif cause is not None:
            self.serial += 1  # her world moved under a rung: a later reopened menu is a new reading
        menu_cause = self._menu(now, menu, program) if menu_known else None
        return cause if cause is not None and not program else menu_cause

    def _menu(self, now, menu, program):
        if not self.menu_seen:
            self.menu_seen, self.menu = True, menu
            return None
        if menu == self.menu:
            return None
        before, self.menu = self.menu, menu
        if program:
            self.menu_counted = False
            return None
        if menu is not None:
            # A menu she opens counts once per world state: reopening the same menu with nothing else changed is a re-read.
            if self.menu_world.get(menu) == self.serial:
                self.menu_counted = False
                return None
            self.menu_world[menu] = self.serial
            self.menu_counted = True
            return self.reset(now, "menu_open:" + str(menu), menu=True)
        if before is not None and self.menu_counted:
            self.menu_counted = False
            return self.reset(now, "menu_close:" + str(before), menu=True)
        return None

    def row(self, now, *, kind, rung=None, **extra):
        self.last_row = now
        out = dict(ev="live7_effect_clock", at=time.time(), kind=kind, age_s=round(self.age(now), 3),
                   ladder_age_s=round(self.ladder_age(now), 3), reset_by=self.reset_by,
                   since_at=round(self.since_wall, 3), resets=self.resets)
        if self.honoured_now(now):
            out["honoured_s"] = round(self.honoured_now(now), 3)
            out["wait_why"] = self.wait_why
        if self.held:
            out["held_s"] = round(self.held, 3)
            out["held_by"] = dict(self.held_by)
        if rung is not None:
            out["rung"] = rung
        out.update(extra)
        return out

    def row_due(self, now) -> bool:
        return self.last_row is None or now - self.last_row >= ROW_INTERVAL_S


def status_text(row) -> str | None:
    """The operator's and the supervisor's status word for a clock row: `effect=42s` (and `rung=2` once climbing)."""
    if not isinstance(row, Mapping) or type(row.get("age_s")) not in (int, float):
        return None
    text = "effect=%ds" % int(row["age_s"])
    if type(row.get("rung")) is int and row["rung"] > 0:
        text += " rung=%d" % row["rung"]
    return text


def replay(rows, *, bounds=(60, 120, 300)):
    """Archive replay of the clock over journal rows (read-only): the stretches without an effect of hers.

    Rows from before this clock existed have no `live7_effect_clock` row; native_vitals give the map, the day and
    the menu, the rest is `effect_of`. Wall time is each row's own `at`, else the last one seen. Returns the
    stretches (seconds, start, end, what ended it) and, per bound, when the clock would first have passed it.
    """
    t = since = None
    menu = date = place = None
    serial, menu_world, counted = 0, {}, False
    stretches, passed = [], {b: [] for b in bounds}
    open_stretch = {}

    def end(at, cause):
        nonlocal since
        if since is not None and at is not None:
            stretches.append(dict(seconds=round(at - since, 1), start=since, end=at, ended_by=cause))
            for b in bounds:
                if at - since >= b:
                    passed[b].append(since + b)
        since = at

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        at = row.get("at")
        if type(at) in (int, float) and at > 1e9:
            t = at
        if t is None:
            continue
        if since is None:
            since = t
        cause = None
        if row.get("ev") == "native_vitals":
            game = row.get("game") if isinstance(row.get("game"), Mapping) else {}
            if place is not None and game.get("location") != place:
                cause = "map"
            elif date is not None and game.get("date") != date:
                cause = "day"
            place, date = game.get("location"), game.get("date")
            if cause:
                serial += 1
            now_menu = row.get("active_menu")
            if now_menu != menu and "active_menu" in row:
                before, menu = menu, now_menu
                if now_menu is not None and menu_world.get(now_menu) != serial:
                    menu_world[now_menu], counted = serial, True
                    cause = cause or "menu_open:" + str(now_menu)
                elif now_menu is None and before is not None and counted:
                    counted = False
                    cause = cause or "menu_close:" + str(before)
                else:
                    counted = False
        else:
            cause = effect_of(row)
            if cause and not cause.startswith("her_press"):
                serial += 1
        if cause:
            end(t, cause)
    if t is not None and since is not None:
        open_stretch = dict(seconds=round(t - since, 1), start=since, end=t, ended_by="<end of journal>")
        stretches.append(open_stretch)
        for b in bounds:
            if t - since >= b:
                passed[b].append(since + b)
    return stretches, passed


def read_rows(paths):
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def soak_marks(rows, rescues=0):
    """ADR 241 proof marks of an unattended soak (bench 17): the longest stretch without an effect of hers, the rungs
    taken by number, the sessions the program ended, the hand rescues (the operator's count, e.g. rescues.tsv)."""
    rows = list(rows)
    stretches, _ = replay(rows)
    rungs = {}
    ended = 0
    for row in rows:
        if row.get("ev") == "live7_effect_ladder" and row.get("kind") == "rung" and row.get("result") != "not_applicable":
            rungs[row.get("rung")] = rungs.get(row.get("rung"), 0) + 1
        if row.get("ev") == "live7_no_progress" and row.get("outcome_class") == "SAFETY_STOP":
            ended += 1  # one per session the no-progress rule ended (its skill_session_finished follows it)
    return dict(longest_stretch_without_effect_s=max((s["seconds"] for s in stretches), default=0.0),
                rungs_taken=dict(sorted(rungs.items())), sessions_ended_by_the_program=ended, hand_rescues=int(rescues))


if __name__ == "__main__":  # python -m arti_runtime.effect_clock <g1_journal.jsonl ...> [--rescues N]
    import sys
    args = sys.argv[1:]
    count = int(args[args.index("--rescues") + 1]) if "--rescues" in args else 0
    paths = [a for i, a in enumerate(args) if a != "--rescues" and (i == 0 or args[i - 1] != "--rescues")]
    print(json.dumps(soak_marks(read_rows(paths), count), indent=1))
