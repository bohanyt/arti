"""ADR 241 B: one ladder of generic actions above every skill, climbed on the effect clock (ADR 241 A).

When the clock's ladder age passes a rung's bound, the next rung that applies acts once, its effect is read back
from the game, one dated row goes to her ONE record and her next page says so once. Nothing here is per place,
per menu or per skill. A choice of hers that changes the game resets the clock and ends the climb; a rung's own
change never resets the clock (it is not hers), it only holds the next rung STEP_S so she gets a page.

Rungs, in order, each skipped when it does not apply:
1. A menu is up: the game's own close (ADR 237); a question: the answer the game itself offers for leaving or
   declining (native role ``decline``). A passive event: watched (the clock honours it). A minigame: no way out the
   program can press; said, and the climb goes on.
2. No menu: step a few tiles to a free tile her page already offers a walk to; her next page withholds the choice
   she last repeated, with its reason.
3. Leave the place by a way out the game offers (ADR 239 binds a failed one), nearest known first.
4. Go home by the ordinary trip.
5. End this attempt cleanly; the cycle drains it and the supervisor starts a fresh cycle in the same game.
6. (supervisor) Relaunch her game from the last saved morning; a human only after that failed.

Bounds, from the two archives (ADR 241 B): healthy stretches without an effect have median 2.4 s, p90 7.4 s and max
62.9 s (10 days, 1919 effects); the longest healthy menu sitting was 29.2 s. Rung 1 keeps ADR 237's 60 s; rung 2 is
about twice the longest healthy stretch; each later rung one STEP_S on; the climb from rung 1 to a fresh cycle is 4
minutes of ladder age.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

BOUND_S = {1: 60.0, 2: 120.0, 3: 150.0, 4: 180.0, 5: 240.0}
STEP_S = 30.0  # after a rung acted, the next one waits at least this long for her page and her choice
RUN_MAX_S = 120.0  # a rung's trip still running this long is read back anyway, so a stuck trip never stalls the climb
NAMES = {1: "menu_close", 2: "step_away", 3: "leave_place", 4: "go_home", 5: "fresh_cycle", 6: "relaunch"}
STEP_TILES = (2, 8)  # a step away: a walk her page offers to a tile this many tiles from her (Manhattan)
MINIGAME = "minigame"


@dataclass
class Ladder:
    resets_seen: int = 0
    taken: list = field(default_factory=list)  # one dict per rung that acted or was judged this stretch
    done: set = field(default_factory=set)  # rungs already judged (acted, stood aside or skipped) this stretch
    acted_at: float | None = None
    acting: dict | None = None  # the rung whose action is still running (its call, its start evidence)
    told: str | None = None  # the line her next page carries once
    withheld: tuple | None = None  # (name, arg, reason) withheld from her next page by rung 2

    def restart(self, resets):
        """Her effect: the climb is over."""
        self.resets_seen = resets
        self.taken, self.done = [], set()
        self.acted_at, self.acting, self.withheld = None, None, None

    def next_rung(self, age, now):
        """The lowest rung not judged yet whose bound has passed, unless a rung acted under STEP_S ago."""
        if self.acting is not None or (self.acted_at is not None and now - self.acted_at < STEP_S):
            return None
        for rung in sorted(BOUND_S):
            if rung not in self.done and age >= BOUND_S[rung]:
                return rung
        return None

    def pass_on(self, age=None):
        """fix144A C9/C10 (ADR 249): a rung that cannot apply for a stated cause (the same call is stalling, a HALT
        holds her hands) passes on at once to the lowest rung not judged yet. A fresh cycle (rung 5) keeps its own
        bound when `age` is given (review R1b: a skipped stalling call never drains an armed world early)."""
        for rung in sorted(BOUND_S):
            if rung in self.done:
                continue
            return None if rung == 5 and age is not None and age < BOUND_S[5] else rung
        return None

    def judge(self, rung, now, *, result, read_back=None, acted=False, **extra):
        row = dict(rung=rung, name=NAMES[rung], result=result, **({"read_back": read_back} if read_back else {}), **extra)
        self.done.add(rung)
        self.taken.append(row)
        if acted:
            self.acted_at = now
        return row

    def summary(self):
        return [dict(rung=r["rung"], name=r["name"], result=r["result"], read_back=r.get("read_back"))
                for r in self.taken if r["result"] != "not_applicable"]


def leaving_answer(view, actions):
    """Rung 1 for a question: the answer the game itself offers for leaving or declining, by native role, generic.

    Native publishes a question's answers with roles (``answer``, ``decline``; UiAffordances.Role). The first
    published ``decline`` the page offers is the leaving answer. A question with no declining answer has none; the
    rung says so and the climb goes on."""
    from arti_stardew_generic_ui import _entry_actions, published
    for entry in (published(view) or {}).get("list", ()):
        if entry.get("role") != "decline":
            continue
        for key, _ in _entry_actions(view, entry):
            if key in actions:
                return key
    return None


def menu_kind(view, wait_reason=None, *, minigame=False):
    """What kind of hold a menu up is, for rung 1: ``closable``, ``question``, ``minigame`` or ``none``.

    ADR 243: a minigame (``minigame=True``, raw hands on) whose native view publishes the game's own way out (B:
    the minigame's forceQuit, the same abort path as the game's; LB: Escape) is ``closable``; without one it stays
    ``minigame``, said and the climb goes on."""
    from arti_stardew_generic_ui import exit_action, controls, published, game_close
    if minigame or str(wait_reason or "").startswith(MINIGAME) or getattr(view, "menu_type", None) == MINIGAME:
        if minigame and view is not None and game_close(view) is not None:
            return "closable"
        return MINIGAME
    if view is None:
        return "none"
    listed = (published(view) or {}).get("list", ())
    if any(e.get("role") in ("answer", "decline") for e in listed):
        return "question"
    return "closable" if exit_action(view, controls(view)) is not None else "none"


def step_target(calls, here, *, withheld=None):
    """Rung 2: an offered walk to a free tile STEP_TILES away from her, nearest first; None when none is offered."""
    if not isinstance(here, (list, tuple)) or len(here) != 2:
        return None
    best = None
    for call in calls:
        tile = getattr(call, "tile", None)
        if getattr(call, "name", None) != "jalan_ke" or not isinstance(tile, (list, tuple)) or len(tile) != 2:
            continue
        if withheld and (call.name, call.arg) == tuple(withheld[:2]):
            continue
        d = abs(tile[0] - here[0]) + abs(tile[1] - here[1])
        if STEP_TILES[0] <= d <= STEP_TILES[1] and (best is None or (d, tuple(tile)) < best[0]):
            best = ((d, tuple(tile)), call)
    return best[1] if best else None


EXIT_KINDS = frozenset({"door", "building_door", "warp"})  # what the scene names as a way out (the approach offer's)
OPEN_CELLS = ("." , "x")  # extras.nav_grid: an open tile, a warp source (the way out itself)
STEP_DELTA = {"up": (0, -1), "right": (1, 0), "down": (0, 1), "left": (-1, 0)}  # ADR 243 `langkah`; y grows down


def _xy(value):
    if isinstance(value, Mapping) and type(value.get("x")) is int and type(value.get("y")) is int:
        return value["x"], value["y"]
    return None


def scene_exits(targets):
    """fix144A C11 (ADR 249): the ways out the scene names at this moment, as (ref, tile, target place), read from the
    same scene targets the watchdog reads, in the shape the mod publishes them (review R1 B4;
    `stardew-mod/ArtiStardewBridge/Protocol/SceneFacts.cs`): a warp's tile is its `source_tile`, a door's and a
    building door's is its `tile`; a villager's own warp (`npc_only`) and a door the game says is not enterable
    (`enterable: false`) are not ways out."""
    out = []
    for ref, f in (targets or {}).items() if isinstance(targets, Mapping) else ():
        if (not isinstance(f, Mapping) or f.get("kind") not in EXIT_KINDS or f.get("npc_only") is True
                or f.get("enterable") is False):
            continue
        tile = _xy(f.get("source_tile") if f.get("kind") == "warp" else f.get("tile"))
        if tile is not None:
            out.append((str(ref), tile, f.get("target_location")))
    return out


def home_place(target) -> bool:
    """A way out that leads home: the Farm itself, the farmhouse or a cabin (never FarmCave; review R1b)."""
    target = str(target or "")
    return target == "Farm" or target.startswith(("FarmHouse", "Cabin"))


def toward(calls, here, exits, grid=None):
    """C11: her raw step (`langkah`, offered on her page) one tile closer to a named exit, onto a tile the native
    walkability grid says is open; the nearest exit first, then the next (review R1b: never a step into a wall).
    Without a grid for this map no raw step is chosen: the climb takes the named way instead."""
    if not exits or not isinstance(here, (list, tuple)) or len(here) != 2 or not isinstance(grid, Mapping):
        return None
    offered = {c.arg: c for c in calls if getattr(c, "name", None) == "langkah" and c.arg in STEP_DELTA}
    if not offered:
        return None
    for _, tile, _ in sorted(exits, key=lambda e: (abs(e[1][0] - here[0]) + abs(e[1][1] - here[1]), e[1])):
        dx, dy = tile[0] - here[0], tile[1] - here[1]
        for d in sorted(STEP_DELTA, key=lambda d: -(STEP_DELTA[d][0] * dx + STEP_DELTA[d][1] * dy)):
            step = (here[0] + STEP_DELTA[d][0], here[1] + STEP_DELTA[d][1])
            if STEP_DELTA[d][0] * dx + STEP_DELTA[d][1] * dy > 0 and d in offered and grid.get(step) in OPEN_CELLS:
                return offered[d]
    return None


def way_out(calls, location, *, trips=None, withheld=None, exits=(), here=None, grid=None):
    """Rung 3: a way out of this place, what the game offers at this moment before a named skill (fix144A C11):
    a map exit the scene publishes on her page (`aksi` on a way out), then her raw step toward an exit the scene
    names, then a trip skill to another map (`pergi_ke`), nearest measured trip first.

    `trips` are measured trip seconds by (from, to) when the runtime has them; unknown trips keep the page order.
    A trip home is rung 4's, unless it is the only way out."""
    from arti_stardew_live7 import home_call, navigation_exit
    out = []
    for order, call in enumerate(calls):
        if withheld and (call.name, call.arg) == tuple(withheld[:2]):
            continue
        scene = call.name == "aksi" and navigation_exit(call.arg)
        if scene or (call.name == "pergi_ke" and call.arg not in (None, location)):
            seconds = (trips or {}).get((location, call.arg)) if call.name == "pergi_ke" else None
            out.append((home_call(call), 0 if scene else 2, seconds if type(seconds) in (int, float) else float("inf"),
                        order, call))
    step = toward(calls, here, exits, grid)
    if step is not None and not (withheld and (step.name, step.arg) == tuple(withheld[:2])):
        out.append((False, 1, float("inf"), -1, step))
    out.sort(key=lambda row: row[:4])
    return out[0][4] if out else None


def home_trip(calls, location, *, exits=(), here=None, grid=None):
    """Rung 4: go home. What the game offers first (C11): the scene's own way out toward home on her page, then her
    raw step toward it; then the ordinary trip home. None at home or when nothing of that is offered."""
    from arti_stardew_live7 import home_call
    if location in (None, "Cabin", "FarmHouse") or str(location).startswith("Cabin"):
        return None
    homeward = [e for e in exits or () if home_place(e[2])]
    refs = {e[0] for e in homeward}
    scene = next((c for c in calls if c.name == "aksi" and c.arg in refs), None)
    return (scene or toward(calls, here, homeward, grid)
            or next((c for c in calls if home_call(c) and c.name == "pergi_ke"), None))


def her_line(rung, *, date, clock, seconds, place=None, did=None, result=None, withheld=None):
    """The one dated line in her ONE record and on her next page, in plain words of the game."""
    from arti_runtime.menu_recovery import _clock, _date_label
    where = (" di %s" % place) if place else ""
    head = "%s %s Selama %d detik tidak ada yang berubah dari tindakanku%s" % (_date_label(date), _clock(clock),
                                                                                int(seconds), where)
    body = {1: "; menu yang terbuka ditutup atau dijawab dengan jalan keluar yang ditawarkan game",
            2: ("; aku melangkah ke %s lalu membaca ulang sekitar" % did) if did else "; aku tidak mengulang pilihan yang sama",
            3: "; aku keluar lewat %s" % (did or "jalan keluar terdekat"),
            4: "; aku pulang ke rumah",
            5: "; permainanku dimulai ulang dari keadaan sekarang di game yang sama"}[rung]
    text = head + body + "."
    if result:
        text += " " + result
    if withheld:
        text += " Pilihan %s kutahan dulu: %s." % (withheld[0] + (" " + str(withheld[1]) if withheld[1] else ""),
                                                   withheld[2])
    return text


def read_back(before, after):
    """What the game shows changed while a rung acted: map, tile, items, money; None when nothing did."""
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    for key, word in (("location", "map"), ("tile", "tile"), ("inventory", "items"), ("money", "money")):
        if after.get(key) is not None and before.get(key) != after.get(key):
            return word
    return None
