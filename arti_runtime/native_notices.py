"""ADR 227: what the game announced to her today, and the mailbox flag she saw.

Pure projection over delivered native receipts (ADR 219/220). No festival
name, language string, game data file or hidden schedule is read here. The
lines carry the game's own words with their source, date, clock and place.
"""
from collections.abc import Mapping
import hashlib

# ModEntry.GameText.cs identifies a HUD message as content:sha256(message + NUL +
# HUDMessage.number). Item pickups carry their count (>= 1); a text message
# with no item has number -1. This is the only classifier: no word list.
TEXT_MESSAGE_NUMBER = "-1"
NOTICE_BYTES = 400  # per page-budget scale; the latest announcements first
NOTICE_SOURCE = "pesan layar game"  # fix131A: a trip labelled by today's notice carries this source
# What the offered mail press does; it is not a reason to press it.
MAIL_PRESS_LABEL = "membuka kotak suratmu"


def _day(receipt):
    value = receipt.get("game_day")
    return value.get("absolute") if isinstance(value, Mapping) else None


def _clock(value):
    return "%02d:%02d" % divmod(value, 100) if type(value) is int and 0 <= value <= 2600 else "jam belum tercatat"


def words(text):
    """The game's words with its line wrapping undone; no word is changed."""
    return " ".join(str(text).split())


def text_announcement(receipt, text):
    return (receipt.get("kind") == "hud_notice" and isinstance(text, str) and bool(text.strip())
            and receipt.get("id") == "content:" + hashlib.sha256(
                (text + "\0" + TEXT_MESSAGE_NUMBER).encode()).hexdigest())


def today_notices(w, today):
    """Today's delivered HUD text-message receipts, newest first, each distinct text once."""
    rows = []
    for receipt in w.matching("hud_notice"):
        text = w.text(receipt)
        if today is not None and _day(receipt) == today and text_announcement(receipt, text):
            rows.append(receipt)
    rows.sort(key=lambda r: r.get("clock") if type(r.get("clock")) is int else -1, reverse=True)
    out, seen = [], set()
    for receipt in rows:
        text = words(w.text(receipt))
        if text not in seen:
            seen.add(text)
            out.append(receipt)
    return out


def source_line(receipt):
    """The notice's own source and time, as its trip label carries it (fix131A)."""
    return "%s, %s %s" % (NOTICE_SOURCE, (receipt.get("game_day") or {}).get("text") or "tanggal belum tercatat",
                          _clock(receipt.get("clock")))


def named_places(w, today):
    """fix131A: [(receipt, place, words)] for each native place today's notices name.

    The ADR 220/227 rule only: a native place id or label as whole words of the
    delivered text, line wraps matching a space. No festival or place word list;
    the place is the one the game's words name, never a place "near" it.
    """
    from arti_runtime.fact_line import _name_pattern
    from arti_runtime.native_rows import native_rows
    places = [p for p in native_rows(((w.catalog or {}).get("world_graph") or {}).get("places", ()))
              if isinstance(p, Mapping) and isinstance(p.get("id"), str) and p["id"]]
    out, taken = [], set()
    for receipt in today_notices(w, today):
        text = w.text(receipt)
        for place in places:
            names = [n for n in (place.get("id"), place.get("label")) if isinstance(n, str) and n]
            if place["id"] in taken or not any(_name_pattern(n).search(text) for n in names):
                continue
            taken.add(place["id"])
            out.append((receipt, place, w.display_name(place, receipt)))
    return out


def announcements(w, today, *, max_bytes=NOTICE_BYTES, notes=None):
    """Today's delivered HUD text messages, newest first, each distinct text once.

    ``notes`` (fix131A): receipt id -> one plain fact the page adds to that
    notice's own line (where its named place is, as a choice, on this page).
    """
    out, used = [], 0
    for receipt in today_notices(w, today):
        text = words(w.text(receipt))
        line = "%s (pesan layar game, %s %s, %s)" % (
            text, (receipt.get("game_day") or {}).get("text") or "tanggal belum tercatat",
            _clock(receipt.get("clock")), receipt.get("place") or "tempat belum tercatat")
        if notes and notes.get(receipt.get("id")):
            line += " " + notes[receipt.get("id")]
        size = len(line.encode("utf-8"))
        if out and used + size > max_bytes:
            break
        out.append(line)
        used += size
    return out


def mailbox_sighting(w, today, count):
    """Her latest sighting today of the native mailbox flag, while the game still draws it.

    The flag is drawn while the native queue is non-empty; only that boolean is
    used, never the hidden count or any letter body. Not seen today: nothing.
    """
    if today is None or type(count) is int and count <= 0:
        return None
    seen = [r for r in w.matching("mailbox_visible", text=False) if _day(r) == today]
    if not seen:
        return None
    return max(seen, key=lambda r: r.get("clock") if type(r.get("clock")) is int else -1)


def mailbox_line(receipt, count):
    where = "%s %s" % (receipt.get("place") or "tempat belum tercatat",
                       str(receipt.get("id") or "").split(":", 1)[-1])
    when = "%s %s" % ((receipt.get("game_day") or {}).get("text") or "tanggal belum tercatat", _clock(receipt.get("clock")))
    if type(count) is int:
        return "Kotak suratmu (%s) bertanda surat; terlihat %s." % (where, when)
    return "Tanda surat terlihat di kotak suratmu (%s), %s." % (where, when)


def mailbox_row_note(receipt):
    return "kotak suratmu bertanda surat (terlihat %s)" % _clock(receipt.get("clock"))
