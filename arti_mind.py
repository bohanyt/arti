"""ARTI's mind layer (slice 1): her preferences, journey goals and the current intent line.

Design: runbooks/MIND_LAYER_DESIGN_2026-10-28.md (owner approved, D1 + D2).
Game-agnostic: no game import, no game words. The game glue passes a day ordinal, a day label,
relevance tags (offered verbs/args/place) and the verified rows; everything else is generic.

Code writes facts; ARTI writes intent. Every goal and why here is authored by her model output, or
pinned from the soul/owner. Code never creates, ranks, orders or expires a goal; it only caps (a logged,
visible safety cap), checks citations and tags relevance. A goal she wrote lasts until native evidence
closes it or she drops it (FIX75C, ADR 133); its age and last cited progress ride beside it as facts.

Files (``arti-state/mind/``, gitignored):
  * ``prefs.jsonl`` pinned trait rows ``{v, id, scope, text, about, polarity, source, pinned, strength}``.
  * ``<game>/goals.jsonl`` goal rows ``{v, id, horizon, text, status, set_on_day, for_day, ...}``.
  * ``<game>/likes.jsonl`` her dated fun / not_fun rows from reflection (``arti.mind_like/1``), bounded.
  * ``<game>/journey.jsonl`` one row per game day (``arti.mind_journey/1``).
  * ``now.json`` the latest decision intent (``arti.mind_now/1``), overwritten per decision.
"""
from __future__ import annotations

from arti_runtime import ipc
from arti_runtime import plan_dates
import contextlib
from functools import wraps
import json
import os
import re
import tempfile
import time
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

import arti_state

MIND_VERSION = 1
NOW_SCHEMA = "arti.mind_now/1"
JOURNEY_SCHEMA = "arti.mind_journey/1"
PREFS_FILE = "prefs.jsonl"
GOALS_FILE = "goals.jsonl"
JOURNEY_FILE = "journey.jsonl"
NOW_FILE = "now.json"

BLOCK_MAX_BYTES = 600  # design cap; includes the immutable date facts (ADR 36)
WHY_MAX_CHARS = 120
GOAL_TEXT_MAX = 100
PREF_TEXT_MAX = 160  # ADR 186: a complete owner-authored first-person preference fits without paraphrasing
HORIZONS = ("today", "week", "long")  # design 2.2 caps 3/3/3; FIX75C: no horizon has a TTL
SEASON_HORIZONS = (*HORIZONS, "season")  # ADR 221: opt-in planning; legacy callers keep their exact contract
GOAL_CAPS = {"today": 3, "week": 3, "long": 3}
GOAL_HISTORY_MAX = 120        # closed rows kept on disk (open rows are never trimmed)
GOALS_FILE_MAX_BYTES = 96_000  # safety cap for the goals file; oldest closed rows go first, logged
LAST_PROGRESS_CHARS = 60
LIKES_FILE = "likes.jsonl"
LIKE_SCHEMA = "arti.mind_like/1"
LIKES_MAX = 60
LIKES_MAX_BYTES = 16_000
LIKES_SHOWN = 3               # per polarity, at bedtime
LIKE_LINE_CHARS = 70
TRAIT_LINES = 3
GAME_PREF_LINES = 2
PREFS_MAX = 12
NOW_MAX_AGE_SEC = 900.0
NOW_LINE_MAX_CHARS = 320


# Example traits (placeholder text): code defaults; a prefs.jsonl row with the same id wins.
OWNER_PREFS = (
    {"v": MIND_VERSION, "id": "example-trait-a", "scope": "any",
     "text": "aku punya sifat contoh A dari pemilik",
     "about": ["contoh"], "polarity": "like", "source": "owner", "pinned": True, "strength": 5},
    {"v": MIND_VERSION, "id": "example-trait-b", "scope": "stardew",
     "text": "aku punya sifat contoh B dari pemilik",
     "about": ["contoh"],
     "polarity": "like", "source": "owner", "pinned": True, "strength": 5},
    {"v": MIND_VERSION, "id": "example-trait-c", "scope": "any",
     "text": "aku punya sifat contoh C dari pemilik",
     "about": ["contoh"], "polarity": "like", "source": "owner", "pinned": True, "strength": 4},
    {"v": MIND_VERSION, "id": "example-trait-d", "scope": "any",
     "text": "aku punya sifat contoh D dari pemilik",
     "about": ["contoh"], "polarity": "like", "source": "owner", "pinned": True, "strength": 5},
    {"v": MIND_VERSION, "id": "example-trait-e", "scope": "stardew",
     "text": "aku punya sifat contoh E dari pemilik",
     "about": ["contoh"], "polarity": "like", "source": "owner", "pinned": True, "strength": 5},
    {"v": MIND_VERSION, "id": "example-trait-f", "scope": "stardew",
     "text": "aku punya sifat contoh F dari pemilik",
     "about": ["contoh"],
     "polarity": "like", "source": "owner", "pinned": True, "strength": 5},
)

# FIX90B: boundary tags that name the moment, beside the offered calls/args/place. Plain words, never an order.
REFLECTION_TAGS = ("besok",)   # the bedtime reflection plans tomorrow
CARRIED_TAG = "dibawa"         # a carried today goal is on this page (ADR 155 fact line)


def moment_tags(time_of_day: Any) -> list[str]:
    """The part of her day for a game clock (HHMM, 600-2600), as one Indonesian word; [] when unknown."""
    if type(time_of_day) is not int:
        return []
    return ["pagi" if time_of_day < 1100 else "siang" if time_of_day < 1500 else "sore" if time_of_day < 1800 else "malam"]

# Banned-steering lint (design §4 acceptance 4). Phrases match anywhere; words match whole words only,
# so a fact like "tidak mengharuskan" is not a hit.
BANNED_PHRASES = ("utamakan", "prioritas", "pilih satu", "progress_rule", "next_choice")
BANNED_WORDS = ("harus", "sebaiknya", "should", "must")
_BANNED_WORD_RE = re.compile(r"(?<![\w])(" + "|".join(BANNED_WORDS) + r")(?![\w])", re.IGNORECASE)


def steering_hits(text: str) -> list[str]:
    """Banned steering phrases/words found in ``text`` (case-insensitive), in order of the lists."""
    low = str(text or "").casefold()
    hits = [p for p in BANNED_PHRASES if p in low]
    hits.extend(m.group(1).casefold() for m in _BANNED_WORD_RE.finditer(low))
    return hits


# --------------------------------------------------------------------------- paths and small I/O

def mind_dir(config: Mapping | None = None, root: Path | None = None) -> Path:
    """``arti_mind_dir`` (absolute or repo-relative), else env ``ARTI_MIND_DIR``, else ``<arti_state_dir>/mind``."""
    cfg = dict(config or {})
    raw = cfg.get("arti_mind_dir") or os.environ.get("ARTI_MIND_DIR")
    if raw:
        path = Path(str(raw))
        return path if path.is_absolute() else Path(root or arti_state._ROOT) / path
    return arti_state.state_dir(cfg, root) / "mind"


def game_dir(base: Path, game: str) -> Path:
    slug = arti_state.slug(game, 24)
    if not slug:
        raise ValueError("mind_game_slug")
    return Path(base) / slug


def _write_json_atomic(path: Path, value: Any) -> None:
    ipc.write_json(path, value, critical=path.name != NOW_FILE, fsync=True)


def text_reason(text: Any, max_len: int) -> str:
    """'' if ARTI-authored ``text`` may be stored/rendered, else why not. Never edits the text."""
    if not isinstance(text, str) or not text.strip():
        return "empty"
    if len(text) > max_len:
        return "too_long"
    if "\n" in text or "\r" in text:
        return "multiline"
    hard = arti_state.arti_curator.hard_junk_reason(text)
    if hard:
        return hard
    if arti_state._URL_RE.search(text):
        return "url"
    if arti_state._MENTION_RE.search(text):
        return "mention"
    if "[" in text or "]" in text or "{" in text:
        return "log_like"
    return ""


# --------------------------------------------------------------------------- preferences

def valid_pref(row: Any) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("id"), str) and bool(row["id"])
            and isinstance(row.get("scope"), str) and bool(row["scope"])
            and not text_reason(row.get("text"), PREF_TEXT_MAX)
            and row.get("polarity", "like") in ("like", "dislike")
            and row.get("source") in ("soul", "owner", "reflection")
            and row.get("status", "ok") == "ok"
            and isinstance(row.get("about", []), list))


def load_prefs(base: Path) -> list[dict]:
    """Valid pref rows (file order), owner defaults first unless the file overrides their id. Never raises."""
    try:
        rows = arti_state.read_jsonl(Path(base) / PREFS_FILE)
    except OSError:
        rows = []
    ids = {r.get("id") for r in rows}
    merged = [dict(r) for r in OWNER_PREFS if r["id"] not in ids] + rows
    return [r for r in merged if valid_pref(r)][:PREFS_MAX]


def private_texts(base: Path) -> list[str]:
    """Texts of soul-sourced prefs, for redaction from run archives (owner D1: never logged). Never raises."""
    try:
        rows = arti_state.read_jsonl(Path(base) / PREFS_FILE)
    except OSError:
        return []
    return [r["text"] for r in rows if r.get("source") == "soul" and isinstance(r.get("text"), str) and r["text"]]


def _tags(values: Iterable[Any]) -> set[str]:
    out = set()
    for value in values:
        if isinstance(value, str) and value.strip():
            out.add(value.strip().casefold())
            out.update(w for w in re.findall(r"[^\W_]+", value.casefold()) if len(w) > 2)
    return out


def _about(row: Mapping) -> set[str]:
    return _tags(row.get("about") or ())


def pick_prefs(prefs: Iterable[Mapping], *, game: str, tags: Iterable[str], projection_scale: int = 1) -> tuple[list[dict], list[dict]]:
    """(traits, game_prefs) for this boundary.

    Traits: pinned ``scope:any`` rows and pinned rows of this game (FIX79C, ADR 141), ≤3 lines; rows whose ``about``
    matches the boundary come first, then by strength, then file order (stable; never a rotation). Game prefs: unpinned
    ``scope:<game>`` rows only when their ``about`` intersects the boundary tags, ≤2 lines."""
    now = _tags(tags)
    rows = [dict(r) for r in prefs if valid_pref(r)]
    def rank(item):
        index, row = item
        strength = row.get("strength") if type(row.get("strength")) is int else 0
        return (-len(_about(row) & now), -strength, index)
    traits = [r for _, r in sorted(((i, r) for i, r in enumerate(rows)
                                    if r["scope"] in ("any", game) and r.get("pinned")), key=rank)][:TRAIT_LINES * projection_scale]
    game_prefs = [r for _, r in sorted(((i, r) for i, r in enumerate(rows)
                                        if r["scope"] == game and not r.get("pinned") and _about(r) & now), key=rank)][:GAME_PREF_LINES * projection_scale]
    return traits, game_prefs


# --------------------------------------------------------------------------- goals

GOAL_STATUSES = ("open", "done", "dropped", "expired", "capped")  # expired: legacy TTL rows only (FIX75C)


def valid_goal(row: Any) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("id"), str) and bool(row["id"])
            and row.get("horizon") in SEASON_HORIZONS and not text_reason(row.get("text"), GOAL_TEXT_MAX)
            and row.get("status") in GOAL_STATUSES
            and type(row.get("set_on_day")) is int)


def load_goals(base: Path, game: str) -> list[dict]:
    try:
        rows = arti_state.read_jsonl(game_dir(base, game) / GOALS_FILE)
    except (OSError, ValueError):
        return []
    return [r for r in rows if valid_goal(r)]


def bound_goals(rows: Iterable[dict], *, max_history: int = GOAL_HISTORY_MAX,
                max_bytes: int = GOALS_FILE_MAX_BYTES) -> tuple[list[dict], int]:
    """(rows, trimmed): the file's count/byte bound. Open rows are never trimmed; the oldest closed rows go first."""
    rows = list(rows)
    closed = [i for i, r in enumerate(rows) if r.get("status") != "open"]
    drop = set(closed[:max(0, len(closed) - max_history)])
    size = lambda: sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1  # noqa: E731
                       for j, r in enumerate(rows) if j not in drop)
    for i in closed:
        if size() <= max_bytes:
            break
        drop.add(i)
    return [r for i, r in enumerate(rows) if i not in drop], len(drop)


def save_goals(base: Path, game: str, rows: Iterable[dict]) -> int:
    """Writes the bounded file; returns how many closed history rows the bound trimmed (0 normally)."""
    kept, trimmed = bound_goals(rows)
    arti_state.write_jsonl_atomic(game_dir(base, game) / GOALS_FILE, kept)
    return trimmed


def expired_recent(row: Mapping, day: int) -> bool:
    """ADR 99 (a46: t6-1 rode 17 days and t15-1 eight as `tujuan.expired`, and reflection's drop of t6-1 was refused three
    times): an expired goal shows on the day it expired and the next, as a dropped one does. The row stays on disk."""
    since = row.get("expired_on_day")
    return type(since) is not int or day - since <= 1


def goal_alive(row: Mapping, day: int) -> bool:
    """Open and already in effect. FIX75C (ADR 133; a54 t20-1 on its 7th carry at Spring 26, one night from the old
    code's expiry): the legacy horizons have no TTL. They end only by her ``done`` with a cited ref, her ``drop``, or
    the logged safety cap. A missed ``today`` still carries. ADR 221 adds an explicitly seasonal horizon only."""
    if row.get("horizon") == "season":
        start = row.get("set_on_day")
        if type(start) is not int or (day - 1) // 28 != (start - 1) // 28:
            return False
    return row.get("status") == "open" and type(row.get("set_on_day")) is int and row["set_on_day"] <= day


def goal_age_days(row: Mapping, day: int) -> int:
    """Game days since the goal took effect (``set_on_day``); 0 on that day."""
    return max(0, day - int(row.get("set_on_day", day)))


def last_progress(row: Mapping, game: str) -> str | None:
    """``"<date>: <her cited evidence>"`` for the newest progress row she cited, else None (no evidence recorded)."""
    rows = row.get("evidence") or []
    if not rows or not isinstance(rows[-1], (list, tuple)) or len(rows[-1]) < 3:
        return None
    when, _ref, text = rows[-1][:3]
    stamp = plan_dates.label(game, when) if type(when) is int else str(when)
    return stamp + ": " + " ".join(str(text).split())[:LAST_PROGRESS_CHARS]


def cap_overflow(rows: Iterable[dict], day: int) -> tuple[list[dict], list[dict]]:
    """(rows, newly capped): the safety cap. More alive goals than ``GOAL_CAPS`` in a horizon (a legacy file or a
    concurrent writer; reflection refuses new goals over the cap before this) marks the oldest ``capped`` with
    ``capped_on_day``. Logged by the caller and shown to her as ``tujuan.dibatasi``; the row stays on disk."""
    out = [dict(r) for r in rows]
    capped = []
    for horizon, cap in GOAL_CAPS.items():
        alive = [r for r in out if valid_goal(r) and goal_alive(r, day) and r["horizon"] == horizon]
        for row in alive[:max(0, len(alive) - cap)]:
            row.update(status="capped", capped_on_day=day)
            capped.append(row)
    return out, capped


def open_goals(rows: Iterable[Mapping], day: int, *, horizons=HORIZONS) -> dict[str, list[dict]]:
    """Every alive goal by horizon, file order. Nothing is hidden here: the cap is enforced where rows are written
    (reflection refuses over it; ``cap_overflow`` marks and logs a surplus)."""
    out: dict[str, list[dict]] = {h: [] for h in horizons}
    for row in rows:
        if valid_goal(row) and goal_alive(row, day) and row["horizon"] in out:
            out[row["horizon"]].append(dict(row))
    return out


def dated_goal(row: Mapping, game: str, calendar=()) -> dict:
    # Legacy reflection goals were stored for the following day. for_day is mutable; set_on_day isn't.
    authored = row.get("set_on_day")
    if type(authored) is int and row.get("why_set") == "reflection":
        authored -= 1
    return plan_dates.ground(row, game=game, authored_day=authored, calendar=calendar)


def _goal_id_day(goal_id):
    match = re.fullmatch(r'[twl](\d+)-\d+', str(goal_id))
    return int(match[1]) if match else None


def goal_view(row: Mapping, *, game: str, day: int, calendar=(), native_targets=(), age_facts: bool = True) -> dict:
    grounded = dated_goal(row, game, calendar)
    view = dict(id=row["id"], text=plan_dates.annotate(row["text"], grounded, game=game, day=day))
    authored = row.get('set_on_day')
    if row.get('why_set') == 'reflection' and type(authored) is int:
        authored -= 1
    if type(authored) is int and authored != _goal_id_day(row['id']):
        view['set'] = authored  # default date is encoded in a validated ID; exceptions remain explicit
    if row.get("stale"):
        view["stale"] = True
    # FIX75C: a carried goal shows how old it is and the last progress she cited, as facts; absence is stated.
    if age_facts and type(day) is int and goal_age_days(row, day) >= 1:
        view["umur_hari"] = goal_age_days(row, day)
        view["maju_terakhir"] = last_progress(row, game) or "belum ada bukti"
    fact = plan_dates.fact(grounded, game=game, day=day)
    if fact:
        view["date_fact"] = fact
    from arti_runtime.goal_facts import status
    native = status(row["text"] + " " + str(row.get("done_when") or ""), native_targets)
    if native:
        view["native_status"] = native
    from arti_runtime.goal_facts import met_status
    if met := met_status(row['text'] + ' ' + str(row.get('done_when') or ''), native_targets):
        view['status'] = met
    return view


# --------------------------------------------------------------------------- the mind block (decision context)

def block_bytes(block: Any) -> int:
    return len(json.dumps(block, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def render_block(prefs: Iterable[Mapping], goals: Iterable[Mapping], *, game: str, day: int,
                 tags: Iterable[str] = (), yesterday: str | None = None,
                 max_bytes: int = BLOCK_MAX_BYTES, calendar=(), yesterday_dates=None, native_targets=(), projection_scale: int = 1) -> dict | None:
    """The ``mind`` block (≤ ``max_bytes`` compact JSON) or None when there is nothing of hers to show.

    Overflow drops yesterday, the released/capped facts, then the age/date facts (compact), game prefs, traits not
    about this choice, oldest week goals, oldest long goals, the remaining traits, then older expired facts. Today's goals and the most recent expired fact remain
    visible; a goal that leaves this page is still hers on disk and in full at bedtime (FIX75C)."""
    tags = list(tags)
    traits, game_prefs = pick_prefs(prefs, game=game, tags=tags, projection_scale=projection_scale)
    now_tags = _tags(tags)
    goals = [g for g in goals if not isinstance(g, Mapping) or g.get('horizon') != 'season']
    live = open_goals(goals, day)
    week = sorted(live["week"], key=lambda r: (int(r.get("set_on_day", 0)), r["id"]))
    long_ = sorted(live["long"], key=lambda r: (int(r.get("set_on_day", 0)), r["id"]))
    expired = [g for g in goals if valid_goal(g) and g.get("status") in ("open", "expired")
               and not goal_alive(g, day) and g["set_on_day"] <= day and expired_recent(g, day)][-GOAL_CAPS["today"]:]
    # ADR 45: a goal she dropped at the last reflection is a fact of her own day, shown once (yesterday/today).
    released = [g for g in goals if valid_goal(g) and g.get("status") == "dropped"
                and type(g.get("revised_on_day")) is int and 0 <= day - g["revised_on_day"] <= 1][-1:]
    # FIX75C: a goal the safety cap closed for her is shown the same way, once (yesterday/today), never silently.
    capped = [g for g in goals if valid_goal(g) and g.get("status") == "capped"
              and type(g.get("capped_on_day")) is int and 0 <= day - g["capped_on_day"] <= 1][-2:]
    compact_dates, expired_text = False, True
    block: dict[str, Any] = {}  # the "not an order" note rides once in the system line (MIND_WHY_LINE)
    def build():
        for key in ("sifat", "suka", "tujuan", "kemarin", "kemarin_date", "kemarin_basis", 'set_shift'):
            block.pop(key, None)
        if traits:
            block["sifat"] = [t["text"] for t in traits]
        if game_prefs:
            block["suka"] = [("" if p.get("polarity", "like") == "like" else "nggak suka: ") + p["text"]
                             for p in game_prefs]
        shown = {h: [goal_view(g, game=game, day=day, calendar=calendar, native_targets=native_targets,
                               age_facts=not compact_dates) for g in rows]
                 for h, rows in (("today", live["today"]), ("week", week), ("long", long_), ("expired", expired),
                                 ("dilepas", released), ("dibatasi", capped)) if rows}
        for view, g in zip(shown.get("dilepas", ()), released):
            view["status"] = "dropped on " + plan_dates.label(game, g["revised_on_day"])
        for view, g in zip(shown.get("dibatasi", ()), capped):
            view["status"] = "closed by the %d-per-horizon safety cap on %s" % (
                GOAL_CAPS[g["horizon"]], plan_dates.label(game, g["capped_on_day"]))
        # Reflection IDs name their effective day; their authored day is usually one day earlier.
        # Share a verified common offset instead of spending one duplicate date column per goal.
        dated = [(view, _goal_id_day(view['id'])) for views in shown.values() for view in views]
        dated = [(view, ordinal) for view, ordinal in dated if ordinal is not None]
        shifts = {view.get('set', ordinal) - ordinal for view, ordinal in dated}
        if len(shifts) == 1 and (shift := next(iter(shifts))) != 0:
            block['set_shift'] = shift
            for view, _ in dated:
                view.pop('set', None)
        if compact_dates:
            block["today"] = plan_dates.label(game, day)
            for horizon, views in shown.items():
                for view in views:
                    if view.get("date_fact"):
                        view["date_fact"] = view["date_fact"].replace("; today " + block["today"] + ": ", ": ")
                        if (not (horizon == "expired" and not expired_text)
                                and view.get("text") != next(g["text"] for g in goals if g["id"] == view["id"])):
                            view.pop("date_fact")  # the inline date/status is atomic with her words
                    if horizon == "expired" and not expired_text:
                        view.pop("text", None)  # identity/date/status remain; the full authored row stays on disk
        if shown:
            block["tujuan"] = shown
        if yesterday and not text_reason(yesterday, 200):
            block["kemarin"] = yesterday
            block["kemarin_basis"] = "authored reflection; completion comes from native evidence"
            remembered = dict(text=yesterday)
            if yesterday_dates is not None:
                remembered["referenced_days"] = yesterday_dates
            grounded = plan_dates.ground(remembered, game=game, authored_day=day - 1, calendar=calendar)
            block["kemarin"] = plan_dates.annotate(yesterday, grounded, game=game, day=day)
            from arti_runtime.goal_facts import annotate
            block["kemarin"] = annotate(block["kemarin"], native_targets)
            date_fact = plan_dates.fact(grounded, game=game, day=day)
            if date_fact:
                block["kemarin_date"] = date_fact
    build()
    if not block:
        return None
    while block_bytes(block) > max_bytes:
        if yesterday:
            yesterday = None
        elif released:
            released.pop()
        elif capped:
            capped.pop()
        elif not compact_dates:
            compact_dates = True  # FIX75C: week/long age and date facts go before any goal of hers leaves the page
        elif game_prefs:
            game_prefs.pop()
        elif traits and not (_about(traits[-1]) & now_tags):
            traits.pop()  # FIX75C: a trait not about this choice leaves before any goal of hers (pick_prefs ranks relevant first)
        elif week:
            week.pop(0)
        elif long_:
            long_.pop(0)
        elif traits:
            traits.pop()
        elif len(expired) > 1:
            expired.pop(0)
        elif expired and expired_text:
            expired_text = False
        else:
            break
        build()
    return block or None


# --------------------------------------------------------------------------- why + intent line

def clip_why(speech: Any) -> tuple[str | None, bool]:
    """(why, clipped): her speech as one line ≤120 chars, or None when empty. Her words, only shortened."""
    if not isinstance(speech, str):
        return None, False
    text = re.sub(r"\s+", " ", speech).strip()
    if not text:
        return None, False
    if len(text) <= WHY_MAX_CHARS:
        return text, False
    cut = text[:WHY_MAX_CHARS]
    space = cut.rfind(" ")
    return (cut[:space] if space >= 60 else cut).rstrip(" ,;:-"), True


def _read_now_raw(base: Path) -> dict | None:
    try:
        row = ipc.read_json(Path(base) / NOW_FILE)
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) and row.get("schema") == NOW_SCHEMA else None


_NOW_UPDATE_LOCK = threading.RLock()


def _serialize_now_update(fn):
    @wraps(fn)
    def update(*args, **kwargs):
        with _NOW_UPDATE_LOCK:
            return fn(*args, **kwargs)
    return update


def _preserve_newer_state_facts(native_facts, previous):
    for key in ("own_state", "menu_container", "last_container", "current_menu", "peer_sleep", "shop_availability", "own_crops", "goal_targets", "quest_status", "scene_state"):
        old = (previous.get("native_facts") or {}).get(key)
        new = native_facts.get(key)
        # Counter observations have their own reception clock, independent of motor pose.
        if key in ("shop_availability", "own_crops", "goal_targets", "quest_status", "scene_state") and isinstance(new, Mapping):
            old_row = old if isinstance(old, Mapping) else {}
            # Wall-minus-age is a projection of two clocks and may jitter backwards.
            # Within one native session, reception order owns counter freshness.
            same_session = (new.get("observation_session") is not None
                            and new.get("observation_session") == old_row.get("observation_session"))
            new_received, old_received = _ts(new, "received_monotonic"), _ts(old_row, "received_monotonic")
            if same_session and new_received is not None and old_received is not None:
                if new_received > old_received:
                    continue
            elif (_ts(new, "observed_ts") or 0) > (_ts(old_row, "observed_ts") or 0):
                continue
        native_facts[key] = old


@_serialize_now_update
def write_now(base: Path, *, game: str, why: str | None, goal_ref: str | None, goal_today: Iterable[str],
              game_time: Any = None, decision_ts: float | None = None, goal_dates: Iterable[Mapping] = (),
              native_facts: Mapping | None = None, calendar=(), observed_ts: float | None = None,
              intent_date: str | None = None, doing: Mapping | None = None) -> dict:
    """Overwrite ``now.json`` with the latest decision intent (a missing why stays null, never invented).

    ``doing`` (ADR 232 part I): the skill and argument she just chose; ``doing_now`` and ``heading_to`` carry it
    with the game clock it was chosen at, ``where_now`` the place and clock her body was last read at.

    A decision is not progress: the controller's ``blocked`` flag survives it (fix10c, a12 gift answers)."""
    ts = float(decision_ts if decision_ts is not None else time.time())
    previous = _read_now_raw(base) or {}
    native_facts = dict(native_facts) if native_facts is not None else {}
    authored_clock = game_time
    if (observed_ts is not None and previous.get("game") == game
            and type(previous.get("observed_ts")) in (int, float) and previous["observed_ts"] > observed_ts):
        game_time, observed_ts = previous.get("game_time"), previous["observed_ts"]
        _preserve_newer_state_facts(native_facts, previous)
    row = {"schema": NOW_SCHEMA, "game": str(game), "why": why, "goal_ref": goal_ref,
           "goal_today": [str(t) for t in goal_today][:GOAL_CAPS["today"]],
           "decision_ts": ts, "game_time": game_time, "observed_ts": ts if observed_ts is None else observed_ts,
           "blocked": previous.get("blocked") if isinstance(previous.get("blocked"), dict) else None}
    # FIX73D: every goal rides with its authored and target day, not only the dated ones, so the persona line can
    # tell a goal written for an earlier day from today's (plan_dates.intent_views).
    row["goal_dates"] = [dict(id=g["id"], **{k: g[k] for k in ("text", "authored_on_day", "referenced_days", "event_days",
                                                                "for_day", "horizon") if k in g})
                         for g in goal_dates if g.get("referenced_days") or type(g.get("for_day")) is int]
    authored = plan_dates.ordinal(game, intent_date or (authored_clock or {}).get("date")) if isinstance(authored_clock, Mapping) else None
    row["why_dates"] = plan_dates.references(why or "", game=game, authored_day=authored, calendar=calendar)
    row["why_event_days"] = plan_dates.event_references(why or "", game=game, authored_day=authored, calendar=calendar)
    row["intent_authored_on_day"] = authored
    row["native_facts"] = dict(native_facts) if native_facts is not None else {}
    from arti_runtime.time_grounding import location_change
    row["location_change"] = location_change(previous, game_time, row["observed_ts"])
    _now_places(row, previous, doing, authored_clock)
    _write_json_atomic(Path(base) / NOW_FILE, row)
    return row


TRIP_SKILLS = ("pergi_ke",)  # a chosen trip names where she intends to go, never where she is


def _clock_of(game_time):
    return ({k: game_time.get(k) for k in ("date", "time")} if isinstance(game_time, Mapping)
            and type(game_time.get("time")) is int else None)


def _now_places(row, previous, doing=None, chosen_at=None):
    """ADR 232 part I (a session: her voice named a trip
    her body had not made): three separate labelled facts for the voice, each with its game clock. ``where_now``: where her body
    was last read (place id and clock); ``doing_now``: the skill she chose and when; ``heading_to``: the destination
    of a chosen trip and when she chose it (None when the choice is not a trip). The older keys stay as they were."""
    gt = row.get("game_time")
    place = gt.get("place") if isinstance(gt, Mapping) else None
    row["where_now"] = dict(place=place, at=_clock_of(gt)) if isinstance(place, str) and place else None
    if isinstance(doing, Mapping) and isinstance(doing.get("skill"), str):
        at = _clock_of(chosen_at) or _clock_of(gt)
        arg = doing.get("arg") if isinstance(doing.get("arg"), str) else None
        row["doing_now"] = dict(skill=doing["skill"], arg=arg, chosen_at=at)
        row["heading_to"] = dict(place=arg, chosen_at=at) if doing["skill"] in TRIP_SKILLS and arg else None
    else:
        for key in ("doing_now", "heading_to"):
            row[key] = previous.get(key) if isinstance(previous.get(key), Mapping) else None


@_serialize_now_update
def note_doing(base: Path, *, game: str, doing: Mapping, chosen_at: Any = None) -> dict | None:
    """ADR 232 part I: record the skill she just chose (``doing_now``) and a chosen trip's destination
    (``heading_to``) on the current ``now.json`` row, each with the game clock it was chosen at."""
    row = _read_now_raw(base)
    if row is None or row.get("game") != game:
        return None
    _now_places(row, dict(row), doing, chosen_at)
    _write_json_atomic(Path(base) / NOW_FILE, row)
    return row


@_serialize_now_update
def mark_state(base: Path, *, game: str, game_time: Any = None, blocked: Mapping | None = None,
               observed_ts: float | None = None, native_facts: Mapping | None = None) -> dict:
    """Controller heartbeat on ``now.json``: the game clock and a generic blocked flag, never the decision.

    ``blocked`` = ``{"since_ts": wall seconds of the last progress, "menu_open": bool}`` or None. PM-1 fix10c
    (a12): during an 18.9 min stall no decision wrote the file, so the persona saw a stale why and invented motion."""
    row = _read_now_raw(base) or {"schema": NOW_SCHEMA, "game": str(game), "why": None, "goal_ref": None,
                                  "goal_today": [], "decision_ts": None}
    stamp = float(observed_ts if observed_ts is not None else time.time())
    native_facts = dict(native_facts) if native_facts is not None else {}
    if (row.get("game") == game and type(row.get("observed_ts")) in (int, float)
            and row["observed_ts"] > stamp):
        game_time, stamp = row.get("game_time"), row["observed_ts"]
        _preserve_newer_state_facts(native_facts, row)
    row["native_facts"] = dict(native_facts) if native_facts is not None else {}
    from arti_runtime.time_grounding import location_change
    row["location_change"] = location_change(row, game_time, stamp)
    row["observed_ts"] = stamp
    if game_time is not None:
        row["game_time"] = game_time
    row["blocked"] = ({"since_ts": float(blocked["since_ts"]), "menu_open": bool(blocked.get("menu_open"))}
                      if isinstance(blocked, Mapping) and type(blocked.get("since_ts")) in (int, float) else None)
    _now_places(row, dict(row))  # ADR 232 part I: the heartbeat moves where_now; doing_now / heading_to stay
    _write_json_atomic(Path(base) / NOW_FILE, row)
    return row


def _ts(row: Mapping, key: str) -> float | None:
    value = row.get(key)
    return float(value) if type(value) in (int, float) else None


def read_now(base: Path, *, now: float | None = None, max_age_sec: float = NOW_MAX_AGE_SEC) -> dict | None:
    """The current row, or None when missing, unreadable, malformed or stale. Never raises.

    Current = the newest of the decision and the controller heartbeat is within ``max_age_sec``; a stale
    decision under a live heartbeat is still returned (the line then says so instead of presenting it as now)."""
    row = _read_now_raw(base)
    if row is None:
        return None
    stamps = [t for t in (_ts(row, "decision_ts"), _ts(row, "observed_ts")) if t is not None]
    if not stamps:
        return None
    age = (time.time() if now is None else now) - max(stamps)
    if not 0 <= age <= max_age_sec:
        return None
    return row


def _minutes_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds} detik" if seconds < 60 else f"{seconds // 60} menit"


def intent_line(row: Mapping | None, *, now: float | None = None, max_age_sec: float = NOW_MAX_AGE_SEC) -> str:
    """One generic ``[NIATKU]`` line for the persona, or '' when there is no current intent or state."""
    if not row:
        return ""
    now = time.time() if now is None else now
    why = row.get("why") if isinstance(row.get("why"), str) else ""
    goals = [g for g in row.get("goal_today") or () if isinstance(g, str) and g.strip()]
    blocked = row.get("blocked") if isinstance(row.get("blocked"), dict) else None
    decided = _ts(row, "decision_ts")
    stale = decided is None or not 0 <= now - decided <= max_age_sec
    from arti_runtime.time_grounding import current_line, current_state
    state = current_state(row, now=now)
    current = current_line(state)
    native = row.get("native_facts") if state["basis"] == "latest_observation" else None
    from arti_runtime.time_grounding import delivered_native
    from arti_runtime.fact_names import projection
    current += "\n[NATIVE_FACTS] " + json.dumps(projection(delivered_native(native, row.get("game"), state) or {"status": "unknown_or_stale"}),
                                                ensure_ascii=False)
    day = plan_dates.ordinal(row.get("game"), (state.get("current_time") or {}).get("date"))
    dated = list(row.get("goal_dates") or [])
    views = plan_dates.intent_views(row, day=day)
    from arti_runtime.goal_facts import intent_views
    views = intent_views(row, views, now=now)
    why, goals = views["why"], views["goals"]
    if row.get("why_dates"):
        dated.append(dict(id="last_intent", referenced_days=row["why_dates"]))
    date_facts = [str(g["id"]) + ": " + plan_dates.fact(g, game=row.get("game"), day=day) for g in dated]
    dates_line = "\n[PLAN_DATES] " + " | ".join(date_facts) if date_facts else ""
    parts = []
    if blocked is not None and type(blocked.get("since_ts")) in (int, float):
        parts.append("STATUS: aku lagi tertahan, belum ada kemajuan sejak " + _minutes_text(now - blocked["since_ts"])
                     + " lalu" + (" (ada dialog/menu terbuka di layar)" if blocked.get("menu_open") else "")
                     + "; kalau ditanya, bilang jujur lagi tertahan, jangan mengarang gerakan")
    if why:
        parts.append(("niat terakhirku (" + _minutes_text(now - decided) + " lalu, belum ada keputusan baru): "
                      if stale and decided is not None else "barusan aku milih karena: ") + why)
    if goals:
        parts.append("tujuanku hari ini: " + "; ".join(goals))
    if views.get("goals_earlier_days"):  # FIX73D: her words, dated; never called today's
        parts.append("tujuan dari hari sebelumnya yang belum kututup: " + "; ".join(views["goals_earlier_days"]))
    if not parts:
        return current + dates_line if row.get("game_time") else ""
    # Keep text plus its resolved annotation indivisible. Authoring word/goal
    # caps bound dated intent; the ordinary truncation applies to undated text.
    text = re.sub(r"\s+", " ", " | ".join(parts)).strip()
    if " [" not in text:
        text = text[:NOW_LINE_MAX_CHARS]
    return ("[NIATKU] " + text + (" (STATUS = fakta dari game; niat = niatku sendiri" if blocked else " (ini niatku sendiri")
            + ", bukan fakta yang sudah terjadi; jangan dibacakan mentah, pakai kalau relevan)\n" + current + dates_line)


def intent_block(base: Path, *, now: float | None = None, max_age_sec: float = NOW_MAX_AGE_SEC) -> str:
    """``"\\n[NIATKU] ...\\n"`` for the persona turn, or '' (missing/stale/broken file → nothing)."""
    try:
        line = intent_line(read_now(base, now=now, max_age_sec=max_age_sec), now=now, max_age_sec=max_age_sec)
    except Exception:  # noqa: BLE001 - the persona turn never fails on the mind file
        return ""
    return "\n" + line + "\n" if line else ""


# --------------------------------------------------------------------------- journey (one row per game day)

def load_journey(base: Path, game: str) -> list[dict]:
    try:
        return arti_state.read_jsonl(game_dir(base, game) / JOURNEY_FILE)
    except (OSError, ValueError):
        return []


# fix55C / ADR 78 (REVIEW2 #6): the deferred reflection thread and the main thread's record_discoveries both rewrite the
# journey file (read-modify-write); one module lock keeps either from dropping the other's row.
_JOURNEY_LOCK = threading.RLock()


def save_journey_row(base: Path, game: str, row: dict) -> None:
    with _JOURNEY_LOCK:
        _save_journey_row(base, game, row)


def _save_journey_row(base: Path, game: str, row: dict) -> None:
    prior = load_journey(base, game)
    old = next((r for r in prior if r.get("id") == row["id"]), {})
    discoveries, seen = [], set()
    for item in old.get("discoveries", []) + row.get("discoveries", []):
        key = ("id", item["id"]) if isinstance(item, dict) and isinstance(item.get("id"), str) else (
            "legacy", json.dumps(item, sort_keys=True, ensure_ascii=False))
        if key not in seen:
            seen.add(key)
            discoveries.append(item)
    row = dict(row, discoveries=discoveries)
    rows = [r for r in prior if r.get("id") != row["id"]] + [row]
    arti_state.write_jsonl_atomic(game_dir(base, game) / JOURNEY_FILE, rows[-120:])


def record_discoveries(base: Path, game: str, *, scope: str, discoveries: Iterable[Mapping],
                       day: int, day_label: str) -> list[dict]:
    """Durable first-seen receipts, independent of reflection success and session restarts.

    The adapter's scope identifies the world+actor. The mind stores facts, never derives game semantics.
    """
    with _JOURNEY_LOCK:
        return _record_discoveries(base, game, scope=scope, discoveries=discoveries, day=day, day_label=day_label)


def _record_discoveries(base: Path, game: str, *, scope: str, discoveries: Iterable[Mapping],
                        day: int, day_label: str) -> list[dict]:
    import hashlib
    path = game_dir(base, game) / "discoveries.jsonl"
    rows = arti_state.read_jsonl(path) if path.exists() else []
    known = {r.get("id") for r in rows}
    scope_hash = hashlib.sha256(scope.encode()).hexdigest()
    fresh = []
    for event in discoveries:
        if not isinstance(event, Mapping) or not isinstance(event.get("id"), str):
            continue
        identity = hashlib.sha256((scope + "\0" + event["id"]).encode()).hexdigest()
        if identity in known:
            continue
        text = event.get("text")
        row = dict(id=identity, kind=str(event.get("kind") or "unknown"),
                   subject=str(event.get("subject") or "unknown"),
                   text=text if isinstance(text, str) and text.strip() else "discovery: unknown",
                   source=str(event.get("source") or "unknown"),
                   date=str(event.get("date") or day_label), observed_day=day,
                   day=event.get("day") if type(event.get("day")) is int else day,
                   day_label=str(event.get("day_label") or day_label), scope_hash=scope_hash,
                   native_seq=event.get("seq") if type(event.get("seq")) is int else 0,
                   event_time=event.get("event_time") or event.get("date") or day_label,
                   event_place=event.get("event_place") or (event.get("subject") if event.get("kind") == "location_entered" else None))
        if isinstance(event.get("place_name"), str):
            row["place_name"] = event["place_name"]
        if isinstance(event.get("references"), list):
            row["references"] = event["references"]
        known.add(identity)
        fresh.append(row)
    if fresh:
        for label in dict.fromkeys(r["day_label"] for r in fresh):
            group = [r for r in fresh if r["day_label"] == label]
            journey = next((r for r in load_journey(base, game) if r.get("id") == label),
                           dict(v=MIND_VERSION, schema=JOURNEY_SCHEMA, id=label, date=label,
                                day=group[0]["day"], effects={}, places=[], discoveries=[]))
            save_journey_row(base, game, dict(journey, discoveries=journey.get("discoveries", []) + group))
        arti_state.write_jsonl_atomic(path, rows + fresh)
    return fresh


def recent_discoveries(base: Path, game: str, *, limit: int = 3, day: int | None = None,
                       scope: str | None = None) -> list[dict]:
    path = game_dir(base, game) / "discoveries.jsonl"
    rows = arti_state.read_jsonl(path) if path.exists() else []
    if scope is not None:
        import hashlib
        scope_hash=hashlib.sha256(scope.encode()).hexdigest()
        rows=[r for r in rows if r.get("scope_hash")==scope_hash]
        rows.sort(key=lambda r: r.get("native_seq",0))
    rows = [r for r in rows if day is None or r.get("day", r.get("observed_day")) == day]
    # ADR 232 part A: speech (ADR 216 "stream:" rows, her own lines included) is never a discovery.
    rows = [r for r in rows if not str(r.get("source") or "").startswith("stream:")]
    return [dict(id=r["id"], text=r["text"], date=r.get("day_label", r.get("date")),
                 event_time=r.get("event_time") or r.get("date"), event_place=r.get("event_place"),
                 place_name=r.get("place_name"),
                 relation="past_observation",
                 **({"references": r["references"]} if r.get("references") else {}))
            for r in rows[-limit:] if r.get("id") and r.get("text")]


def item_descriptions(base: Path, game: str, *, scope: str | None = None) -> dict:
    """ADR 88: the game's own item text she has met, by qualified id. recent_discoveries keeps only the newest rows."""
    import hashlib
    from arti_runtime.item_facts import native_descriptions
    path = game_dir(base, game) / "discoveries.jsonl"
    rows = arti_state.read_jsonl(path) if path.exists() else []
    if scope is not None:
        scope_hash = hashlib.sha256(scope.encode()).hexdigest()
        rows = [r for r in rows if r.get("scope_hash") == scope_hash]
    return native_descriptions(rows)


def yesterday_line(base: Path, game: str, day: int) -> str | None:
    """Her own one-line summary of the previous game day, when a reflection wrote one."""
    for row in reversed(load_journey(base, game)):
        if row.get("day") == day - 1 and isinstance((row.get("reflection") or {}).get("today"), str):
            text = row["reflection"]["today"].strip()
            return text[:200] if text else None
    return None


def yesterday_dates(base: Path, game: str, day: int):
    for row in reversed(load_journey(base, game)):
        if row.get("day") == day - 1:
            return (row.get("reflection") or {}).get("today_dates")
    return None


# --------------------------------------------------------------------------- likes (FIX75C: what she found fun or not)
# Her own ``fun`` / ``not_fun`` claims from reflection, each with its verified ref, dated by game day. A memory, not a
# preference rule: nothing here ranks a choice or picks for her; it is shown back to her at bedtime as "yang kamu suka".
# Soul/live prefs files are never read or written here.

def valid_like(row: Any) -> bool:
    return (isinstance(row, dict) and row.get("schema") == LIKE_SCHEMA and isinstance(row.get("id"), str) and bool(row["id"])
            and row.get("polarity") in ("like", "dislike") and type(row.get("day")) is int
            and isinstance(row.get("date"), str) and isinstance(row.get("ref"), str) and bool(row["ref"])
            and not text_reason(row.get("text"), PREF_TEXT_MAX + 20))


def load_likes(base: Path, game: str) -> list[dict]:
    try:
        rows = arti_state.read_jsonl(game_dir(base, game) / LIKES_FILE)
    except (OSError, ValueError):
        return []
    return [r for r in rows if valid_like(r)]


def bound_likes(rows: Iterable[dict], *, max_rows: int = LIKES_MAX, max_bytes: int = LIKES_MAX_BYTES) -> tuple[list[dict], int]:
    rows = list(rows)
    trimmed = max(0, len(rows) - max_rows)
    rows = rows[trimmed:]
    while rows and sum(len(json.dumps(r, ensure_ascii=False).encode("utf-8")) + 1 for r in rows) > max_bytes:
        rows.pop(0)
        trimmed += 1
    return rows, trimmed


def save_likes(base: Path, game: str, *, day: int, day_label: str, fun: Iterable[Mapping], not_fun: Iterable[Mapping],
               evidence: Mapping[str, str] | None = None) -> dict:
    """Append her verified claims for ``day`` (that day's rows are replaced, never doubled).
    Returns ``{saved, trimmed}``; a bad-shaped claim is skipped, never repaired."""
    evidence = dict(evidence or {})
    fresh = []
    for polarity, claims in (("like", fun), ("dislike", not_fun)):
        for n, claim in enumerate(claims, 1):
            if not isinstance(claim, Mapping):
                continue
            row = dict(v=MIND_VERSION, schema=LIKE_SCHEMA, id="%d-%s-%d" % (day, polarity, n), day=day, date=str(day_label),
                       polarity=polarity, text=claim.get("text"), ref=str(claim.get("ref") or ""))
            if evidence.get(row["ref"]):
                row["evidence"] = str(evidence[row["ref"]])[:140]
            if valid_like(row):
                fresh.append(row)
    rows = [r for r in load_likes(base, game) if r.get("day") != day] + fresh
    rows, trimmed = bound_likes(rows)
    arti_state.write_jsonl_atomic(game_dir(base, game) / LIKES_FILE, rows)
    return dict(saved=len(fresh), trimmed=trimmed)


def likes_lines(base: Path, game: str, *, before_day: int | None = None, limit: int = LIKES_SHOWN) -> dict[str, list[str]]:
    """``{"suka": [...], "nggak_suka": [...]}``: her newest dated rows, oldest first, as ``"<date>: <her words>"``.
    ``before_day`` leaves out the day being reflected on (its claims are in the reply, not yet memory)."""
    rows = [r for r in load_likes(base, game) if before_day is None or r["day"] < before_day]
    out = {}
    for key, polarity in (("suka", "like"), ("nggak_suka", "dislike")):
        picked = [r for r in rows if r["polarity"] == polarity][-limit:]
        out[key] = [(r["date"] + ": " + r["text"])[:LIKE_LINE_CHARS] for r in picked]
    return out


def diary_beats(base: Path, *, wall_date: str) -> list[str]:
    """Game-day reflections written on ``wall_date`` (YYYY-MM-DD), as diary beats. Never raises."""
    beats = []
    try:
        games = sorted(p for p in Path(base).iterdir() if p.is_dir())
    except OSError:
        return []
    for folder in games:
        for row in arti_state.read_jsonl(folder / JOURNEY_FILE):
            reflection = row.get("reflection") or {}
            if str(row.get("reflected_at") or "")[:10] != wall_date or not isinstance(reflection.get("today"), str):
                continue
            line = "[%s %s] %s" % (folder.name, row.get("date") or row.get("id"), reflection["today"].strip())
            fun = [f.get("text") for f in reflection.get("fun") or () if isinstance(f, dict) and f.get("text")]
            if fun:
                line += " Seru: " + "; ".join(fun[:2]) + "."
            beats.append(line)
    return beats


# --------------------------------------------------------------------------- durable records + progress (fix13c)
# PROGRESS_GAP_AUDIT_2026-10-29 §6/§7: letters and quests were readable for one menu turn only, and no progress surface
# persisted. The adapter's records (any kind: letter, quest, ...) and progress snapshots are stored per world+actor scope;
# the mind keeps them as facts and never derives game meaning from them.
LEDGER_FILE = "ledger.jsonl"
PROGRESS_FILE = "progress.jsonl"
LEDGER_HISTORY_MAX = 8
_LEDGER_LOCK = threading.RLock()


def _serialize_ledger_update(fn):
    @wraps(fn)
    def update(*args, **kwargs):
        with _LEDGER_LOCK:
            return fn(*args, **kwargs)
    return update
PROGRESS_ROWS_MAX = 400


def _scope_hash(scope: str) -> str:
    import hashlib
    return hashlib.sha256(str(scope).encode()).hexdigest()


@_serialize_ledger_update
def record_items(base: Path, game: str, *, scope: str, kind: str, items: Iterable[Mapping], day: int,
                 day_label: str) -> list[dict]:
    """Upsert adapter records keyed by (scope, kind, item id). Returns the rows that are new or changed status.

    An item is ``{id, status, title?, text?, facts?}``. The first-seen day is kept; status changes append to a bounded
    history. A record missing from a later read is not deleted (absence is not a fact here)."""
    import hashlib
    path = game_dir(base, game) / LEDGER_FILE
    rows = arti_state.read_jsonl(path) if path.exists() else []
    by_id = {r.get("id"): r for r in rows}
    scope_hash = _scope_hash(scope)
    changed, dirty = [], False
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        identity = hashlib.sha256((scope + "\0" + kind + "\0" + item["id"]).encode()).hexdigest()
        status = str(item.get("status") or "unknown")
        old = by_id.get(identity)
        row = dict(old or dict(id=identity, kind=kind, item_id=item["id"], scope_hash=scope_hash, first_day=day,
                               first_label=str(day_label), history=[]))
        for key in ("title", "text"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                row[key] = value.strip()[:240]
        if isinstance(item.get("facts"), Mapping):
            row["facts"] = dict(item["facts"])
        if old is None or old.get("status") != status:
            row["status"], row["status_day"], row["status_label"] = status, day, str(day_label)
            row["history"] = (list(row.get("history") or []) + [[str(day_label), status]])[-LEDGER_HISTORY_MAX:]
            changed.append(row)
        dirty |= row != old
        by_id[identity] = row
    if dirty:
        arti_state.write_jsonl_atomic(path, list(by_id.values()))
    return changed


def ledger_rows(base: Path, game: str, *, scope: str, kind: str | None = None) -> list[dict]:
    path = game_dir(base, game) / LEDGER_FILE
    try:
        rows = arti_state.read_jsonl(path) if path.exists() else []
    except (OSError, ValueError):
        return []
    scope_hash = _scope_hash(scope)
    return [r for r in rows if r.get("scope_hash") == scope_hash and (kind is None or r.get("kind") == kind)]


def save_progress(base: Path, game: str, *, scope: str, day: int, day_label: str, snapshot: Mapping) -> dict:
    """The day's first snapshot (``start``) is kept; ``latest`` is overwritten. Returns the stored day row."""
    path = game_dir(base, game) / PROGRESS_FILE
    rows = arti_state.read_jsonl(path) if path.exists() else []
    identity = "%s:%d" % (_scope_hash(scope)[:16], day)
    from arti_runtime.save_reload import latest as latest_reload
    reload = latest_reload(base, scope) if game == "stardew" else None
    if reload is not None and day >= reload["to_day"]:
        identity += ":r%d" % int(reload["at"])  # ADR 232 part H: a day played again gets its own row
    old = next((r for r in rows if r.get("id") == identity), None)
    if old is not None and old.get("latest") == dict(snapshot):
        return old  # unchanged: no rewrite per decision boundary
    row = dict(id=identity, scope_hash=_scope_hash(scope), day=day, day_label=str(day_label),
               start=(old or {}).get("start") or dict(snapshot), latest=dict(snapshot), at=time.time())
    rows = [r for r in rows if r.get("id") != identity] + [row]
    arti_state.write_jsonl_atomic(path, rows[-PROGRESS_ROWS_MAX:])
    return row


def progress_days(base: Path, game: str, *, scope: str, include_before_reload: bool = False) -> list[dict]:
    """Her day snapshots, oldest day first. ADR 232 part H: after a save was loaded back, the snapshots of the days
    before the reload (written before it, dated on or after the day it went back to) are not the present world and
    are left out unless ``include_before_reload``; they stay in the file."""
    path = game_dir(base, game) / PROGRESS_FILE
    try:
        rows = arti_state.read_jsonl(path) if path.exists() else []
    except (OSError, ValueError):
        return []
    scope_hash = _scope_hash(scope)
    rows = [r for r in rows if r.get("scope_hash") == scope_hash and type(r.get("day")) is int]
    if not include_before_reload and game == "stardew":
        from arti_runtime.save_reload import latest as latest_reload, before_reload
        if (reload := latest_reload(base, scope)) is not None:
            rows = [r for r in rows if not before_reload(reload, day=r["day"], at=r.get("at"))]
    return sorted(rows, key=lambda r: r["day"])
