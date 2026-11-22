"""ADR 239: a failed way out of a place is bound to the facts of its failure.

One primitive for every exit kind: a Mines ladder up, and a trip through a door,
a warp or a map edge. The memory of a failed try closes that way out only while
she, the world and the game clock are exactly as they were when it failed. A
change in any of them makes the next try a real try. Nothing here sends input or
grants permission: the exit's own native checks still decide, and name their
reason when they refuse.

Program recovery (a retry the program starts by itself) keeps its own bounds.
A way out she chose is never refused from this memory with zero inputs.
"""
from __future__ import annotations

from collections.abc import Mapping


def bind(*, place, clock, tile=None, world=None, reason=None) -> dict:
    """The facts a failed way out is bound to: the place, the game clock, her tile and the world reading."""
    return dict(place=place, clock=clock, tile=_tile(tile), world=world, reason=reason)


def holds(bound, *, place, clock, tile=None, world=None) -> bool:
    """True only while every fact of the failure is unchanged.

    The clock is compared as read; an unknown clock equals only an unknown clock
    (nothing shows time moved). A bound tile or world reading must match the
    current one; a current reading that is missing cannot match a known one.
    """
    if not isinstance(bound, Mapping) or bound.get("place") != place:
        return False
    if bound.get("clock") != clock:
        return False
    if bound.get("tile") is not None and _tile(tile) != bound["tile"]:
        return False
    if bound.get("world") is not None and world != bound["world"]:
        return False
    return True


def said_clock(clock) -> str:
    """The game time as her HUD shows it, as ' jam 17:20'; empty when the clock was not read."""
    if type(clock) is not int:
        return ""
    return " jam %02d:%02d" % (clock // 100, clock % 100)


def _tile(tile):
    if isinstance(tile, (list, tuple)) and len(tile) == 2 and all(type(v) is int for v in tile):
        return tuple(tile)
    return None
