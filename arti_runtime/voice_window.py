"""One chronological conversation window for her live voice turn (V6.1).

The grouped history (``arti_bridge.get_categorized_history``: the owner's last 5 lines, her last 3, one line per
viewer, each group in its own block) lost the order of the talk: an owner line just before a reply could drop while
the reply stayed, and an older plan could be gone when he later repeated a part of it. The Codex thread resets every
6 turns, so it cannot carry older talk either.

Under ``live_voice_fixes_v1`` (mabar turns it on) ``get_categorized_history`` returns :func:`render` instead:

- every conversation line (owner, her, chat) of the last 4 minutes, at most the newest 24, in the order it was said
  and exactly as recorded (``[HH:MM:SS] [speaker] text``);
- plus the owner's last 8 lines said within the last 15 minutes (V6.4: kept apart from the 50-line deque), so what he said earlier is still in front of
  her when he has not said 8 more lines since; they join the same order, nothing is regrouped;
- at most ``MAX_CHARS`` characters for the conversation block, header included; the oldest lines go first.

Native text read in her game keeps its own block and its last 8 lines, exactly as before. System lines stay out,
as before. Pure function; no I/O, no model call.
"""
from __future__ import annotations

from collections import deque
import re
import time

WINDOW_SEC = 240        # the last 4 minutes ...
WINDOW_LINES = 24       # ... or the newest 24 conversation lines, whichever is smaller
OWNER_KEEP = 8          # the owner's last 8 lines stay even when older than the window ...
OWNER_REACH_SEC = 900   # ... when said within the last 15 minutes (V6.4; CT 2026-11-18)
MAX_CHARS = 3500        # the conversation block, header included
GAME_READ_KEEP = 8      # unchanged from the grouped history
FUTURE_SLACK_SEC = 60   # a stamp up to a minute after `now` counts as now

GAME_READ_HEADER = "=== NATIVE TEXT PREVIOUSLY READ IN HER GAME ==="
HEADER = ("=== OBROLAN TERAKHIR, URUT WAKTU (atas paling lama, bawah paling baru; "
          "baris [Arti (VTuber)] = JAWABAN ARTI TERAKHIR) ===")
EMPTY = "(Belum ada history)"

# add_to_history writes "[HH:MM:SS] [source] text"; a viewer source is "Viewer <name> (<platform>)".
_LINE = re.compile(r"\[(\d\d):(\d\d):(\d\d)\] \[(Streamer|Arti \(VTuber\)|Game read|Viewer .+? \([^()\[\]]*\))\] ")
_DAY = 86400


def _kind(label: str) -> str:
    if label == "Streamer":
        return "owner"
    if label == "Arti (VTuber)":
        return "arti"
    if label == "Game read":
        return "game"
    return "chat"


def _rows(lines, now: float | None):
    """``[kind, age_sec, line]`` oldest first. Ages run newest to oldest and never decrease, so a line from before
    midnight counts as older, never as tomorrow."""
    clock = time.localtime(time.time() if now is None else now)
    now_sec = clock.tm_hour * 3600 + clock.tm_min * 60 + clock.tm_sec
    rows = []
    for line in lines:
        match = _LINE.match(str(line))
        if match:
            stamp = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
            rows.append([_kind(match[4]), stamp, str(line)])
    floor = 0
    for row in reversed(rows):
        age = (now_sec - row[1]) % _DAY
        if age > _DAY - FUTURE_SLACK_SEC:
            age = 0  # a line stamped a moment after `now` (clock step) is new, not a day old
        while age < floor:
            age += _DAY
        floor = row[1] = age
    return rows


# V6.4: on a busy stream the owner's plan can leave the bridge's 50-line history deque within minutes, so no
# window over the deque could show it. His own lines are kept here
# for OWNER_REACH_SEC, fed and cleared by the bridge under its history lock (a privacy boundary clears both); the
# window still shows at most OWNER_KEEP of them inside MAX_CHARS, so the prompt does not grow.
_OWNER_LINES: deque = deque(maxlen=256)


def note_owner(line: str, now: float | None = None) -> None:
    _OWNER_LINES.append((time.time() if now is None else float(now), str(line)))


def clear_owner_lines() -> None:
    _OWNER_LINES.clear()
    _MARKED.clear()


# V6.5: owner lines her voice marked as about her play (ADR 233's tag) stay in her window for
# OWNER_REACH_SEC outside the last-OWNER_KEEP recency, bounded at MARKED_KEEP lines / MARKED_CHARS. Recency alone
# cannot hold a plan through a busy stretch of talk.
# The same marked lines reach her game page (ADR 233), so her voice and her game brain hold the same plans.
MARKED_KEEP = 4
MARKED_CHARS = 600
_MARKED: deque = deque(maxlen=32)


def mark_owner(text: str) -> bool:
    """Mark the newest kept owner line whose words are ``text`` (her trigger). True when one was found."""
    tail = "] [Streamer] " + str(text or "")
    for at, line in reversed(_OWNER_LINES):
        if line.endswith(tail):
            _MARKED.append((at, line))
            return True
    return False


def marked_owner_lines(now: float | None = None) -> list[str]:
    t = time.time() if now is None else float(now)
    return [line for at, line in _MARKED if 0 <= t - at <= OWNER_REACH_SEC]


def owner_lines(now: float | None = None) -> list[str]:
    """The owner's recorded lines of the last OWNER_REACH_SEC, oldest first."""
    t = time.time() if now is None else float(now)
    return [line for at, line in _OWNER_LINES if 0 <= t - at <= OWNER_REACH_SEC]


def render(lines, now: float | None = None, owner_extra=(), marked=()) -> str:
    """The voice turn's history: the game-read block as before, then one chronological conversation block.

    ``owner_extra``: the owner's kept lines (``owner_lines``); those no longer in ``lines`` are older than all of
    them and go first, in order."""
    lines = list(lines)
    present = set(lines)
    lines = [line for line in owner_extra if line not in present] + lines
    rows = _rows(lines, now)
    reads = [row[2] for row in rows if row[0] == "game"]
    talk = [i for i, row in enumerate(rows) if row[0] != "game"]
    keep = {i for i in talk if rows[i][1] <= WINDOW_SEC}
    keep = set(sorted(keep)[-WINDOW_LINES:])
    owner = [i for i in talk if rows[i][0] == "owner" and rows[i][1] <= OWNER_REACH_SEC]
    keep.update(owner[-OWNER_KEEP:])
    marked_set, marked_kept, marked_size = set(marked), set(), 0
    for i in reversed(owner):  # V6.5: newest marked first, bounded
        if rows[i][2] in marked_set and i not in keep and len(marked_kept) < MARKED_KEEP                 and marked_size + len(rows[i][2]) + 1 <= MARKED_CHARS:
            marked_kept.add(i)
            marked_size += len(rows[i][2]) + 1
    keep.update(marked_kept)
    order = sorted(keep)
    kept = [rows[i][2] for i in order]
    size = len(HEADER) + sum(1 + len(text) for text in kept)
    while size > MAX_CHARS and len(kept) > 1:
        # the oldest unmarked line goes first; a marked plan stays while anything else can go
        drop = next((j for j, i in enumerate(order) if i not in marked_kept), 0)
        size -= 1 + len(kept.pop(drop))
        order.pop(drop)
    if kept and size > MAX_CHARS:
        kept[0] = kept[0][:max(0, MAX_CHARS - len(HEADER) - 1)]
    result = []
    if reads:
        result.append(GAME_READ_HEADER)
        result.extend(reads[-GAME_READ_KEEP:])  # Historical text, never a claim about the current screen.
    if kept:
        result.append(HEADER)
        result.extend(kept)
    return "\n".join(result) if result else EMPTY
