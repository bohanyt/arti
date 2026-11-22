"""Compact native bundle understanding; no action selection (ADR 58).

FIX75B (ADR 129): every line here is a native fact, and the weight is bounded too. Nothing is sorted so that a bundle fit comes
first; bundles, chest stacks and forage keep the game's or her own order, and a cut says how many more there are. Outside the
Community Center, the bundle menu and a goal of her own that names it, one `day` line at most names a bundle (``settle``); the
rest stays one read away in ``day.bundle_rinci``.
"""
from collections.abc import Mapping
import json

# Native 1.6.15 CommunityCenter.shouldNoteAppearInArea (CommunityCenter.cs:444-507): how many bundles must be complete, in all,
# before a room's note appears. A room with nothing unfinished has no note. The Joja room hangs on an event and is not listed.
ROOM_OPENS_AFTER = {"Crafts Room": 0, "Pantry": 1, "Fish Tank": 1, "Boiler Room": 2, "Bulletin Board": 3, "Vault": 4}
OPEN_LINE_CHARS = 480       # the room's six bundles nearly fit, in native order; a cut says how many more there are
OPEN_REFLECTION_ROWS = 2    # FIX75B: the rooms and what opens the rest, then one row of bundles (was 4 of the 20 bedtime rows)
BEDTIME_ROWS = 4            # FIX75B: bundle rows at bedtime, in all; the Community Center is one of five standing yardsticks there
ROW_CHARS = 140             # a bedtime row (arti_mind_reflect.progress_facts cuts there)
OPENS_LATER = "terbuka setelah bundle selesai: "
STORED_USES_CHARS = 200
READ_BLOCK = "bundle_rinci"  # FIX75B: the `day` key the renderer always keeps one read away (arti_stardew_skills.READ_ONLY_DAY)
GOAL_WORDS = ("bundle", "bundel", "community center", "junimo")


def open_rooms(cc):
    """{room: open now} from the native bundle read and the native rule; a native read of a room outranks the rule. None = unknown."""
    rows = [b for b in cc.get("bundles") or () if isinstance(b, Mapping)] if isinstance(cc, Mapping) else []
    if not rows or cc.get("joja") or cc.get("cc_complete"):
        return None
    done = sum(1 for b in rows if b.get("complete"))
    unfinished = {b.get("room") for b in rows if not b.get("complete")}
    rooms = {room: done >= after and room in unfinished for room, after in ROOM_OPENS_AFTER.items()}
    for match in cc.get("bag_matches") or ():
        if isinstance(match, Mapping) and match.get("room") in rooms and type(match.get("room_open")) is bool:
            rooms[match["room"]] = match["room_open"]
    return rooms


def reward_label(text):
    """FIX79C: a room reward as the note shows it, without the UI's "Reward: " label; the font's heart glyph "<" reads "hati"."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    text = text[len("Reward: "):] if text.startswith("Reward: ") else text
    return (text[:-1].rstrip() + " (hati)" if text.endswith("<") else text) or None


def reward_suffix(row):
    """FIX79C (ADR 141): " -> hadiah X xN", the bundle's own native reward (Data/Bundles via the mod's ``reward_text``), or ''."""
    text = row.get("reward_text") if isinstance(row, Mapping) else None
    if not isinstance(text, str) or not text or text.startswith(("unknown", "none")):
        return ""
    return " -> hadiah " + text[:40]


def _need(need, stacks, stored=None):
    name, quantity, quality = need
    held = stacks.get(name) if stacks else None
    kept = stored.get(name) if stored else None
    return "%s%s%s%s%s" % (name, " x%d" % quantity if type(quantity) is int and quantity > 1 else "",
                           " q%d" % quality if type(quality) is int and quality > 0 else "", " [tas %d]" % held if held else "",
                           " [peti %d]" % kept if kept else "")


def stored_uses(cc, stored):
    """FIX72D (a53 d5 J1358 "[recorded remark omitted]", then in the chest from J1767 and on no page or bedtime row again):
    what a bundle asks for among what her chests held, each with its room's state. A count below the need stays a count, and
    a quality the chest read does not hold is not claimed. Facts only; offers nothing.
    FIX75B: the stacks come in the chest's own order (the order she put them away), never fits first; one claim per stack is
    the most exact one the native rows allow (a whole fit before a count, an open room before a shut one)."""
    rooms = open_rooms(cc)
    if not rooms or not stored:
        return None
    claims = {}  # item name -> (rank, text)
    for row in cc["bundles"]:
        if not isinstance(row, Mapping) or row.get("complete"):
            continue
        is_open = rooms.get(row.get("room")) is True
        shut = "" if is_open else " (ruangnya belum terbuka)"
        for need in row.get("needs", ()):
            if not isinstance(need, list) or len(need) != 3 or not stored.get(need[0]):
                continue
            name, quantity, quality = need
            count = stored[name]
            if type(quality) is int and quality > 0:
                fits, text = False, "%s x%d: bundle %s minta q%d%s" % (name, count, row.get("name"), quality, shut)
            elif type(quantity) is int and count < quantity:
                fits, text = False, "%s x%d: bundle %s minta x%d%s" % (name, count, row.get("name"), quantity, shut)
            else:
                fits, text = True, "%s x%d: cocok bundle %s%s" % (name, count, row.get("name"), shut)
            rank = (not fits, not is_open)
            if name not in claims or rank < claims[name][0]:
                claims[name] = (rank, text)
    parts = [claims[name][1] for name in stored if name in claims]
    if not parts:
        return None
    kept, room = [], STORED_USES_CHARS - len("Di peti: ") - len("; +99 lagi")
    for part in parts:
        if len(part) + 2 > room:
            break
        kept.append(part)
        room -= len(part) + 2
    rest = len(parts) - len(kept)
    return "Di peti: " + "; ".join(kept + (["+%d lagi" % rest] if rest else []))


def open_fits(cc):
    """{item name: "cocok bundle X"} for what an open room's unfinished bundles still ask; a stricter need says what it asks. FIX73E:
    the same native read as the `cc_terbuka` line, so a shut or unread room claims nothing."""
    rooms = open_rooms(cc)
    fits = {}
    for row in (cc.get("bundles", ()) if rooms else ()):
        if not isinstance(row, Mapping) or row.get("complete") or rooms.get(row.get("room")) is not True:
            continue
        for need in row.get("needs", ()):
            if not isinstance(need, list) or len(need) != 3 or not isinstance(need[0], str):
                continue
            name, quantity, quality = need
            if type(quality) is int and quality > 0:
                text = "bundle %s minta q%d" % (row.get("name"), quality)
            elif type(quantity) is int and quantity > 1:
                text = "bundle %s minta x%d" % (row.get("name"), quantity)
            else:
                fits[name] = "cocok bundle %s" % row.get("name")  # a plain fit outranks a stricter row for the same item
                continue
            fits.setdefault(name, text)
    return fits


def _open_parts(cc, stacks=None, stored=None):
    """(open room names, bundle texts in native order with what she holds marked, what opens the rest + native limits), or None.
    FIX75B: no fits-first sort; the bundles come as the game lists them and a cut says how many more there are."""
    rooms = open_rooms(cc)
    named = [room for room, is_open in (rooms or {}).items() if is_open]
    if not named:
        return None
    done = sum(1 for b in cc["bundles"] if isinstance(b, Mapping) and b.get("complete"))
    texts = []
    for row in cc["bundles"]:
        if isinstance(row, Mapping) and not row.get("complete") and row.get("room") in named:
            needs = [n for n in row.get("needs", ()) if isinstance(n, list) and len(n) == 3]
            texts.append("%s %s/%s (%s)%s" % (row.get("name"), row.get("done"), row.get("slots"),
                                              ", ".join(_need(n, stacks, stored) for n in needs), reward_suffix(row)))
    later = ["%s %d" % (room, after) for room, after in ROOM_OPENS_AFTER.items() if after > done and rooms[room] is False]
    tail = [OPENS_LATER + ", ".join(later)] if later else []
    if cc.get("unlocked") is False:
        tail.append("pintu Community Center masih terkunci")
    if cc.get("can_read_junimo_text") is False:
        tail.append("tulisan Junimo belum terbaca")
    return named, texts, tail


def open_room_line(cc, stacks=None, stored=None):
    """FIX71D (a52: 385 of 389 fit lines on pages offering the trip named a shut room; no page named the open one): which rooms
    are open, what their unfinished bundles still need, how many of each she holds by name (``stacks``), and how many complete
    bundles open the rest. Facts only; offers nothing. A count below the need stays a count, never a fit."""
    parts = _open_parts(cc, stacks, stored)
    if parts is None:
        return None
    named, texts, tail = parts
    head = ", ".join(named) + " terbuka: "
    room = OPEN_LINE_CHARS - len(head) - len("; ".join(tail)) - 20  # 20: "; " before the tail and the count of the rest
    kept = []
    for text in texts:
        if len(text) + 2 > room:
            break
        kept.append(text)
        room -= len(text) + 2
    rest = ["+%d bundle lagi" % (len(texts) - len(kept))] if len(kept) < len(texts) else []
    return head + "; ".join(kept + rest + tail)


def signature(cc):
    """FIX75B: what a player notices changing in the Community Center: which rooms are open and each bundle's count. Her own bag
    and chest counts are not part of it. None without a native read."""
    rooms = open_rooms(cc)
    if rooms is None:
        return None
    return json.dumps([sorted(room for room, is_open in rooms.items() if is_open),
                       [[b.get("id"), b.get("done"), bool(b.get("complete"))] for b in cc["bundles"] if isinstance(b, Mapping)]],
                      separators=(",", ":"))


def own_goal_names(texts, cc=None):
    """FIX75B: True when a goal of her own names the Community Center, a bundle, or one of this read's bundles by name."""
    words = list(GOAL_WORDS)
    if isinstance(cc, Mapping):
        words += [b["name"].lower() for b in cc.get("bundles") or () if isinstance(b, Mapping) and isinstance(b.get("name"), str)]
    return any(word in text.lower() for text in texts or () if isinstance(text, str) for word in words)


def settle(day, *, free, changed, moved_on=True):
    """FIX75B (ADR 129): the page's bundle weight. ``free`` = she is in the Community Center, on the bundle menu, or a goal of
    her own names it: every line stays. Otherwise one `day` line at most names a bundle: her bag's own fit line (`tas_kegunaan`,
    ADR 88) or, when the native state changed since she last saw it (``changed``), the room line in full. `cc_terbuka` (unchanged),
    `peti_kegunaan` and the forage fits move to ``day[READ_BLOCK]``, which the renderer keeps one read away. `forage_ingat`
    rides only when the map changed (``moved_on``), free or not. Returns what moved; no fact is dropped."""
    if not isinstance(day, dict):
        return {}
    rinci = {}
    if "forage_ingat" in day and not moved_on:
        rinci["forage_ingat"] = day.pop("forage_ingat")
    if not free:
        if "cc_terbuka" in day and not changed:
            rinci["cc_terbuka"] = day.pop("cc_terbuka")
        if "peti_kegunaan" in day:
            rinci["peti_kegunaan"] = day.pop("peti_kegunaan")
        from arti_runtime.forage_memory import split_fits
        for key, label in (("forage_terlihat", "di sini"), ("forage_ingat", "musim ini")):
            if isinstance(day.get(key), str):
                bare, fit = split_fits(day[key])
                if fit:
                    day[key] = bare
                    rinci.setdefault("forage_cocok", {})[label] = fit
    if rinci:
        day[READ_BLOCK] = dict(day.get(READ_BLOCK) or {}, **rinci)
    return rinci


def present(cc):
    bundles = {b['id']: b for b in cc.get('bundles', ()) if isinstance(b, dict) and 'id' in b}
    rooms = {r['room']: r for r in cc.get('room_rewards', ()) if isinstance(r, dict) and 'room' in r}
    matches = sorted(cc.get('bag_matches') or (), key=lambda m: (
        m.get('room_open') is not True,
        (bundles.get(m.get('bundle'), {}).get('slots', 99) - bundles.get(m.get('bundle'), {}).get('done', 0)),
        m.get('bundle', 999), m.get('slot', 999), m.get('ingredient', 999)))
    top = []
    for m in matches[:3]:
        b = bundles.get(m.get('bundle'), {})
        room = rooms.get(m.get('room') or b.get('room'), {})
        text = 'slot %s %s x%s -> %s ingredient %s q%s (room_open=%s)' % (
            m.get('slot'), m.get('name'), m.get('quantity'), m.get('bundle_name'),
            m.get('ingredient'), m.get('quality'), m.get('room_open'))
        if b:
            text += '; %s/%s; bundle reward %s' % (b.get('done'), b.get('slots'), b.get('reward_text', b.get('reward', 'unknown')))
        if room:
            text += '; room completion -> ' + room.get('effect', room['reward'])
        top.append(text)
    compact = dict(bundle_bag=top, bundle_matches=cc.get('bag_matches_total', len(matches)))
    if matches:
        compact['bundle_match_scope'] = 'Zero-based bag/ingredient indices; independent fits, not simultaneous allocation or donation credit.'
    # Standing purpose remains readable even when the current bag has no matches.
    if rooms:
        compact['room_rewards'] = [[r['room'], r['reward']] for r in list(rooms.values())[:6]]
    details = {k: cc[k] for k in ('bundles', 'room_rewards', 'junimo_note') if k in cc}
    return compact, details


def _open_rows(cc):
    """The same facts as bedtime rows: the rooms and what opens the rest, then their bundles in native order. FIX75B: at most
    OPEN_REFLECTION_ROWS rows, and a cut says how many bundles did not fit instead of dropping them in silence."""
    parts = _open_parts(cc)
    if parts is None:
        return []
    named, texts, tail = parts
    lines, line = ["; ".join([", ".join(named) + " terbuka"] + tail)], ""
    more = "; +%d bundle lagi"
    for i, text in enumerate(texts):
        last = len(lines) == OPEN_REFLECTION_ROWS - 1
        room = ROW_CHARS - (len(more % (len(texts) - i)) if last else 0)
        if line and len(line) + len(text) + 2 > room:
            if last:
                line += more % (len(texts) - i)
                break
            lines.append(line)
            line = ""
        line = line + "; " + text if line else text
    if line:
        lines.append(line)
    return [dict(ref="p:cc-open%d" % i, text=text[:ROW_CHARS]) for i, text in enumerate(lines[:OPEN_REFLECTION_ROWS])]


def reflection(compact, clock, cc=None, stored=None):
    """Bedtime rows about the Community Center, BEDTIME_ROWS at most: the open rooms and their bundles, then what her chests hold
    for them (`p:peti-cc`), then the observed bag match and the room rewards (ADR 58). FIX75B: one bounded family of rows."""
    rows = _open_rows(cc) if cc else []
    if use := stored_uses(cc, stored):
        rows.append(dict(ref="p:peti-cc", text=use[:ROW_CHARS]))
    if compact:
        # Absolute observation time, not an assertion that the old bag is still current at bedtime.
        if compact.get('bundle_bag'):
            match = compact['bundle_bag'][0]
            eligibility, *rest = match.split('; ')
            eligibility = eligibility.split(' (room_open=')[0]
            date = '%s @%s' % ((clock or {}).get('date', '?'), (clock or {}).get('time', '?'))
            shut = ' (ruangnya belum terbuka)' if ' (room_open=False)' in match else ''  # FIX71D: as her page says it
            rows.append(dict(ref='p:cc-match', text='Observed %s: %s%s' % (date, eligibility, shut)))
            if rest:
                rows.append(dict(ref='p:cc-match-reward', text='Same observed bundle: ' + '; '.join(rest[:2])))
        rewards = compact.get('room_rewards') or []
        for i in range(0, len(rewards), 2):
            rows.append(dict(ref='p:cc-rewards%d' % i,
                             text='CC completion rewards: ' + '; '.join('%s: %s' % tuple(r) for r in rewards[i:i+2])))
    return rows[:BEDTIME_ROWS]
