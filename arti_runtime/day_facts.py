"""Compact day facts the game already states and the page never carried (AUDIT_UNDERSTANDING1, ADR 93).

Facts only, from values already observed: what is true or possible, never what to do. No item, NPC or location IDs.
"""
import re
from collections.abc import Mapping

SEASONS = ("spring", "summer", "fall", "winter")
SEASON_DAYS = 28
FOOD_NAMES_SHOWN = 4
LINE_CHARS = 160


def _short(text, limit=LINE_CHARS):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def season_fact(date):
    """``hari 5/28 spring; summer mulai 24 hari lagi``: where the season stands (a 28-day season is game data), or None."""
    try:
        season, day = str(date[1]).casefold(), int(date[2])
    except (TypeError, ValueError, IndexError):
        return None
    if type(date[2]) is bool:  # fix64: int(True) == 1 is not a day
        return None
    if season not in SEASONS or not 1 <= day <= SEASON_DAYS:
        return None
    following = SEASONS[(SEASONS.index(season) + 1) % len(SEASONS)]
    left = SEASON_DAYS - day + 1
    when = "besok" if left == 1 else "%d hari lagi" % left
    return "hari %d/%d %s; %s mulai %s" % (day, SEASON_DAYS, season, following, when)


SEASON_RULE = "Saat musim berganti, tanaman di luar ruangan yang bukan tanaman musim baru mati."  # fix64: not indoor crops (ADR 93)
SEED_SHOP_CHARS = 420  # about eight seeds; the stock rows and the choices are unchanged
SEED_SHOWN = 3         # a planting page names at most this many kinds, then a count


def seed_line(qid, name, date):
    """ADR 113 (a50 d22 J898: Parsnip Seeds bought on spring 26, no line set 4 days against 2): days to ripen beside days her
    crop can live. Game data only; ``None`` when the seed or the date is not known. Buying and planting stay her choice."""
    from arti_stardew_seeds import CROP_SEEDS, SEED_CROP, SEASONS as ORDER, crop_grow_days
    row, grow = CROP_SEEDS.get(qid), crop_grow_days(SEED_CROP.get(qid))
    try:
        season, day = str(date[1]).casefold(), int(date[2])
    except (TypeError, ValueError, IndexError):
        return None
    if row is None or grow is None or season not in ORDER or not 1 <= day <= SEASON_DAYS:
        return None
    seasons = row[1]
    text = "%s: tumbuh %d hari, " % (name, grow)
    if season not in seasons:
        return text + "bukan musimnya (%s)" % "/".join(seasons)
    if len(seasons) == len(ORDER):
        return text + "hidup di semua musim"
    left, at = SEASON_DAYS - day, ORDER.index(season)
    while ORDER[(at + 1) % len(ORDER)] in seasons:
        at, left = at + 1, left + SEASON_DAYS
    return text + "sisa musim %d hari" % left


def seed_shop_fact(stock, date):
    """The shop page has no `day` block: where the season stands, its rule and each seed row's days, or None without a seed."""
    lines = [line for row in stock or () if isinstance(row, (list, tuple)) and len(row) > 2
             for line in [seed_line(row[1], row[2], date)] if line]
    if not lines:
        return None
    kept, used = [], 0
    for line in lines:
        if used + len(line) > SEED_SHOP_CHARS:
            break
        kept.append(line)
        used += len(line) + 2
    more = len(lines) - len(kept)
    return " ".join(p for p in (season_fact(date), SEASON_RULE, "; ".join(kept) + (" +%d benih lagi" % more if more else "")) if p)


def seed_lines(inventory, date):
    """The seeds she holds, for the planting page: count, days to ripen and days left."""
    out = []
    for row in inventory or ():
        if isinstance(row, Mapping) and row.get("category") == -74 and type(row.get("stack")) is int and row["stack"] > 0:
            from arti_runtime.fact_names import item_name
            name = "%s x%d" % (item_name(row.get("qualified_item_id"), name=row.get("name")), row["stack"])
            if line := seed_line(row.get("qualified_item_id"), name, date):
                out.append(line)
    return out[:SEED_SHOWN] + (["+%d benih lagi" % (len(out) - SEED_SHOWN)] if len(out) > SEED_SHOWN else [])


def stored_seed_rows(counts, names=None):
    """FIX72D (a53 d6 J2296): the crop seeds among what her chests held, as rows. Game data decides what a seed is."""
    from arti_runtime.fact_names import item_name
    from arti_stardew_seeds import CROP_SEEDS
    return [dict(qualified_item_id=qid, name=item_name(qid, names), stack=count)
            for qid, count in sorted((counts or {}).items()) if qid in CROP_SEEDS and type(count) is int and count > 0]


def stored_seed_lines(rows, date):
    """The same days-to-ripen line as a seed in the bag, saying where it is. Nothing here makes planting possible from there."""
    out = []
    for row in rows or ():
        if line := seed_line(row["qualified_item_id"], "%s x%d di peti" % (row["name"], row["stack"]), date):
            out.append(line)
    return out[:SEED_SHOWN]


# ADR 99 (a46: museum on 233 pages, blacksmith on 156, community center on 377, with hours and arrival only; no line said what
# any of them does). Life map L023, L080, L088, L089 and the room rewards: what she can do there, every place alike.
PLACE_USES = ("seed shop (toko Pierre): beli benih, jual hasil panen; blacksmith: buka geode (25g, isinya bisa mineral), upgrade alat; museum: sumbang artefak dan mineral; "
              "community center: isi bundle, hadiahnya perbaikan kota; animal shop: beli hewan dan pakan; "
              "@FishShop: beli alat pancing, jual ikan; @Saloon: beli makanan; @ScienceHouse: bangun, beli kayu.")


def crop_line(crops):
    """ADR 99 (a46: `day.crops` on 94 of 284 Farm decisions, with 1.4 KB of guard unused): one line of the same numbers.
    Scene counts are of the place she stands in, so the line says so; her own plantings are the ledger's, across days."""
    own = crops.get("own") if isinstance(crops.get("own"), dict) else None
    stored = crops.get("stored_seeds")  # FIX72D: `tanam` needs seeds in the bag; these are known, not plantable from there
    # FIX76A (a55 d4 p19, p20): "[recorded remark omitted]", one step later "0": the count is the ground beside her (`planting_soil`), the rest
    # is the scene's, so it now says which. What makes ground plantable was in `crops.needs`, off the page on 738 of 738 (d3 p13).
    return ("di sini %d petak dicangkul: %d di sebelahmu bisa ditanam, %d tertanam, %d kering, %d siap panen; benih musim ini di tas %d%s; "
            "cangkul membuat tanah di sebelahmu bisa ditanam; %s"
            % (crops.get("hoed_tiles", 0), crops.get("plantable_tiles", 0), crops.get("planted_tiles", 0),
               crops.get("dry_tiles", 0), crops.get("ready_tiles", 0), crops.get("seeds", 0),
               ", di peti %d" % stored if stored else "",
               "tanaman sendiri: %d tumbuh, %d siap, %d mati" % (own.get("growing", 0), own.get("ready", 0), own.get("dead", 0))
               if own else "tanaman sendiri: tidak ada catatan"))


def food_fact(ui_facts, inventory):
    """Which bag items the game admits as food, by name and count, or None (the mod's own candidate rows).

    fix64 (ADR 93): not called cheap; the mod also admits a pricier hotbar food under its survival flag. Stacks of one
    item split across hotbar and backpack are summed."""
    names = {f.get("qualified_item_id"): f.get("name") for f in inventory or () if isinstance(f, Mapping)}
    counts = {}
    for key in ("eat_candidates", "eat_backpack_candidates"):
        for c in (ui_facts or {}).get(key) or ():
            if not isinstance(c, Mapping) or not isinstance(c.get("qualified_item_id"), str):
                continue
            stack = c.get("stack") if type(c.get("stack")) is int else 0
            counts[c["qualified_item_id"]] = counts.get(c["qualified_item_id"], 0) + max(stack, 0)
    from arti_runtime.fact_names import item_name
    rows = [item_name(qid, names) + (" x%d" % stack if stack else "") for qid, stack in counts.items()]
    if not rows:
        return None
    shown = rows[:FOOD_NAMES_SHOWN] + (["+%d lagi" % (len(rows) - FOOD_NAMES_SHOWN)] if len(rows) > FOOD_NAMES_SHOWN else [])
    return _short("makanan di tas (dimakan memulihkan energi/HP): " + ", ".join(shown))


def native_entry_hours(places, location):
    """``0900-1700`` from the native door fact of the catalog place ``location`` (LockedDoorWarp hours), or None."""
    for p in places or ():
        if not isinstance(p, Mapping) or p.get("id") != location:
            continue
        for fact in p.get("facts", ()):
            match = re.match(r"entry hours (\d+)-(\d+)", str(fact))
            if match:
                return "%04d-%04d" % (int(match[1]), int(match[2]))
    return None


def night_fact(night):
    """One line on the wallet across the night, from the carried night record, or None when nothing is known.

    The clock is when that amount was first seen (the wallet row is journaled once per change), not bedtime (fix64, ADR 93).
    The difference is the observed net change; the game pays the shipping bin and may charge fees overnight, and
    the record does not separate them."""
    if not isinstance(night, Mapping):
        return None
    before, after = night.get("money_before"), night.get("money_after")
    slept = night.get("outcome") == "verified"
    when = (night.get("money_before_observed") or {}).get("time_of_day")
    clock = "%02d:%02d" % (when // 100 % 24, when % 100) if type(when) is int else "?"
    if type(before) is int and type(after) is int:
        text = "dompet %dg (terakhir berubah jam %s) -> %dg pagi ini (%+dg semalam)" % (before, clock, after, after - before)
    elif type(before) is int:
        text = "dompet %dg (terakhir berubah jam %s); dompet pagi ini belum terbaca" % (before, clock)
    else:
        text = "dompet semalam tidak terbaca"
    energy = night.get("energy_after")
    if type(energy) in (int, float):
        text += "; energi pagi %g" % energy
    from arti_runtime.sleep_truth import night_kind
    kind = night.get("kind") or night_kind(knockout=night.get("reason") == "knocked_out",
                                           observed=night.get("observed"),
                                           collapse_clock=night.get("reason") == "live7:passed_out"
                                           and type(night.get("time_of_day")) is int and night["time_of_day"] >= 2600)
    if slept or kind == "slept":
        tail = "; tidur terverifikasi"
    elif kind == "knocked_out":
        tail = "; pingsan karena KO tercatat"
    elif kind == "collapsed" or (night.get("reason") == "live7:passed_out"
                                 and type(night.get("time_of_day")) is int and night["time_of_day"] >= 2600):
        tail = "; pingsan 02:00 tercatat"
    else:
        tail = "; malam berakhir tanpa tidur terverifikasi"
    return _short(text + tail + ". Selisih = efek malam (kiriman dibayar, biaya), tidak dipisah.")
