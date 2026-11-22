"""Native social facts for her decision page (FIX79C, ADR 141). Facts only; whom she visits is her choice.

Every number here is the game's own rule (Stardew Valley 1.6.15 decompile, read-only, not in this repository):
- ``NPC.friendshipPointsPerHeartLevel`` = 250 (NPC.cs:68).
- the first talk of a day gives +20 (``NPC.grantConversationFriendship``, NPC.cs:2924-2938);
- a day without a talk takes 2 points (``Farmer.resetFriendshipsForNewDay``, Farmer.cs:4130-4133); a58 lost Leah 14 and
  Pierre 14 this way, 0 talks in 27 days;
- a gift: loved +80, liked +45, neutral +20, disliked -20, hated -40, eight times on the villager's birthday, one a day
  and two a week (``NPC.receiveGift`` NPC.cs:4899-4974, ``NPC.tryToReceiveActiveObject`` NPC.cs:2384, 2398).
What a rising friendship opens comes from native rows only: Grandpa's ``friends_8_hearts`` criterion (the mod's mirror of
``Utility.getGrandpaScore``) and the Bulletin Board's room reward (``Strings/UI:JunimoNote_RewardBulletin``).

Who is where: a farmhand's game holds the characters of her own map only, so ``here_line`` names the villagers on her map
now (scene facts: ``villager``, ``talked_today``), never a guessed schedule elsewhere.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Mirror of the mod's BgNpcTalk.ExcludedNames: the game offers them no talk.
from arti_stardew_interact_b import EXCLUDED_NPCS

POINTS_PER_HEART = 250
FRIENDS_SHOWN = 3        # villagers she knows, by points; a cut says how many more (eight in progress_details.friends)
HERE_SHOWN = 6           # villagers on her map now; a cut says how many more
# On the page (short); the gift table rides one read away in `progress_details.teman_aturan`.
RULES = "250 poin = 1 hati; ngobrol pertama tiap hari +20; hari tanpa ngobrol -2; hadiah -40..+80, x8 saat ulang tahun"
RULES_DETAIL = ("hadiah: disukai sekali +80, disukai +45, biasa +20, nggak disukai -20, dibenci -40; x8 saat ulang tahun; "
                "1 hadiah sehari, 2 seminggu")


def hearts(points) -> int | None:
    return points // POINTS_PER_HEART if type(points) is int and points >= 0 else None


def friends_line(friends: Mapping | None, *, limit: int = FRIENDS_SHOWN) -> str | None:
    """Each villager she knows with hearts and points, closest first (points, then name), or None."""
    rows = sorted(((n, p) for n, p in (friends or {}).items() if isinstance(n, str) and type(p) is int), key=lambda r: (-r[1], r[0]))
    if not rows:
        return None
    parts = ["%s %d hati (%d poin)" % (n, hearts(p) or 0, p) for n, p in rows[:limit]]
    return "%d kenal: %s%s" % (len(rows), ", ".join(parts), "; +%d lagi" % (len(rows) - limit) if len(rows) > limit else "")


def unlock_line(grandpa: Mapping | None, room_rewards: Iterable = (), rooms_done: Iterable = ()) -> str | None:
    """What a rising friendship opens, from native rows only: Grandpa's friends criterion and the Bulletin Board reward."""
    parts = []
    row = next((c for c in (grandpa or {}).get("criteria") or () if isinstance(c, list) and len(c) >= 4
                and c[0] == "friends_8_hearts"), None)
    if row is not None:
        parts.append("evaluasi Kakek: %s (%s/%s poin)" % (row[3] if isinstance(row[3], str) and row[3] else row[0], row[1], row[2]))
    done = {r for r in rooms_done if isinstance(r, str)}
    board = next((r for r in room_rewards if isinstance(r, Mapping) and r.get("room") == "Bulletin Board"), None)
    from arti_runtime.bundle_knowledge import reward_label
    if board is not None and (label := reward_label(board.get("reward"))):
        parts.append("ruang Bulletin Board selesai: %s%s" % (label, " (sudah)" if "Bulletin Board" in done else ""))
    return "; ".join(parts) or None


def _birthdays_today(calendar, day, game) -> set[str]:
    from arti_runtime.plan_dates import ordinal
    out = set()
    for e in calendar or ():
        if (isinstance(e, Mapping) and e.get("kind") == "birthday" and isinstance(e.get("id"), str)
                and any(ordinal(game, d) == day for d in e.get("dates") or ())):
            out.add(e["id"].split(":", 1)[-1].casefold())
    return out


def villagers_here(targets: Mapping | None) -> list[dict]:
    """Talkable villagers on her map now, from the scene's native NPC facts (any distance on the map)."""
    out = []
    for ref, f in (targets or {}).items():
        if not isinstance(f, Mapping) or f.get("kind") != "npc" or f.get("villager") is not True:
            continue
        internal = str(f.get("internal_name") or "")
        if internal.casefold() in EXCLUDED_NPCS or not isinstance(f.get("tile"), Mapping):
            continue
        out.append(dict(ref=ref, internal=internal, name=str(f.get("display_name") or internal),
                        talked_today=f.get("talked_today") is True))
    return sorted(out, key=lambda r: r["name"])


def _clock(hhmm) -> str | None:
    return "%02d:%02d" % (hhmm // 100 % 24, hhmm % 100) if type(hhmm) is int and 0 <= hhmm <= 2800 else None


def here_line(targets: Mapping | None, friends: Mapping | None = None, *, calendar=(), day=None, game="stardew",
              limit: int = HERE_SHOWN, place: str | None = None, clock=None, routes=None) -> str | None:
    """The villagers on her map now: talked to today or not, hearts when she knows them, a birthday today. None if none.

    FIX81B: the old line did not say which map; with ``place``/``clock`` the line names the map and the read's
    game time ("[recorded remark omitted]")."""
    rows = villagers_here(targets)
    if not rows:
        return None
    born = _birthdays_today(calendar, day, game) if type(day) is int else set()
    known = {k.casefold(): v for k, v in (friends or {}).items() if isinstance(k, str) and type(v) is int}
    parts = []
    for r in rows[:limit]:
        bits = ["sudah disapa hari ini" if r["talked_today"] else "belum disapa hari ini"]
        if r["internal"].casefold() in known:
            bits.append("%d hati" % hearts(known[r["internal"].casefold()]))
        else:
            bits.append("belum kenal")
        if r["internal"].casefold() in born:
            bits.append("ulang tahun hari ini")
        if routes and routes.get(r["name"]):
            bits.append(routes[r["name"]])
        parts.append("%s (%s)" % (r["name"], ", ".join(bits)))
    rest = "; +%d lagi" % (len(rows) - limit) if len(rows) > limit else ""
    head = ("di %s sekarang%s: " % (place, ", " + _clock(clock) if _clock(clock) else "")) if place else "di peta ini sekarang: "
    return head + "; ".join(parts) + rest


# FIX81B (ADR 145): in a recorded run she planned a visit while that villager stood on her own map; on arrival the page had
# no villager line at all. A farmhand's game holds only her own map's characters, so where a villager is off her map is
# unknown; where she last SAW each one today is a dated native observation. One entry per map, the latest read of it.
SEEN_PLACES = 2   # the most recent maps with villagers seen there today
SEEN_NAMES = 8    # names per map; a cut says how many more


def remember_seen(seen: dict, location, place, date, clock, targets) -> None:
    """Record who is on her map now (scene villager facts) under that map; a name seen here leaves older maps."""
    if not isinstance(seen, dict) or not isinstance(location, str) or not location or type(clock) is not int:
        return
    names = [r["name"] for r in villagers_here(targets)]
    for key in [k for k in seen if k != location]:
        seen[key]["names"] = [n for n in seen[key]["names"] if n not in names]
        if not seen[key]["names"]:
            del seen[key]
    if names:
        seen[location] = dict(place=place or location, date=list(date or ()), clock=clock, names=names)
    else:
        seen.pop(location, None)


def seen_line(seen: Mapping | None, location, date) -> str | None:
    """``terakhir terlihat hari ini (bukan posisi sekarang): kota (Town) 16:20: Jas, Lewis``; None when nothing applies."""
    rows = sorted((r for k, r in (seen or {}).items() if k != location and isinstance(r, Mapping)
                   and list(r.get("date") or ()) == list(date or ()) and r.get("names")),
                  key=lambda r: -r["clock"])[:SEEN_PLACES]
    if not rows:
        return None
    parts = []
    for r in rows:
        names = r["names"]
        more = ", +%d lagi" % (len(names) - SEEN_NAMES) if len(names) > SEEN_NAMES else ""
        parts.append("%s %s: %s%s" % (r["place"], _clock(r["clock"]) or "?", ", ".join(names[:SEEN_NAMES]), more))
    return "terakhir terlihat hari ini (bukan posisi sekarang): " + "; ".join(parts)


# FIX81B (ADR 145): the talk routine's own name grammar (interact_b.parse_routine_objective, "ngobrol dengan <name>").
_TALK_NAME = re.compile(r"[A-Za-z][A-Za-z' .\-]{1,23}")
ANY_TALK = "warga"   # the argless talk as a listed arg: any talkable villager the routine reaches ("ngobrol dengan warga")
TALK_DOES = "Ngobrol saja, tanpa hadiah, sampai dialog tertutup; arg = warga yang diajak (warga = siapa saja terdekat)."


def talk_args(targets: Mapping | None, *, limit: int = HERE_SHOWN) -> list[str]:
    """FIX81B (a recorded run: the argless talk reached another villager): one talk arg per talkable
    villager on her map not talked to today, the same set the talk routine would walk to, by the name it matches; at most
    ``limit`` (by name, as `day.warga` lists them). Every villager past the cut stays reachable through ANY_TALK."""
    out = []
    for r in villagers_here(targets):
        if r["talked_today"]:
            continue
        name = next((n for n in (r["name"], r["internal"]) if _TALK_NAME.fullmatch(n)), None)
        if name and name not in out and name.casefold() != ANY_TALK:
            out.append(name)
    return out[:limit]


# FIX84B (ADR 149, a58-a61: ngobrol_hadiah's gift form admitted, never offered, 0 gifts in ten sessions). Live7 listed only talk
# args, so `<barang> ke <warga>` never reached her page; and the gift leg judged the item with the legacy sell allowlist (crops
# only), which refuses Stone, ore or seeds she actually carries. A gift is offered for each held item the game can take as one,
# to each villager the talk args name; the mod's own capture (ModEntry.Gift.cs) stays the authority at the press.
GIFT_ARGS_MAX = 6    # item x villager pairs on one page, bag order then villager order (a61 replay: 12 displaced 5 facts)
# Mirror of ModEntry.Gift.cs CaptureGiftKind: furniture / big craftables / special items the capture refuses.
GIFT_SPECIAL = frozenset({"(O)StardropTea", "(O)458", "(O)460", "(O)808", "(O)809", "(O)897", "(O)277", "(O)233", "(O)71",
                          "(O)867", "(O)864", "(O)865", "(O)866", "(O)868", "(O)869", "(O)870"})
_GIFT_ITEM = re.compile(r"[a-z][a-z0-9 '-]{0,39}")                  # extras "kasih hadiah <item> ke <name>" grammar
_GIFT_NAME = re.compile(r"[a-z][a-z'-]{0,19}(?: [a-z][a-z'-]{0,19})?")
GIFT_DOES = (" '<barang> ke <warga>' = ngobrol lalu beri 1 barang itu; selera belum terbaca; tidak di hari ulang tahunnya; "
             "maks 6 pasang, urut tas.")


def gift_refusal(row: Mapping) -> str | None:
    """Why a bag row cannot be offered as a gift, or None: tools/quest items (owner B's denylist), non-objects and the
    capture's special list. Whether the game takes it is the mod's capture at the press."""
    from arti_stardew_economy import hard_dispose_refusal
    qid = str(row.get("qualified_item_id") or "")
    if (refusal := hard_dispose_refusal(row)) is not None:
        return refusal
    if not qid.startswith("(O)"):
        return "not_object"
    return "special" if qid in GIFT_SPECIAL else None


def gift_items(inventory) -> list[str]:
    """Bag names a gift arg can carry, in bag order: giftable, in exactly one slot (the gift leg needs one), in the grammar."""
    rows = [r for r in inventory or () if isinstance(r, Mapping)]
    names = [str(r.get("name") or r.get("display_name") or "") for r in rows]
    out = []
    for row, name in zip(rows, names):
        if (name and gift_refusal(row) is None and _GIFT_ITEM.fullmatch(name.casefold())
                and sum(n.casefold() == name.casefold() for n in names) == 1 and name not in out):
            out.append(name)
    return out


def gift_args(villagers: Iterable[str], inventory, *, birthdays: Iterable[str] = (), limit: int = GIFT_ARGS_MAX) -> tuple[list[str], int]:
    """(`<barang> ke <warga>` args, pairs left out by ``limit``) for villagers she may still talk to today (the talk leg runs
    first). A villager whose birthday is today is left out: the mod refuses that gift (GiftRules.Miss), the page says so."""
    born = {b.casefold() for b in birthdays if isinstance(b, str)}
    who = [v for v in villagers if v != ANY_TALK and v.casefold() not in born and _GIFT_NAME.fullmatch(v.casefold())]
    pairs = ["%s ke %s" % (item, v) for item in gift_items(inventory) for v in who]
    return pairs[:limit], max(0, len(pairs) - limit)


# FIX91B (ADR 161, a65 d2 J510-J556): right after her verified talk with Linus the scene still said `talked_today: null`
# (the native flag caught up about a minute later, J561), so the page offered Linus and `warga` again and the routine,
# reading the newer flag, found nobody: `talk_target_not_offered`. Her own verified talk is a native fact too (the
# dialogue's speaker, then dialogue_closed); it marks that villager greeted for the rest of that game day.
def mark_greeted(ledger: dict, name, date) -> None:
    """Record her verified talk with ``name`` on game ``date`` (one entry per villager; a later day replaces it)."""
    if isinstance(ledger, dict) and isinstance(name, str) and name.strip() and date:
        ledger[name.strip().casefold()] = tuple(date)


def greeted_names(ledger: Mapping | None, date) -> frozenset:
    """Villagers (casefolded names) she talked to on game ``date``, from her own verified talks."""
    return frozenset(n for n, d in (ledger or {}).items() if date and tuple(d) == tuple(date))


def _greeted(fact: Mapping, names) -> bool:
    return any(str(fact.get(k) or "").casefold() in names for k in ("internal_name", "display_name") if fact.get(k))


def with_greeted(targets: Mapping | None, names) -> dict:
    """The scene facts with ``talked_today`` true for each villager in ``names`` (her verified talks today); a native
    true is never undone. Every reader of who is new (offer, `day.warga`, route pause) sees the same day."""
    out = dict(targets or {})
    for ref, f in out.items():
        if isinstance(f, Mapping) and f.get("kind") == "npc" and f.get("talked_today") is not True and _greeted(f, names):
            out[ref] = dict(f, talked_today=True)
    return out


def nearest_villager(targets: Mapping | None, here, greeted=()) -> dict | None:
    """FIX91B (ADR 161): what `warga` means at the start of a talk: the nearest talkable villager on her map she has not
    talked to today, by tile distance, as ``{ref, name}`` with the name the talk routine matches. None if nobody."""
    if not (isinstance(here, (tuple, list)) and len(here) == 2 and all(type(v) is int for v in here)):
        return None
    names = frozenset(greeted or ())
    best = None
    for r in villagers_here(with_greeted(targets, names) if names else targets):
        if r["talked_today"]:
            continue
        name = next((n for n in (r["name"], r["internal"]) if _TALK_NAME.fullmatch(n)), None)
        tile = (targets or {}).get(r["ref"], {}).get("tile") or {}
        if name is None or name.casefold() == ANY_TALK or type(tile.get("x")) is not int or type(tile.get("y")) is not int:
            continue
        key = (max(abs(tile["x"] - here[0]), abs(tile["y"] - here[1])), name)
        if best is None or key < best[0]:
            best = (key, dict(ref=r["ref"], name=name))
    return best[1] if best else None


def talk_open(targets: Mapping | None) -> bool:
    """ADR 156: the talk routine's own accepted scene entries, at its map-wide reach."""
    from arti_stardew_interact_b import approach_entries, parse_routine_objective
    eligible = {r["ref"]: targets[r["ref"]] for r in villagers_here(targets) if not r["talked_today"]}
    return bool(approach_entries(eligible, (0, 0), parse_routine_objective("ngobrol dengan warga"), npc_reach=None))


def talk_route_open(targets: Mapping | None, here, grid, name) -> bool:
    """ADR 195: a named talk offer needs the villager on this map and a native route now.

    A missing pose or nav grid cannot prove they are unreachable, so the offer stays.
    A grid whose start cell is not open is unknown, not a withhold. Already beside them is a route.
    """
    who = str(name or "").casefold()
    tile = None
    for row in villagers_here(targets):
        if who in (row["name"].casefold(), row["internal"].casefold()):
            raw = ((targets or {}).get(row["ref"]) or {}).get("tile") or {}
            if type(raw.get("x")) is int and type(raw.get("y")) is int:
                tile = (raw["x"], raw["y"])
            break
    if tile is None:
        return False
    if not (isinstance(here, (tuple, list)) and len(here) == 2 and all(type(v) is int for v in here)):
        return True
    if not grid:
        return True
    hx, hy = int(here[0]), int(here[1])
    stands = [(tile[0] + dx, tile[1] + dy) for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0))]
    if (hx, hy) in stands or (hx, hy) == tile:
        return True
    if grid.get((hx, hy)) != ".":
        return True
    if any(stand not in grid for stand in stands if stand[0] >= 0 and stand[1] >= 0):
        return True  # a local window cannot disprove a map-wide approach
    from arti_stardew_goal_routes import open_paths
    parents = open_paths(grid, (hx, hy))
    if any(stand in parents for stand in stands):
        return True
    # Even an in-window NPC may need a detour outside this window. Only a
    # completely observed closed component disproves a route.
    return any(x+dx >= 0 and y+dy >= 0 and (x+dx,y+dy) not in grid
               for x,y in parents for dx,dy in ((0,-1),(1,0),(0,1),(-1,0)))
