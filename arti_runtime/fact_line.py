"""ADR 220: trial presentation follows native display receipts, not loaded data.

Admission, routing, safety and authored memory remain with their existing owners.
This projection runs before the normal/diet/pager/detail/emergency render paths.
Text capture belongs to the native readers; a receipt never invents native prose.
"""
from collections.abc import Mapping
from arti_runtime.native_rows import native_rows
from copy import deepcopy
import re

SETTING = "live7_fact_line_enabled"

# The audit's REMOVE list. These are authored conclusions or hidden arithmetic,
# not native statements awaiting a better paraphrase.
DAY_REMOVE = ("tas_kegunaan", "peti_kegunaan", "day_rules", "musim", "benih_tumbuh",
              "tempat", "cc_terbuka", "bundle_rinci", "bedtime_return", "festival_leave", "quest_lanjut")
PROGRESS_REMOVE = ("tamat", "teman_aturan", "teman_membuka", "teman_hati", "teman",
                   "cc_hadiah", "donation_facts", "grandpa", "stats", "money_earned",
                   "money_earned_self", "uang_dihasilkan", "skill", "skills", "alat",
                   "tools", "tool_names", "tool_upgrading", "sejak_pagi", "cc", "crops")
DETAIL_REMOVE = ("bundles", "rooms_done", "grandpa", "friends", "teman_aturan", "tools",
                 "stats", "donations", "quest_facts", "quests", "letters")


def enabled(config):
    return isinstance(config, Mapping) and config.get(SETTING) is True


def _absolute(value):
    from arti_runtime.plan_dates import ordinal
    if isinstance(value, str):
        got = ordinal("stardew", value)
        if got:
            return got
        match = re.match(r"^(\d+):(spring|summer|fall|winter):(\d+)(?: |$)", value)
        if match:
            return ordinal("stardew", "Y%s %s %s" % match.groups())
    return None


def _name_pattern(name):
    """A native place name as whole words; a line wrap in displayed text matches its space (ADR 227)."""
    parts = name.split()
    if not parts:
        return re.compile(r"(?!)")  # a blank name names nothing
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(part) for part in parts) + r"(?!\w)", re.I)


def _drop(row, keys):
    if isinstance(row, dict):
        for key in keys:
            row.pop(key, None)


def _rows(value):
    return native_rows(value)


def visible_targets(context):
    """Actual current native viewport, never same-map membership or guessed radius."""
    catalog = context.get("world_knowledge") or {}
    bounds = catalog.get("visible_tiles")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4 or not all(type(n) is int for n in bounds):
        return {}
    left, top, right, bottom = bounds
    if (catalog.get("world_graph") or {}).get("current") != context.get("location_id"):
        return {}
    out = {}
    for ref, row in (context.get("targets") or {}).items():
        tile = row.get("tile") or row.get("source_tile") or {}
        if isinstance(tile, Mapping):
            x, y = tile.get("x"), tile.get("y")
        elif isinstance(tile, (list, tuple)) and len(tile) == 2:
            x, y = tile
        else:
            continue
        if type(x) is int and type(y) is int and left <= x <= right and top <= y <= bottom:
            out[ref] = row
    return out


class Witnesses:
    """A read-only index over the shared scoped first-seen records and native text.

    A save flag proves identity only. Structured metadata, calendar file entries,
    acquired-item descriptions and any-board-read are never text witnesses.
    """
    def __init__(self, catalog=None):
        from arti_runtime.experience_facts import experiences
        self.catalog = catalog or {}
        self.events = experiences(self.catalog)
        # ADR 228 part C: story lines kept from retiring story rows are long memory (digest, planner); the
        # live page keeps its own 7-day story channel, so this index skips their two keep sources.
        self.rows = [e["receipt"] for e in self.events if isinstance(e.get("receipt"), Mapping)
                     and e["receipt"].get("source") not in ("own_screen", "host_screen")]
        # The existing native first-seen ledger already has dated entered-map
        # records. Wrap those valid B observations instead of demanding that a
        # legacy save had been written by the new receipt API.
        for event in self.events:
            if event.get("kind") == "location_entered" and event.get("subject") and (day := _absolute(event.get("date"))):
                from arti_runtime.plan_dates import label
                self.rows.append(dict(v=1, kind="place_entered", id=event["subject"],
                    game_day=dict(absolute=day, text=label("stardew", day)), clock=event.get("clock"),
                    place=event.get("event_place") or event["subject"], source=event.get("source") or "native_visit", first=False))
        self.texts = {}
        for event in self.events:
            # Text is only dereferenced through a receipt; never promoted by itself.
            if isinstance(event.get("text"), str):
                for key in (event.get("id"), event.get("text_ref"), event.get("source"), event.get("observation_ref")):
                    if isinstance(key, str):
                        self.texts[key] = event["text"]
        self.entered = {r["id"]: r for r in self.rows if r.get("kind") == "place_entered" and isinstance(r.get("id"),str) and r["id"]}
        self.current = (self.catalog.get("world_graph") or {}).get("current")

    def on_page(self, batch):
        """ADR 231 part B2: a HUD notice whose whole text is in this page's own game-text block is one she is shown
        now, so this page's notice line and trip label carry it too; its receipt follows when the page is answered.
        Only complete texts; never a slice, a gap or another kind."""
        if not isinstance(batch, Mapping) or not isinstance(batch.get("block"), Mapping):
            return
        block = batch["block"]
        rows = native_rows(block.get("rows", ()))
        primary_whole = (batch.get("start") == 0 and not batch.get("gap") and block.get("sisa_teks_tersimpan_dan_terbaca") == 0
                         and block.get("bagian_belum_diterima") == 0 and block.get("sisa_teks_di_game_belum_terbaca") is False)
        from arti_runtime.plan_dates import label
        from arti_runtime.native_story import day_of
        for index, row in enumerate(rows):
            if (not isinstance(row, Mapping) or row.get("kind") != "hud_notice" or not isinstance(row.get("text"), str)
                    or not row["text"] or not isinstance(row.get("text_ref"), str) or (index == 0 and not primary_whole)):
                continue
            day = day_of(row.get("date"))
            if day is None:
                continue
            if any(r.get("kind") == "hud_notice" and r.get("id") == row.get("id") for r in self.rows):
                continue
            self.rows.append(dict(v=1, kind="hud_notice", id=row.get("id"), game_day=dict(absolute=day, text=label("stardew", day)),
                                  clock=row.get("clock"), place=row.get("place"), source=row.get("source"),
                                  text_ref=row["text_ref"], first=row.get("first") is True))
            self.texts[row["text_ref"]] = row["text"]

    def text(self, receipt):
        ref = receipt.get("text_ref")
        return self.texts.get(ref) if isinstance(ref, str) else None

    def matching(self, kinds, identity=None, *, text=True):
        if isinstance(kinds, str):
            kinds = (kinds,)
        return [r for r in self.rows if r.get("kind") in kinds
                and (identity is None or r.get("id") == identity)
                and (not text or self.text(r))]

    def provenance(self, row):
        names = {"calendar_day_read": "kalender", "letter_opened": "surat",
                 "door_message": "pintu", "shop_menu_seen": "menu toko",
                 "shipping_summary_seen": "ringkasan kiriman", "recipe_seen": "resep",
                 "bundle_note_opened": "catatan Junimo", "place_entered": "kunjungan",
                 "menu_tab_opened": "menu", "item_description_read": "keterangan barang"}
        date = row.get("game_day") or {}
        date = date.get("text", "tanggal belum tercatat") if isinstance(date, Mapping) else str(date)
        if str(row.get("source", "")).startswith("native_save_flags"):
            return "tercatat di save, %s; tanggal lihat belum tercatat" % date
        if row.get("kind") == "calendar_day_read" and (selected := _absolute(row.get("id"))):
            from arti_runtime.plan_dates import label
            return "dilihat di kalender, %s; entri %s" % (date, label("stardew", selected))
        # Keep the native year when it matters; Indonesian connective only.
        return "dilihat di %s, %s" % (names.get(row.get("kind"), "game"), date)

    def line(self, row):
        return "%s (%s)" % (self.text(row), self.provenance(row))

    def names(self):
        sources = dict(self.entered)
        graph = self.catalog.get("world_graph") or {}
        # fix129A: the receipt list does not depend on the place; scan it once.
        # setdefault keeps the first match, so a place already named needs no scan.
        read = [(row, self.text(row)) for row in self.matching((
            "menu_tab_opened", "door_message", "letter_opened", "calendar_day_read",
            "event_watched", "quest_entry_read", "dialogue_read", "text_read"))]
        for place in native_rows(graph.get("places", ())):
            if not isinstance(place,Mapping):continue
            identity = place.get("id")
            if identity == self.current:
                sources.setdefault(identity, None)
            if identity in sources:
                continue
            patterns = [_name_pattern(n) for n in (identity, place.get("label")) if isinstance(n, str) and n]
            for row, text in read:
                if any(p.search(text) for p in patterns):
                    sources[identity] = row
                    break
        return sources

    def display_name(self, place, receipt=None):
        """A sign/map teaches its literal label, not a longer asset-table alias."""
        label = place.get("label") or place.get("id") or "tempat yang belum dikenal"
        if not receipt or receipt.get("kind") == "place_entered":
            return label
        native = self.text(receipt) or ""
        candidates = [n for n in (place.get("label"), place.get("id")) if isinstance(n, str) and n]
        for name in sorted(candidates, key=len, reverse=True):
            if match := _name_pattern(name).search(native):
                return " ".join(match.group(0).split())  # the game's words, its line wrap undone
        return "tempat yang belum dikenal"

    def calendar(self):
        """A native calendar-day receipt releases that entry's name/date only.

        fix122A uses year:season:day as the native hovered-day ID. File hours,
        place tables and the rest of the month never follow this receipt.
        """
        found = []
        for event in native_rows(self.catalog.get("calendar", ())):
            if not isinstance(event, Mapping) or not isinstance(event.get("name"), str):
                continue
            for row in self.matching("calendar_day_read"):
                if event["name"].casefold() not in self.text(row).casefold():
                    continue
                selected = _absolute(row["id"])
                dates = [d for d in native_rows(event.get("dates", ())) if _absolute(d) == selected] if selected else (
                    list(native_rows(event.get("dates", ()))) if event.get("id") == row["id"] else [])
                if dates:
                    found.append(dict(id=event["id"], name=event["name"], dates=dates,
                        **({"kind": event["kind"]} if event.get("kind") else {}), sumber=self.provenance(row), _receipt_only=True))
                    break
        return found


def witnessed_catalog(catalog, config):
    """Narrow the older broad experience projection without changing motor data."""
    if not enabled(config) or not isinstance(catalog, Mapping):
        return catalog
    w = Witnesses(catalog)
    result = dict(catalog, experience_view=True)
    graph = catalog.get("world_graph") or {}
    known = w.names()
    # Entering teaches a name, never every facility, timetable or hidden lock.
    result["world_graph"] = dict(graph, places=[dict(p, facts=[], experience_known=p.get("id") in known,
        label=w.display_name(p, known.get(p.get("id"))) if p.get("id") in known else "tempat yang belum dikenal")
        for p in native_rows(graph.get("places", ()))], connections=[], unknown=[])
    result["calendar"] = w.calendar()
    result["game_facts"] = [w.line(r) for r in w.matching(("item_description_read", "text_read"))]
    result["fact_sources"] = {}
    progress = dict(catalog.get("progress") or {})
    _drop(progress, ("cc", "museum", "grandpa", "friends", "stats", "skills", "tools", "money_earned", "money_earned_self"))
    result["progress"] = progress
    from arti_runtime.experience_facts import person_shown
    result["discoveries"] = [e for e in native_rows(catalog.get("discoveries", ())) if e.get("kind") in (
        "location_entered", "npc_met", "knocked_out") and person_shown(e)]  # ADR 230 N2
    return result


def _exact_walk(policy, here, destination, route=None):
    """Only this actor's completed forward journey, never reverse/hop calibration."""
    history = (getattr(policy, "last_trips", None) or {}) if policy is not None else {}
    for (origin, arg), last in reversed(list(history.items())):
        from arti_stardew_interact import _LOCATION_ALIASES
        dest = _LOCATION_ALIASES.get(arg, str(arg).removeprefix("@"))
        if origin != here or dest != destination:
            continue
        for sample in reversed(native_rows(last.get("measurements", [last]))):
            if (sample.get("result") == "done" and type(sample.get("game_minutes")) is int
                    and sample.get("route") and sample["route"][0] == here
                    and sample["route"][-1] == destination
                    and (route is None or sample["route"] == [here] + list(route))):
                return sample
    return None


def _visit_record(policy, identity, today):
    """ADR 232 part C: her dated visits to that place, what her page there said, the game's words, her choice."""
    from arti_runtime.place_page import visit_label
    from arti_runtime.plan_dates import label as day_label
    records = getattr(policy, "place_visits", None) if not isinstance(policy, Mapping) else policy.get("place_visits")
    if not records or today is None:
        return None
    return visit_label(records, identity, day_label("stardew", today))


def _travel(offer, w, policy, notice=None, doors=None):
    """``notice`` (fix131A): offered arg -> label from today's game notice that names its place.

    ``doors`` (fix132A): offered arg -> door facts its label carries: the place a
    notice names has its door in this location, and she has been here.
    """
    from arti_runtime.experience_facts import explore_arg
    notice = notice or {}
    doors = doors or {}
    from arti_stardew_world import location_arg
    graph = w.catalog.get("world_graph") or {}
    places = {p["id"]: p for p in native_rows(graph.get("places", ())) if p.get("id")}
    # Replays may have only the retained world_details, without a native catalogue.
    if not places:
        places = {p["id"]: p for p in native_rows(((offer.get("world") or {}).get("world_details") or {}).get("places", ()))
                  if isinstance(p, Mapping) and p.get("id")}
    by_arg = {("home" if identity == graph.get("home") else location_arg(identity)): p for identity, p in places.items()}
    known = w.names()
    if w.current:
        known.setdefault(w.current, None)
    from arti_runtime import walk_refusal
    today = _page_day(offer, w.catalog)
    for entry in native_rows(offer.get("skills", ())):
        if entry.get("name") != "pergi_ke":
            continue
        args, names, arrivals, after = [], {}, {}, {}
        old_after = entry.get("after_travel") or {}
        rows = native_rows((entry.get("arrival_facts") or {}).get("rows"))
        trips = {row[0]: row[6] for row in rows if isinstance(row, (list, tuple)) and len(row) > 6
                 and isinstance(row[6], Mapping)}
        for number, arg in enumerate(native_rows(entry.get("args", ())), 1):
            place = by_arg.get(arg)
            if place is None and isinstance(arg, str) and arg.startswith("jelajah-"):
                place = next((p for a, p in by_arg.items() if explore_arg(a) == arg), None)
            identity = (place or {}).get("id")
            is_known = identity in known or arg == "home" or arg in notice
            shown = arg if is_known or str(arg).startswith("jelajah-") else explore_arg(arg)
            args.append(shown)
            names[shown] = (w.display_name(place, known.get(identity)) if place else identity or "rumahmu") if is_known else "jalan keluar %d" % number
            if is_known and known.get(identity):
                names[shown] += " (" + w.provenance(known[identity]) + ")"
            if arg in notice:
                names[shown] = notice[arg]  # today's notice names this place: its own words and time
            if arg in doors:
                names[shown] += "; " + "; ".join(doors[arg])  # fix132A: the named place's door is in this location
            refused = walk_refusal.label(policy, today, arg) if today is not None else None
            if refused:
                names[shown] += "; " + refused  # ADR 231 part B1: the game's words that ended this trip today
            if is_known and identity and (visited := _visit_record(policy, identity, today)):
                names[shown] += "; " + visited  # ADR 232 part C: her own dated record of that place
            prior = old_after.get(arg) or {}
            walked = _exact_walk(policy, w.current, identity, prior.get("route"))
            if walked:
                from arti_runtime.travel_facts import trip_stamp
                duration = "perkiraan dari perjalananmu %dm, %s" % (walked["game_minutes"], trip_stamp(walked))
            else:
                duration = "belum pernah lewat"
            # Only a displayed literal range is remembered. No open-on-arrival judgment.
            hours = [r for r in w.matching("door_message", identity)]
            parts = [duration] + [w.line(r) for r in hours]
            trip = prior.get("last_trip") or trips.get(arg) or getattr(policy, "last_trips", {}).get((w.current, arg))
            if trip and type(trip.get("seconds")) is int:
                from arti_runtime.travel_facts import destination_token
                parts.append(destination_token([arg, None, None, [], [], None, trip]))
            arrivals[shown] = "; ".join(parts)
            after[shown] = dict(label=names[shown], jalan=duration)
            if trip:
                after[shown]["last_trip"] = deepcopy(trip)
            if hours:
                after[shown]["pesan_pintu"] = [w.line(r) for r in hours]
        entry["args"], entry["nama"] = args, names
        entry["arrival_facts"] = dict(rows=arrivals)
        entry["after_travel"] = after
        entry["does"] = "Berjalan lewat jalan keluar yang ditawarkan."
    _drop(offer.get("day"), ("pintu_nama",))


def harness_goals():
    """The harness goal as it reaches a page: whole, and as the owner-turn grant body.

    ADR 229: a Live7 session's goal text is GOAL minus its grant prefix
    ("Arti, main mandiri "), as arti_stardew_runtime.decide_owner_turn parses it,
    and skill_offer keeps its first 400 characters at every page scale.
    """
    from arti_stardew_live7 import GOAL
    grant = re.fullmatch(r"(?:arti[\s,:]+)?(?:main mandiri|main sendiri|tujuan baru)(?:\s*[:,-]\s*|\s+)(\S[\s\S]{0,1199})",
                         GOAL.strip(), re.I)
    return {GOAL[:400]} | ({grant[1].strip()[:400]} if grant else set())


# ADR 229 part 4: program-written rule sentences, exactly as their producers write them.
MINES_LAW = "HP 0 = pingsan, bangun di klinik, barang bisa hilang (game). "  # arti_runtime.mines_rules.rules_line
MINES_COLLAPSE = ", pingsan 02:00"  # the collapse law inside the same line's curfew note
GIFT_LAW = "ACTION dengan Object di tangan langsung memberi hadiah tanpa konfirmasi."  # arti_stardew_live7 offer
FOOD_LAW = "makanan di tas (dimakan memulihkan energi/HP): "  # arti_runtime.day_facts.food_fact


def own_outcomes(days):
    """Her own verified meals and gifts this campaign, as dated plain records (ADR 229 part 4).

    Only rows the runtime verified from native readbacks: a meal whose item
    count fell by one with the gains the game's vitals showed, a gift whose
    native effect was verified. Never a rule, never an inferred property.
    """
    from arti_runtime.plan_dates import label, ordinal
    meals, gifts = {}, []
    for day in native_rows(days or ()):
        if not isinstance(day, Mapping):
            continue
        date = day.get("date")
        if isinstance(date, (list, tuple)) and len(date) == 3:
            number = ordinal("stardew", "Y%s %s %s" % tuple(date))
            date = label("stardew", number) if number else None
        elif not isinstance(date, str):
            date = None
        for row in native_rows(day.get("skills", ())):
            if not isinstance(row, Mapping) or row.get("verified") is not True:
                continue
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            observed = evidence.get("observed") if isinstance(evidence.get("observed"), Mapping) else {}
            names = evidence.get("names") if isinstance(evidence.get("names"), Mapping) else {}
            if row.get("skill") == "makan":
                effect = observed.get("eat_effect") if "eat_effect" in observed else observed
                if isinstance(effect, Mapping) and effect.get("consumed") is True and isinstance(effect.get("qid"), str):
                    name = str(names.get(effect["qid"]) or effect["qid"])
                    meals[name] = "kamu makan %s: energi %+g, HP %+g" % (
                        date or "tanggal belum tercatat", effect.get("energy_gain") or 0, effect.get("health_gain") or 0)
            elif row.get("skill") == "ngobrol_hadiah" and " ke " in str(row.get("arg") or ""):
                gifts.append("%s, %s" % (row["arg"], date or "tanggal belum tercatat"))
    return dict(meals=meals, gifts=gifts)


def _rule_sentences(result, own):
    """Withhold the program's rule sentences; what she did and saw stays, dated, as her own record.

    The body guard, the bedtime return and every other mechanism are untouched:
    only the sentences on the page change. Her knock-out record
    (facts.knocked_out) already rides as a dated outcome.
    """
    day = result.get("day")
    if isinstance(day, dict) and isinstance(day.get("aturan_mines"), str):
        day["aturan_mines"] = day["aturan_mines"].replace(MINES_LAW, "").replace(MINES_COLLAPSE, "")
    social = result.get("social_availability")
    if isinstance(social, dict) and str(social.get("action_notice") or "").startswith(GIFT_LAW):
        social.pop("action_notice")
        if own.get("gifts"):
            social["action_notice"] = "Hadiah terakhirmu: %s (dari hasil aksimu sendiri)." % own["gifts"][-1]
    if isinstance(day, dict) and isinstance(day.get("makanan"), str) and day["makanan"].startswith(FOOD_LAW):
        meals = own.get("meals") or {}
        items = []
        for item in day["makanan"][len(FOOD_LAW):].split(", "):
            name = re.sub(r"\s+x\d+$", "", item)
            items.append("%s (%s)" % (item, meals[name]) if name in meals else item)
        day["makanan"] = "makanan di tas: " + ", ".join(items)


MINES_FLOOR = "UndergroundMine"  # the motor's floor branch offers only mine/home (arti_stardew_live7._day_plan)


def _trip_args(offer):
    entries = [e for e in native_rows(offer.get("skills", ())) if e.get("name") == "pergi_ke"]
    if not entries:
        return None
    return list(dict.fromkeys(a for e in entries for a in native_rows(e.get("args", ())) if isinstance(a, str)))


def _reachable(place):
    return (isinstance(place, Mapping) and place.get("reachable_now") is True
            and place.get("go_to_available", True) is True and bool(place.get("route")))


# fix132A: native connection kinds that are a door into a place (NativeWorldKnowledge.cs graph capture):
# the location's door list, a farm building's door, a Buildings-layer Action tile, a building's action tile.
# Walk-over edges (warp, map:*, touch:*) and transports (native:*) are not a door. Protocol names, no map words.
DOOR_KINDS = ("door", "building_door")
DOOR_KIND_PREFIXES = ("action:", "building_action:")


def door_holders(graph, place_id):
    """fix132A: native ids of the other locations holding a player door into ``place_id``, in graph order.

    Read from the native world graph's connections ([from, to, kind, locked,
    travel, player, id]); a locked door is still a door (a festival day locks
    shop doors, ADR 229). Nothing is inferred from names, routes or maps.
    """
    out = []
    for edge in native_rows((graph or {}).get("connections", ())):
        if isinstance(edge, Mapping):
            source, target, kind, player = edge.get("from"), edge.get("to"), edge.get("kind"), edge.get("player", True)
        elif isinstance(edge, (list, tuple)) and len(edge) >= 3:
            source, target, kind = edge[0], edge[1], edge[2]
            player = edge[5] if len(edge) > 5 else True
        else:
            continue
        if (target == place_id and isinstance(source, str) and source and source != place_id and player is not False
                and isinstance(kind, str) and (kind in DOOR_KINDS or kind.startswith(DOOR_KIND_PREFIXES))):
            out.append(source)
    return list(dict.fromkeys(out))


def _visited(w):
    """Places her own record shows she has been in: dated visits, never an identity-only save flag (ADR 228)."""
    return {r["id"] for r in w.rows if r.get("kind") == "place_entered" and isinstance(r.get("id"), str)
            and r["id"] and not r.get("identity_only")}


def _notice_trips(offer, w, today):
    """fix131A: a place today's game notice names, as a choice on this page.

    Returns (offered arg -> trip label in the notice's own words and time,
    [(receipt, words, reachable now, door locations)] for named places this
    page does not offer, offered arg -> door facts for its label). Nothing is
    added, removed or reordered: only offered trips are labelled. Where no trip
    list exists the page says nothing.

    fix132A: when the named place's own trip is not offered, the trip to a
    location that holds its door (native graph) carries the notice's words, if
    her own record shows she has been in that location.
    """
    from arti_runtime import native_notices
    from arti_stardew_world import location_arg
    offered = _trip_args(offer)
    if offered is None:
        return {}, [], {}
    graph = w.catalog.get("world_graph") or {}
    here = w.current or (offer.get("facts") or {}).get("location")
    places = {p["id"]: p for p in native_rows(graph.get("places", ())) if isinstance(p, Mapping) and p.get("id")}
    visited = _visited(w) | ({here} if here else set())
    labels, missing, door_labels = {}, [], {}
    for receipt, place, words in native_notices.named_places(w, today):
        if place["id"] == here:
            continue
        arg = "home" if place["id"] == graph.get("home") else location_arg(place["id"])
        if arg in offered:
            labels[arg] = "%s (%s)" % (words, native_notices.source_line(receipt))
            continue
        doors = []
        for holder in door_holders(graph, place["id"]):
            if holder not in visited:
                continue  # she has not been there: no program knowledge of where the door is
            row = places.get(holder) or dict(id=holder)
            door_arg = "home" if holder == graph.get("home") else location_arg(holder)
            doors.append(dict(id=holder, name=w.display_name(row), here=holder == here,
                              offered=holder != here and door_arg in offered, reachable=_reachable(row)))
            if doors[-1]["offered"]:
                door_labels.setdefault(door_arg, []).append(
                    "pintu %s (%s) ada di sini" % (words, native_notices.source_line(receipt)))
        missing.append((receipt, words, _reachable(place), doors))
    return labels, missing, door_labels


def _notice_notes(result, w, missing):
    """Receipt id -> one plain fact for that notice's line: the named place is not a choice on this page.

    No route, hours or reason. On a Mines floor (the motor offers only the way
    out there) a place the native graph reaches now is said to be reached after
    leaving, naming the floor's trips as this page shows them.

    fix132A: where a location she has been in holds the named place's door,
    the line says that, and nothing more: off the Mines when that location's
    trip is offered or she stands in it; on a floor when the graph reaches it
    now (the after-leaving sentence then names that location).
    """
    shown = _trip_args(result) or []
    here = w.current or (result.get("facts") or {}).get("location")
    floor = str(here or "").startswith(MINES_FLOOR)
    notes = {}
    for receipt, words, reachable, doors in missing:
        out = [d["name"] for d in doors if d["reachable"]] if floor else [d["name"] for d in doors if d["offered"] or d["here"]]
        out = list(dict.fromkeys(out))
        if out and floor:
            fact = "%s: pintunya ada di %s, dicapai sesudah keluar dari tambang; pergi_ke di lantai ini: %s" % (
                words, ", ".join(out), ", ".join(shown))
        elif out:
            fact = "%s: pintunya ada di %s" % (words, ", ".join(out))
        elif reachable and floor:
            fact = "%s: dicapai sesudah keluar dari tambang; pergi_ke di lantai ini: %s" % (words, ", ".join(shown))
        else:
            fact = "%s: belum ada di pilihan pergi_ke sekarang" % words
        rid = receipt.get("id")
        notes[rid] = (notes[rid][:-1] + "; " + fact + "]") if rid in notes else "[" + fact + "]"
    return notes


def _native_lines(w, kinds):
    return list(dict.fromkeys(w.line(r) for r in w.matching(kinds)))


def _page_day(offer, catalog):
    """The page's native date: the day line, else the catalogue's native date."""
    day = _absolute((offer.get("day") or {}).get("date"))
    if day is None:
        date = ((offer.get("facts") or {}).get("hud") or {}).get("date")
        if isinstance(date, (list, tuple)) and len(date) == 3:
            day = _absolute("%s:%s:%s" % tuple(date))
    return day if day is not None else _absolute((catalog or {}).get("date"))


def _mailbox_rows(result, w, sighting):
    """Away from the mailbox, label the offered trips to its map and to her home by what she saw.

    Where the mail press itself is offered, that press carries its own words.
    No choice is added, removed or reordered.
    """
    skills = native_rows(result.get("skills", ()))
    if any(e.get("name") == "cek_pagi" and "mail" in native_rows(e.get("args", ())) for e in skills):
        return
    from arti_runtime import native_notices
    from arti_stardew_world import location_arg
    graph = w.catalog.get("world_graph") or {}
    reach = {"home"} | {("home" if p.get("id") == graph.get("home") else location_arg(p["id"]))
                        for p in native_rows(graph.get("places", ())) if isinstance(p, Mapping)
                        and p.get("id") and p.get("id") == sighting.get("place")}
    note = native_notices.mailbox_row_note(sighting)
    for entry in skills:
        if entry.get("name") != "pergi_ke":
            continue
        after = entry.get("after_travel") if isinstance(entry.get("after_travel"), dict) else {}
        names = entry.get("nama") if isinstance(entry.get("nama"), dict) else {}
        for arg in native_rows(entry.get("args", ())):
            if arg not in reach:
                continue
            if isinstance(after.get(arg), dict):
                after[arg]["label"] = "%s; %s" % (after[arg].get("label") or arg, note)
            if arg in names:
                names[arg] = "%s; %s" % (names[arg], note)


def _discovery_identities(rows, w):
    """Keep dated native acquisitions; acquiring an item does not read its use."""
    sources = {e.get("id"): e for e in w.events if isinstance(e, Mapping)}
    result = []
    for event in _rows(rows):
        if not isinstance(event, Mapping):
            continue
        original = event if event.get("kind") else sources.get(event.get("id"), {})
        kind = original.get("kind")
        from arti_runtime.experience_facts import person_shown
        if kind == "npc_met" and not person_shown(original):
            continue  # ADR 230 N2: a friendship record alone is not "met"
        if kind in ("location_entered", "npc_met", "knocked_out"):
            result.append(deepcopy(event))
        elif kind == "item_obtained":
            # NativeWorldKnowledge's discovery format is DisplayName: description.
            name = str(original.get("text") or original.get("subject") or "").partition(":")[0]
            result.append(dict(deepcopy(event), text=name, kind=kind, references=[]))
    return result


def _progress(block, w):
    if not isinstance(block, dict):
        return
    _drop(block, PROGRESS_REMOVE)
    for key in ("progress_details", "detail", "semua"):
        _drop(block.get(key), DETAIL_REMOVE)
    # Native Social/Skills values are represented by the exact displayed text,
    # rather than converting hidden friendship points to a public heart count.
    native = _native_lines(w, "menu_tab_opened")
    if native:
        block["menu_dibaca"] = native
    for key, kinds in (("quest", "quest_entry_read"), ("surat", "letter_opened")):
        block.pop(key, None)
        if lines := _native_lines(w, kinds):
            block[key] = lines
    block.pop("quest_read", None)


def _bag_suffix(text):
    if not isinstance(text, str):
        return text
    # Exact authored upgrade fact and legacy economic conclusion, not slot counts.
    text = re.sub(r";?\s*tas \d+ slot: \d+g di toko Pierre\.?", "", text)
    return text


def denied_counts(catalog, witnesses):
    """Count native candidate records denied at admission, never their text.

    Units are source rows (a multi-field native record counts once). Repeated
    presentations count again; missing input contributes zero, not a guessed loss.
    Authored advice removal is deliberately outside this receipt census.
    """
    w = witnesses
    def denied(rows, kind):
        rows = native_rows(rows)
        return sum(not any(str(row.get('id', row.get('key', ''))) == r.get('id')
                           for r in w.matching(kind)) for row in rows if isinstance(row, Mapping))
    graph = catalog.get('world_graph') or {}
    progress = catalog.get('progress') or {}
    known = w.names()
    detail_rows = [s for p in native_rows(graph.get('places')) for s in native_rows(p.get('facts'))]
    return dict(
        calendar=max(0, len(native_rows(catalog.get('calendar'))) - len(w.calendar())),
        place_identity=sum(p.get('id') not in known for p in native_rows(graph.get('places'))),
        place_details=len(detail_rows),
        door_hours=sum(bool(re.search(r'\b(?:hours|opens|closes)\b',str(s),re.I)) for s in detail_rows),
        prices=sum(bool(re.search(r'\b(?:price|prices)\b|\d+g\b',str(s),re.I)) for s in detail_rows),
        quest=denied((catalog.get('quest_log') or {}).get('quests'), 'quest_entry_read'),
        recipe=denied(catalog.get('recipes'), 'recipe_seen'),
        bundle=denied((progress.get('cc') or {}).get('bundles'), 'bundle_note_opened'),
        item_text=sum(isinstance(s,str) and not any(s == w.text(r) for r in w.rows)
                      for s in native_rows(catalog.get('game_facts'))),
        letter=denied((catalog.get('mail') or {}).get('unread'), 'letter_opened'),
        forecast=int(bool(((catalog.get('weather') or {}) if isinstance(catalog.get('weather'),Mapping) else {}).get('tomorrow'))
                     and not w.matching('weather_forecast_read')))


def present(offer, catalog=None, *, config=None, policy=None, context=None, counts=None):
    """Pure policy projection. Disabled is the original object, byte-for-byte.

    Call before rendering so all read_boundary paths see the same fact policy.
    No executable call is removed; unknown travel labels use the existing opaque
    handle that live7_choice already resolves to the original admitted call.
    """
    if not enabled(config):
        return offer
    w = Witnesses(catalog)
    w.on_page(offer.get("_game_text"))  # ADR 231 part B2: the notice on this page counts on this page
    if counts is not None:
        counts.update(denied_counts(catalog or {}, w))
        forecast = (offer.get('day') or {}).get('weather_tomorrow')
        counts['forecast'] = int(forecast not in (None, 'unknown') and not w.matching('weather_forecast_read'))
        # These surfaces can be produced directly from current UI captures rather
        # than the world catalogue. Count source rows, not invented missing input.
        for entry in native_rows(offer.get('skills')):
            if entry.get('name') == 'buat':
                recipes = entry.get('recipes') or {}
                ids = recipes.keys() if isinstance(recipes, Mapping) else ()
                counts['recipe'] = max(counts['recipe'], sum(not w.matching('recipe_seen', str(k)) for k in ids))
    result = deepcopy(offer)
    result["fact_line"] = True
    # Legacy same-map memories have no camera witness. Keep only this frame's
    # visible identities here; native dated action/chest/visit memories are B.
    current_targets = visible_targets(context or {})
    ui = ((context or {}).get("mines_ui") or {})
    event_actions = ui.get("event_actions")
    event_actions = event_actions if isinstance(event_actions, Mapping) else {}
    event_people = event_actions.get("people")
    event_labels = result.pop("_event_choices", {})
    map_action_labels = result.pop("_map_action_labels", {})
    day_view = result.get("day") or {}
    for key in ("warga", "warga_terlihat", "forage_terlihat", "forage_ingat", "benda_terlihat"):
        day_view.pop(key, None)
    if current_targets:
        from arti_runtime.social_facts import villagers_here
        if people := villagers_here(current_targets):
            day_view["warga"] = "Terlihat di sini: " + ", ".join(r["name"] for r in people)
        from arti_stardew_interact import is_forage_fact
        forage = [str(r.get("name") or r.get("display_name") or r.get("qualified_item_id"))
                  for r in current_targets.values() if is_forage_fact(r)]
        if forage:
            day_view["forage_terlihat"] = "Terlihat di sini: " + ", ".join(forage)
    if isinstance(event_people, Mapping) and event_people:
        people = [name for name in event_people.values() if isinstance(name, str) and name.strip()]
        if people:
            day_view["warga"] = "Terlihat di sini: " + ", ".join(dict.fromkeys(people))
    social = result.get("social_availability")
    if isinstance(social, dict):
        social["observed_npcs"] = [r for r in native_rows(social.get("observed_npcs", ())) if r.get("ref") in current_targets]
        social["observed_npcs"].extend(dict(ref=arg, name=name) for arg, name in event_labels.items())
        social["status"] = "observed" if social["observed_npcs"] else "unknown"
    # Only remove the known harness clause; her/owner's other words stay exact.
    if isinstance(result.get("owner_goal"), str):
        clause = "Fakta: jam 02:00 karakter pingsan kalau belum tidur, cadangan waktu pulang tetap dijaga."
        if result["owner_goal"] in harness_goals():
            result["owner_goal"] = result["owner_goal"].replace(clause, "").strip()
    _rule_sentences(result, (catalog or {}).get("own_outcomes") or {})
    _drop(result, ("task_finishes_without_choice", "unproductive_travel_loop"))
    # The old route sightings came from map enumeration, not a viewport.
    result.pop("forage_seen_on_way", None)
    result.pop("forage_here", None)
    if isinstance(result.get("travel_shuttle"), str):
        result["travel_shuttle"] = result["travel_shuttle"].replace(", 0 kerja selesai", "")
    day = result.get("day") or {}
    _drop(day, DAY_REMOVE)
    leave = (offer.get("day") or {}).get("festival_leave")
    if isinstance(leave, str) and leave.startswith("Peta festival."):
        day["festival_leave"] = "Peta festival." + (" Tetap di sini juga boleh." if "Tetap di sini juga boleh." in leave else "")
    _drop(day.get("economy"), ("income_preferred", "seed_reserve_g"))
    _drop(day.get("crops"), ("next_step", "needs", "buy"))
    if isinstance(day.get("kebun"), str):
        day["kebun"] = day["kebun"].replace("cangkul membuat tanah di sebelahmu bisa ditanam; ", "")
    if "bag" in day:
        day["bag"] = _bag_suffix(day["bag"])
    # Keep dated arrivals, remove appended tool/place conclusions.
    if isinstance(day.get("kunjungan"), str):
        day["kunjungan"] = day["kunjungan"].split("). ", 1)[0] + ")."
    from arti_runtime.forage_memory import split_fits
    for key in ("forage_ingat", "forage_terlihat"):
        if isinstance(day.get(key), str):
            day[key] = split_fits(day[key])[0]
    for key, kinds in (("kalender", ("calendar_day_read",)), ("festival_hari", ("calendar_day_read", "letter_opened")),
                       ("tas_info", ("item_description_read",)), ("weather_tomorrow", ("weather_forecast_read",)),
                       ("cc_terbuka", ("bundle_note_opened",)), ("quest_lanjut", ("quest_entry_read",))):
        day.pop(key, None)
        if lines := _native_lines(w, kinds):
            day[key] = lines
    # Clock/capacity are A; remote openness, reserve deadlines and collapse law are not.
    if "bedtime_facts" in day:
        facts = result.get("facts") or {}
        hud = facts.get('hud') or {}
        day["bedtime_facts"] = ["Jam %s; energi %s/%s." % (
            day.get("clock", "?"), hud.get('energy', facts.get("energy", day.get("energy", "?"))),
            hud.get('max_energy', facts.get('max_energy', '?')))]
    facts = result.get("facts") or {}
    _drop(facts, ("ruang_tas", "cadangan_pulang", "malam", "collection", "mancing"))
    # ADR 227: today's HUD text messages in the game's own words, and the
    # mailbox flag she saw while the game still draws it. Protected floor keys.
    from arti_runtime import native_notices
    from arti_runtime.page_budget import PageBudget
    today = _page_day(offer, catalog)
    if isinstance(event_people, Mapping):
        if place := event_place(w, event_actions.get("festival_id"), today):
            day["di_sini"] = place
    mailbox_count = (context or {}).get("mailbox_count")
    notice_trips, notice_missing, notice_doors = _notice_trips(offer, w, today)
    notice_bytes = native_notices.NOTICE_BYTES * PageBudget.from_config(config).scale
    if notices := native_notices.announcements(w, today, max_bytes=notice_bytes):
        result.setdefault("facts", {})["pengumuman_game"] = notices
    mailbox = native_notices.mailbox_sighting(w, today, mailbox_count)
    if mailbox is not None:
        result.setdefault("facts", {})["kotak_surat"] = native_notices.mailbox_line(mailbox, mailbox_count)
    # ADR 230 N3: the night earnings screen the game showed her (dated by the morning it appeared), today only,
    # as her own dated record; the game's words exactly, line wrapping kept. Nothing when no screen was shown.
    if today is not None and (shipped := [r for r in w.matching("shipping_summary_seen")
                                          if ((r.get("game_day") or {}).get("absolute")) == today]):
        result.setdefault("facts", {})["kiriman_kemarin"] = [w.line(r) for r in shipped][-1:]
    rod = (offer.get("facts") or {}).get("mancing")
    if isinstance(rod, str) and " di tas;" in rod:
        facts["mancing"] = rod.split(";", 1)[0] + "."
    if isinstance(day.get("home_unreachable"), dict):
        _drop(day["home_unreachable"], ("pass_out_at", "minutes_until_pass_out", "route_blocked"))
        day["home_unreachable"]["note"] = "Percobaan pulang otomatis sudah habis."
    if isinstance(day.get("route_home_blocked"), dict):
        _drop(day["route_home_blocked"], ("note", "needs"))
    if isinstance(facts.get("risiko_tambang"), str):
        facts["risiko_tambang"] = re.sub(r"damage native [^.;]+", "damage belum dilihat", facts["risiko_tambang"])
    # Counter sightings stay dated; map action schedules do not become read-hours.
    if isinstance(facts.get("konter"), list):
        facts["konter"] = [{k: v for k, v in row.items() if k not in ("buka", "tutup", "dasar", "buka_sekarang")}
                           for row in facts["konter"]]
    for key in ("konter_teramati",):
        if isinstance(facts.get(key), list):
            facts[key] = [line for line in facts[key] if not re.search(r"\d{2}:?\d{2}[-–]\d{2}:?\d{2}", str(line))]
    capabilities = result.get("capabilities") or {}
    _drop(capabilities, ("places",))
    _drop(capabilities.get("fishing"), ("sources",))
    commerce = capabilities.get("commerce") or {}
    if isinstance(commerce.get("fact"), str):
        commerce["fact"] = commerce["fact"].replace(" Shipping pays next morning.", "")
    commerce.pop("local_counters", None)
    _drop(result.get("counter_status"), ("opens_at", "closes_at", "hours", "hours_basis", "open_now"))
    # New native text is independent of any item-purpose join.
    result["game_facts"] = _native_lines(w, ("item_description_read", "text_read", "door_message",
        "shop_menu_seen", "shipping_summary_seen", "recipe_seen", "bundle_note_opened", "quest_entry_read", "event_watched"))
    # Preserve the factual scope/ownership notices (E) that do not teach lore.
    result["game_facts"] += [s for s in native_rows(offer.get("game_facts", ())) if isinstance(s, str) and s.startswith((
        "Ingatan farm berasal", "Riwayat sebelum catatan", "Native rest facts", "Quest log", "Dikatakan ke kamu soal game:"))]
    # Literal place-scoped reads must not become an undated-history summary in
    # the next projection. The receipt supplies scope; never guess it from prose.
    from arti_runtime.page_fact_selection import places_for
    _, places, _ = places_for(offer, catalog)
    local_reads = [w.line(r) for r in w.matching(('door_message','shop_menu_seen'))
                   if r.get('id') in places or r.get('place') in places]
    if local_reads:
        result.setdefault('day', {})['native_place'] = list(dict.fromkeys(local_reads))
    result["discoveries"] = _discovery_identities(offer.get("discoveries"), w)
    result.pop("calendar", None)
    if rows := w.calendar():
        result["calendar"] = [{k: v for k, v in row.items() if k != "_receipt_only"} for row in rows]
    for holder in (result, result.get("unknown_ui") or {}):
        if holder is not result:
            if "game_facts" in holder:
                holder["game_facts"] = deepcopy(result["game_facts"])
            holder.pop("calendar", None)
            if "discoveries" in holder:
                holder["discoveries"] = _discovery_identities(holder["discoveries"], w)
        _progress(holder.get("progress"), w)
        world = holder.get("world")
        if isinstance(world, dict):
            # Global reachability/lock predicates stay in admission. Only entered
            # or natively labelled places survive in the presentation catalogue.
            known = w.names()
            details = world.get("world_details") or {}
            shown = [dict(id=p["id"], label=w.display_name(p, known.get(p["id"])),
                          **({"sumber": w.provenance(known[p["id"]])} if known.get(p["id"]) else {}))
                     for p in native_rows(details.get("places", ())) if isinstance(p, Mapping) and p.get("id") in known]
            world.clear()
            world.update(places=[p["label"] for p in shown], world_details=dict(places=shown))
    _drop(day, ("collection_details",))
    _travel(result, w, policy, notice_trips, notice_doors)
    if notices and notice_missing:
        # fix131A: the not-offered fact names this page's trips as she sees them (after opaque handles).
        result["facts"]["pengumuman_game"] = native_notices.announcements(
            w, today, max_bytes=notice_bytes, notes=_notice_notes(result, w, notice_missing))
    for entry in native_rows(result.get("skills", ())):
        neutral = {"cangkul": "Mencangkul petak yang ditawarkan.", "pasang": "Memasang barang yang dibawa pada petak yang ditawarkan.",
                   "jalan_ke": "Berjalan ke petak yang ditawarkan.", "aksi": "Menekan ACTION pada target yang ditawarkan.",
                   "kirim": "Memindahkan barang lewat kotak pengiriman.", "tidur": "Membuka pilihan tidur pada kasur milikmu."}
        if entry.get("name") in neutral:
            entry["does"] = neutral[entry["name"]]
        if entry.get("name") in ("jalan_ke", "aksi") and isinstance(entry.get("nama"), dict):
            visible_args = set(current_targets)
            for target in current_targets.values():
                tile = target.get("tile") or {}
                if isinstance(tile, Mapping) and type(tile.get("x")) is int and type(tile.get("y")) is int:
                    visible_args.add("%d,%d" % (tile["x"], tile["y"]))
            entry["nama"] = {arg: label for arg, label in entry["nama"].items() if arg in visible_args}
            if entry["name"] == "aksi":
                entry["nama"].update({arg: label for arg, label in map_action_labels.items() if arg in entry.get("args", ())})
                targets = {a for a in native_rows(entry.get("args"))
                           if isinstance(a, str) and a.startswith(("map-action:", "event-"))}
                if targets:
                    for receipt in w.matching("map_action_read"):
                        if receipt.get("id") in targets and receipt.get("place") == (context or {}).get("location_id"):
                            entry["nama"][receipt["id"]] = "%s (%s)" % (
                                w.text(receipt), native_notices.source_line(receipt))
        if entry.get("name") == "cek_pagi":
            entry.pop("tv_channels", None)
            if native_notices.MAIL_PRESS_LABEL and "mail" in native_rows(entry.get("args", ())):
                # ADR 227: the bare arg reached her pages without words. Say what the press does.
                entry["nama"] = dict(entry.get("nama") or {}, mail=native_notices.MAIL_PRESS_LABEL)
        if entry.get("name") == "ngobrol_hadiah":
            visible_names = {r.get("internal_name") for r in current_targets.values() if r.get("kind") == "npc"}
            from arti_runtime.experience_facts import explore_arg
            original_args = native_rows(entry.get("args", ()))
            entry["args"] = [a if a in visible_names or a in event_labels or a == "warga" else explore_arg(a)
                             for a in original_args]
            entry["nama"] = {a: "warga %d" % (n + 1) for n, a in enumerate(entry["args"]) if a.startswith("jelajah-")}
            entry["nama"].update({a: event_labels[a] for a in original_args if a in event_labels})
        if entry.get("name") == "buat":
            _drop(entry, ("recipes", "recipes_read"))
            if lines := _native_lines(w, "recipe_seen"):
                entry["recipes"] = lines
        if entry.get("name") == "cek_jurnal":
            _drop(entry, ("jurnal_baca",))
        entry.pop("ringkas", None)  # authored most-valuable ranking
        for row in _rows(entry.get("items")):
            if isinstance(row, dict):
                _drop(row, ("unit_price_g", "unit_price", "price", "price_source", "facts", "policy", "ship_policy"))
        for quantity in (entry.get("quantity") or {}).values():
            _drop(quantity, ("reserve_g", "max_affordable"))
    if mailbox is not None:
        _mailbox_rows(result, w, mailbox)
    ui = result.get("unknown_ui")
    if isinstance(ui, dict):
        # Current native text, controls, capacity, recipes and literal item
        # descriptions stay untouched. Only the authored engine-law notice goes.
        if isinstance(ui.get("passout_safety"), dict):
            ui["passout_safety"]["instruction"] = "Gerak/hold biasa dilepas oleh safety."
            ui["passout_safety"].pop("minutes_left", None)
        _drop(ui, ("seed_shop_fact", "benih_musim", "tas_kegunaan", "peti_cocok", "cc_terbuka", "nilai_kiriman"))
        # Typed Crafting capture contains all known recipe ingredients even
        # when no particular recipe row has been read. Controls remain offered.
        if "crafting" in ui:
            ui["crafting"] = {"dibaca": _native_lines(w, "recipe_seen")}
        # The game's current shipping summary may state values; the bin's hidden
        # pre-sale UnitSellPrice is never that display.
        container = ui.get("menu_container") or {}
        if container.get("kind") == "shipping_bin":
            def without_prices(value):
                if isinstance(value, dict):
                    return {k: without_prices(v) for k, v in value.items() if k not in (
                        "unit_price", "unit_price_g", "sell_price", "price", "total_value", "value_g")}
                if isinstance(value, list):
                    return [without_prices(v) for v in value]
                return value
            result["unknown_ui"] = without_prices(ui)
    if "bisa_di_sini" in day:
        from arti_runtime.place_page import HERE, EMPTY
        day["bisa_di_sini"] = "; ".join(e["name"] for e in native_rows(result.get("skills", ())) if e["name"] in HERE) or EMPTY
    feedback = result.get("decision_feedback")
    if isinstance(feedback, dict) and isinstance(feedback.get("offered_args"), list):
        name = (feedback.get("submitted") or {}).get("name")
        if name in ("pergi_ke", "ngobrol_hadiah"):
            from arti_runtime.experience_facts import explore_arg
            shown = {a for e in native_rows(result.get("skills", ())) if e.get("name") == name for a in native_rows(e.get("args", ()))}
            feedback["offered_args"] = [explore_arg(a) if a not in shown and explore_arg(a) in shown else a
                                        for a in feedback["offered_args"]]
    return result


def reflection(rows, catalog):
    """Same policy for citable bedtime facts; preserve own actions/chests/crops."""
    w = Witnesses(catalog)
    result = []
    for row in rows:
        ref = row.get("ref", "")
        if ref.startswith(("p:cc", "p:peti-cc", "p:forage", "p:grandpa", "p:skills", "p:tools", "p:earned",
                           "p:today", "q:", "m:", "cc:", "seen:")):
            continue
        from arti_runtime.forage_memory import split_fits
        result.append(dict(row, text=split_fits(row.get("text", ""))[0]))
    result.extend(dict(ref="seen:" + r["kind"] + ":" + r["id"], text=w.line(r)) for r in w.matching((
        "calendar_day_read", "letter_opened", "recipe_seen", "bundle_note_opened", "quest_entry_read", "menu_tab_opened")))
    return result


def event_display(w, event_id, today, *, through=None):
    """The title or exact visible text the game associated with this event."""
    if not isinstance(event_id, str) or not event_id or today is None:
        return None
    from arti_runtime.native_notices import today_notices, words

    def eligible(row):
        clock = row.get("clock")
        return through is None or type(clock) is int and clock <= through

    # Calendar data is a candidate for matching literal HUD text, never a witness.
    notices = today_notices(w, today)
    event_rows = [r for r in reversed(w.matching("event_watched", event_id))
                  if eligible(r) and (r.get("game_day") or {}).get("absolute") == today]
    for row in native_rows(w.catalog.get("calendar")):
        label = row.get("name")
        native_id = row.get("id")
        if not isinstance(native_id, str) or event_id not in (native_id, "festival_" + native_id):
            continue
        for notice in notices + event_rows:
            if eligible(notice) and isinstance(label, str) and label and (match := _name_pattern(label).search(w.text(notice))):
                return dict(name=words(match.group(0)), text=words(w.text(notice)), receipt=notice)
    for receipt in event_rows:
        if text := words(w.text(receipt)):
            return dict(name=None, text=text, receipt=receipt)
    return None


def event_place(w, event_id, today):
    from arti_runtime.native_notices import source_line

    if display := event_display(w, event_id, today):
        return "%s (%s)" % (display["name"] or display["text"][:240], source_line(display["receipt"]))
    return None


def plan_views(rows, calendar):
    """Hide unreceipted native calendar bindings, keeping authored plans exact.

    This is a copy for rendering, never a migration of the goal store. Own
    relative/absolute words keep their original authored-day interpretation.
    """
    from arti_runtime.plan_dates import references, event_references
    calendar = list(native_rows(calendar))
    known = {e.get("id") for e in calendar}
    result = []
    for original in rows:
        row = deepcopy(original)
        prior_refs = list(row.get("calendar_refs") or ())
        authored = row.get("authored_on_day")
        if type(authored) is not int:
            authored = row.get("set_on_day")
            if type(authored) is int and row.get("why_set") == "reflection":
                authored -= 1
        words = " ".join((row.get("text", ""), row.get("done_when", "")))
        named = {e.get("id") for e in calendar if event_references(words, game="stardew", authored_day=authored,
                                                                 calendar=[e], named_only=True)}
        allowed_refs = [ref for ref in prior_refs if ref in known and ref in named]
        allowed_days = {day for e in calendar if e.get("id") in allowed_refs
                        for date in native_rows(e.get("dates", ())) if (day := _absolute(date)) is not None}
        prior_days = set(row.get("event_days") or ())
        removed = prior_days - allowed_days
        if removed or prior_refs != allowed_refs:
            row["calendar_refs"] = allowed_refs
            row["event_days"] = sorted(prior_days & allowed_days)
            own_days = set(references(words, game="stardew", authored_day=authored))
            if "referenced_days" in row:
                row["referenced_days"] = [d for d in row["referenced_days"] if d not in removed or d in own_days]
            row["binding_unknown"] = True
        result.append(row)
    return result


def runtime_catalog(runtime, catalog, *, game_day=None, clock=None):
    """Join the shared receipts and backfill explicit native identity flags only."""
    if not enabled(getattr(runtime, "_config", None)) or not isinstance(catalog, Mapping):
        return catalog
    from arti_runtime.experience_facts import with_memory, experiences
    from arti_runtime.seen_receipts import SeenReceipts, backfill_from_save
    base, scope = runtime._live7_mind_base(), catalog.get("scope")
    if base is not None and scope:
        from arti_runtime.native_story import BOARD_READ_ONLY
        BOARD_READ_ONLY.add((str(base), scope))  # ADR 230 N4: her story memory keeps only calendar entries she read
    current = (catalog.get("world_graph") or {}).get("current")
    clock = clock if clock is not None else catalog.get("clock")
    if base is not None and scope:
        store = SeenReceipts(base, scope)
        day = game_day or _absolute(catalog.get("date"))
        if day:
            known = {key for key in store.index() if all(key)}  # keys only, no per-receipt copy (fix129A)
            for event in experiences(catalog):
                if (event.get("kind") == "location_entered" and event.get("subject")
                        and ("place_entered", event["subject"]) not in known):
                    store.note("place_entered", event["subject"], game_day=_absolute(event.get("date")) or day,
                               clock=event.get("clock"), place=event["subject"], source=event.get("source") or "native_visit")
                    known.add(("place_entered", event["subject"]))
            if current and ("place_entered", current) not in known:
                store.note("place_entered", current, game_day=day, clock=clock, place=current, source="native_current")
                known.add(("place_entered", current))
            native = catalog.get("personal_save_state") or {}
            native = dict(native, game_day=day, clock=clock, place=current, source="native_save_flags")
            native.setdefault("mail_read", (catalog.get("mail") or {}).get("read_ids", ()))
            for row in backfill_from_save(native):
                if (row["kind"], row["id"]) not in known:
                    store.note(row["kind"], row["id"], game_day=row["game_day"], clock=row["clock"],
                               place=row["place"], source=row["source"])
        catalog = with_memory(catalog, base, vars(runtime).setdefault("_live7_fact_line_cache", {}))
        # fix122A owns literal text and delivery offsets in its existing ledger.
        # Only completed, answered, hash-checked text may back a reusable fact.
        from arti_runtime.game_text_delivery import read_text
        native_text = []
        pointers = {r["receipt"].get("text_ref") for r in experiences(catalog)
                    if isinstance(r.get("receipt"), Mapping) and isinstance(r["receipt"].get("text_ref"), str)
                    and r["receipt"]["text_ref"].startswith("game_text:")}
        from arti_runtime.game_text_delivery import texts
        index = texts(base, scope) if pointers else {}  # ADR 231 part C: one ledger snapshot, not one per pointer
        for ref in sorted(pointers):
            shown = read_text(base, scope, ref, index=index)
            if shown and shown.get("complete") is True:
                native_text.append(dict(id=ref, kind="native_delivered_text", text=shown["text"],
                    source=shown["source"], date=shown["date"], clock=shown["clock"],
                    event_place=shown["place"]))
        if native_text:
            catalog = dict(catalog, experience_records=list(catalog.get("experience_records") or ()) + native_text)
    catalog = dict(catalog, fact_line=True)
    # ADR 229 part 4: her own verified meals and gifts this campaign release their dated records.
    days = getattr(getattr(getattr(runtime, "_skill_session", None), "campaign", None), "days", None)
    if days:
        catalog["own_outcomes"] = own_outcomes(days)
    runtime._live7_fact_line_catalog = catalog
    return catalog


def goal_evidence(rows):
    """ADR 221: owned mind is always visible; external lines retain receipt provenance.

    Speech can prompt a question or want. Only native receipts can measure met
    or answer found. Missing stream dates never acquire a game date as a substitute.
    """
    result = []
    for row in rows:
        if row.get('basis') in ('owner_said', 'arti_said', 'viewer_said'):  # ADR 228: viewers are a source too
            if row.get('stream_date') and row.get('speaker') in ('owner', 'arti', 'viewer'):
                result.append(row)
        elif row.get('receipt_kind') and not str(row.get('source') or '').startswith('stream:'):
            result.append(row)
    return result
