"""Live7 WP4: additive source tags for the campaign journal and the provider archive.

Tags are evidence only, never authority. They add new keys (``domain``, ``cycle``,
``day_trial`` and, on boundary events, ``game``) and never rename, remove or
overwrite a key the record already carries.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

DOMAIN = "stardew"

# Boundary events (PLAN section 3.1): skill start/end, row results, transitions, sleep.
BOUNDARY_EVENTS = frozenset({
    "skill_session_started", "skill_session_finished", "skill_chosen", "skill_started", "skill_leg",
    "skill_leg_started", "skill_finished", "skill_failed", "skill_skipped",
    "location_changed", "unexpected_location_change", "day_start_detected", "day_start_cleared",
    "sleep_home_leg",
})
_BOUNDARY_PART = re.compile(r"part_[a-z]+_(?:row|started|preface|sleep_reserve)")


def tag_value(value: Any) -> int | str | None:
    """A cycle/day-trial label: a non-bool int or a non-empty string; anything else is null."""
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def source_tags(cycle: Any = None, day_trial: Any = None) -> dict:
    return {"domain": DOMAIN, "cycle": tag_value(cycle), "day_trial": tag_value(day_trial)}


def grant_tags(grant: Mapping | None) -> dict:
    """Tags of one approved trial: the optional approval keys ``cycle`` / ``day_trial`` (null outside Live7)."""
    grant = grant if isinstance(grant, Mapping) else {}
    return source_tags(grant.get("cycle"), grant.get("day_trial"))


def is_boundary(ev: Any) -> bool:
    return isinstance(ev, str) and (ev in BOUNDARY_EVENTS or _BOUNDARY_PART.fullmatch(ev) is not None)


def game_stamp(snapshot: Any) -> dict:
    """``{date, time, location}`` of the latest motor snapshot; unknown parts are null (never guessed)."""
    year = getattr(snapshot, "year", None)
    season = getattr(snapshot, "season", None)
    day = getattr(snapshot, "day", None)
    date = ([year, season, day] if type(year) is int and year >= 1
            and season in ("spring", "summer", "fall", "winter") and type(day) is int and 1 <= day <= 28 else None)
    clock = getattr(snapshot, "time_of_day", None)
    location = getattr(snapshot, "location", None)
    return {"date": date, "time": clock if type(clock) is int else None,
            "location": location if isinstance(location, str) else None}


def tagged(record: Mapping, tags: Mapping, game: Mapping | None = None) -> dict:
    """A NEW dict: the record's keys first and unchanged, then any missing tag keys."""
    out = dict(record)
    for key, value in tags.items():
        out.setdefault(key, value)
    if game is not None:
        out.setdefault("game", dict(game))
    return out
