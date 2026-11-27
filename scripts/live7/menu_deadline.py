"""Bounded in-menu decision deadline (ADR 43). Pure bookkeeping; no input, no game I/O.

a25/a26/a27 each stopped on the 600 s no-progress watchdog inside a native menu. Every failed boundary (no fresh
view, a decision superseded at its start fence, a render failure, a provider error) returned a "safe pause" and the
same boundary was retried. The only escalations needed the very preconditions that had failed. This ladder runs from
the first menu boundary without an admitted decision: re-acquire native state, then settle a held cursor item, then
the menu's own exit, each a named recovery FAULT. A model choice that is admitted resets it; nothing else does.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SETTING = "stardew_live7_menu_visit_bound"


def enabled(config):
    return (config or {}).get(SETTING, False) is True


# fix137A (ADR 237 B): the close bound. mabar 2026-11-18 c1 d2: a QuestLog page with no decision she could make held her
# 600 s and the no-progress watchdog stopped her game. Measured from both runs of that night (native_vitals sittings):
# the longest healthy menu sitting was 29.2 s (GameMenu, 21:38 run d7), every bed ReadyCheck ~21 s (exempt as a bed wait);
# one menu decision took 1.7-10 s, the slowest 26.6 s, under the provider's 30 s logical timeout. 60 s is twice the longest
# healthy sitting and two whole provider timeouts: a menu with no admitted decision for 60 s is closed by the game's own close.
REACQUIRE_S = 30.0
SETTLE_S = 45.0
EXIT_S = 60.0
CYCLE_S = 60.0  # after an exhausted ladder, the next re-acquire; first close at 60 s, repeats every 60 s, all < 600 s
STAGES = ("reacquire", "settle", "exit")
_BOUNDS = dict(reacquire=REACQUIRE_S, settle=SETTLE_S, exit=EXIT_S)

# Settle targets that keep the held item hers. Swap moves another item to the cursor; drop/trash are her choices.
SETTLE_KINDS = ("inventory", "container")


@dataclass
class MenuDeadline:
    since: float
    menu: str | None
    cycle: int = 0
    done: set = field(default_factory=set)
    causes: dict = field(default_factory=dict)
    opened: float | None = None  # first boundary of this episode; `since` moves only when a settle defers the exit
    receiver: str | None = None
    rung_deferred: bool = False

    def __post_init__(self):
        if self.opened is None:
            self.opened = self.since

    def note(self, cause):
        self.causes[str(cause)] = self.causes.get(str(cause), 0) + 1

    def due(self, now) -> list:
        """Stages whose bound has passed this cycle and not yet run, in ladder order."""
        age = now - self.since - self.cycle * CYCLE_S
        return [s for s in STAGES if age >= _BOUNDS[s] and s not in self.done]

    def ran(self, stage):
        self.done.add(stage)
        if stage == "exit":  # exhausted or closed: the next cycle re-acquires again from scratch
            self.cycle += 1
            self.done = set()

    def defer_exit(self, now, seconds=REACQUIRE_S):
        """After a controller settle she gets a decision with the exit on offer before any controller close."""
        self.since = max(self.since, now + seconds - EXIT_S - self.cycle * CYCLE_S)

    def elapsed(self, now):
        return round(now - self.opened, 3)

    def can_act(self, now, boundary_s):
        """Give the first exit stage one more boundary to run before recovery takes over (counted from the start that
        `defer_exit` moved). Once an exit stage ran (closed, refused or unavailable) recovery judges."""
        return self.cycle == 0 and now - self.since < EXIT_S + boundary_s


def observe(slot, session_id, menu, now, *, receiver=None):
    """The deadline for this session's open menu; a different menu (or none) starts a new episode."""
    if menu is None:
        return None
    if (slot is None or slot[0] != session_id or slot[1].menu != menu
            or receiver is not None and slot[1].receiver != receiver):
        return session_id, MenuDeadline(since=now, menu=menu, receiver=receiver)
    return slot


def settle_target(view, actions):
    """The published settle that returns a held cursor item to her bag or this menu's storage, else None."""
    block = getattr(view, "cursor_settle", None) or {}
    cursor = block.get("cursor")
    if not isinstance(cursor, dict) or not cursor.get("item"):
        return None
    for kind in SETTLE_KINDS:
        for target in block.get("targets", ()):
            key = "settle:%s" % target.get("id")
            if target.get("kind") == kind and not target.get("refused") and key in actions:
                return key
    return None


def held_item(view):
    cursor = (getattr(view, "cursor_settle", None) or {}).get("cursor")
    return cursor.get("item") if isinstance(cursor, dict) else None
