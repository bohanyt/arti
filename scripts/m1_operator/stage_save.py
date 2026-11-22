"""stage_save.py: the route-A save staging recipe (docs/stardew/primitives-v2/SAVE_STAGING_RESEARCH.md §5) as one tool.

  stage_save.py status                       read-only save facts (cheats flag, mine level, date, money, farmhands)
  stage_save.py backup                       copy the whole save folder -> PREP/save-backup-<prefix>-<utc>/ + manifest
  stage_save.py edit --set KEY=VALUE ... [--dry-run]
                                             game closed; ALWAYS backs up first; BOM-preserving single-element edits
                                             (allowChatCheats=true|false, mine_lowestLevelReached=N,
                                             date=spring:13 [--set year=N], animals=ready: every Coop/Barn FarmAnimal
                                             wasPet=false, fullness=255, happiness=255 always; currentProduce + daysSinceLastLay
                                             only from the installed FarmAnimals.xnb (adult, one unconditional produce id)
                                             or kept when already set; other animals are reported as skipped with a reason;
                                             date edits also update SaveGameInfo;
                                             hay=N: the Farm's own silo <piecesOfHay> (0..sum of Farm building hayCapacity);
                                             farm_clear=X,Y;X,Y: remove ONLY wild Trees + Twig/Stone/Weeds litter keyed on
                                             exactly those Farm tiles; refuses buildings/clumps/bushes/crops/chests/machines/
                                             fences/any other element on a listed tile, and a listed tile with nothing removable;
                                             fruit_tree=Farm:X,Y:633:ready: insert one mature Apple tree + one Apple on an
                                             empty 3x3 (no tree within 2 tiles); Farm only, no clearing or inventory edits;
                                             sunny staging baseline, no simulated night/season progression; the new file must
                                             equal the backup with exactly these edits; prints the element diff
                                             mail_received=FARMER_ID:FLAG[,FLAG]: add only ccDoorUnlock and/or
                                             canReadJunimoText to that exact farmer's mailReceived; no bundle edits
  stage_save.py diff [BACKUP_DIR] [--max N]  element-level diff of the save vs a backup (default: the newest backup)
  stage_save.py snapshot NAME [--note T] [--overwrite]
                                             game closed; copy the whole save folder -> PREP/savestates/NAME/save/ + manifest.json
  stage_save.py restore NAME                 game closed; verify the snapshot sha256 manifest, safety backup, replace the save
                                             files, verify sha256 again; extra files in the live folder are removed (in the backup)
  stage_save.py list-snapshots               name, date facts, utc, note of every snapshot under PREP/savestates
  stage_save.py clear-bin-area COPY_SAVE [--rect 69 12 74 16] [--dry-run]
                                             copy under PREP only; remove bounded Farm trees/ground litter;
                                             backup, byte-preserving XML edit, verification and JSON receipt
  stage_save.py locations                    building interiors and cabins with a unique name: `type  uniqueName  (tile)`;
                                             copy the exact name for `/debug warp`
  stage_save.py check-script FILE            validate a /debug script; sends nothing
  stage_save.py run-script FILE --target farmhand|host [--owner-ok] [--dry-run] [--label L] [--line-delay S]
                                             type every line into the target's chat: `ui_op key t` (opens chat),
                                             2 x backspace (clears a stray char), `ui_op type LINE --hwnd H` (aborts the
                                             moment the target loses the foreground), `ui_op key enter`; a screenshot
                                             before, after every block (blank line) and at the end; a receipt
                                             RUN/staging-<label>-<utc>/STAGING_<n>.md + every line tagged STAGING in
                                             RUN/staging-log.jsonl; the HOST back in front after a farmhand script.

Script lines: `/debug <cmd> ...` or `/money N`; `#` comments; a blank line ends a block. Every line is validated BEFORE
anything is typed (one bad line refuses the whole script, exit 2):
  - commands from the research table only (§2, DECOMP DebugCommands.cs); refused on purpose: gold (1M), achievement
    (Steam unlock), fillbackpack (random), profession (permanent, owner D8), changewallet/separatewallets/mergewallets,
    allmail (every mail flag at once, irreversible);
  - host-only commands (finishallbuilds; dog / cat spawn an NPC pet) refuse --target farmhand; world-state commands
    (build, animal, horse, spawncoopsandbarns, year/day/season, growcrops, completecc, allbundles, bundle) refuse
    --target farmhand unless --allow-world-on-farmhand (research risk 2: farmhand desync); completecc / allbundles
    (the whole Community Center) also need --owner-ok AND --confirm-world;
  - run-script verifies the chat box opened after `T` (PrintWindow before/after: the bottom-left 896 x 64 client strip
    where ChatBox draws its text box, ChatBox.cs:89-100, must change by >= --chat-min-mad) before anything is typed;
    otherwise it aborts with nothing typed and no Enter (an Enter in the world would act there). HYPOTHESIS threshold;
    every score is in the receipt;
  - `warp <location> [X Y]` (not in SAVE_STAGING_RESEARCH §2; added with the location guard): the location must be an
    EXACT name from the save (static map names or a building/cabin unique name; `stage_save.py locations` lists them);
    any `~` or unknown name is refused, with a suggestion when a compact token's prefix matches;
  - `seenevent <id>` only for the ids in SEEN_EVENTS (100162 first Mine entry; FINAL Part B proof save);
  - `item <qid> [n] [quality]` ids and `itemnamed "<name>"` names must exist in the installed game's data
    (xnb_items.py reads Content/Data/{Objects,BigCraftables,Weapons,Tools}.xnb; --catalog FILE = a JSON override).
--target host types into the owner's HOST window: refused without --owner-ok (PLAN §6.2: HOST input waits for the owner).
run-script refuses while a trial runner runs, without chat cheats in the save (`edit --set allowChatCheats=true` with
the game closed first), or when the target window is not the operator's farmhand / the HOST.
Staged state is never evidence (PLAN §5.2): the next evidence attempt starts with a fresh label.
Exit: 0 done | 2 refused (validation / precondition) | 3 aborted mid-script (receipt still written) | 4 edit verify failed.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402

BOM = b"\xef\xbb\xbf"
EDITABLE = {"allowChatCheats": "bool", "mine_lowestLevelReached": "int", "date": "calendar", "year": "calendar"}
# Festival days: fill in from your own game data (left out of the public tree).
FESTIVAL_DATES = set()
GAME_EXES = ("Stardew Valley.exe", "StardewModdingAPI.exe")
SKILLS = ("farming", "fishing", "foraging", "mining", "combat", "all")
SEASONS = ("spring", "summer", "fall", "winter")
QUALITIES = (0, 1, 2, 4)
REFUSED = {"gold": "adds 1,000,000 (far over the staging target)", "achievement": "unlocks a Steam achievement",
           "fillbackpack": "random items", "profession": "a permanent profession choice is the owner's (PLAN D8)",
           "changewallet": "wallet mode is the owner's", "separatewallets": "wallet mode is the owner's",
           "mergewallets": "wallet mode is the owner's",
           "allmail": "sets every mail flag at once (irreversible); stage one letter with mailfortomorrow <id>"}
WORLD = {"build", "animal", "horse", "spawncoopsandbarns", "year", "day", "season", "growcrops", "completecc",
         "allbundles", "bundle"}
HOST_ONLY = {"finishallbuilds", "dog", "cat"}             # dog / cat spawn an NPC pet: world state, host only
CONFIRM_WORLD = {"completecc", "allbundles"}               # the whole Community Center: --owner-ok + --confirm-world
CHAT_STRIP = (896, 64)                                     # ChatBox text box: x 0..896, bottom 56 px (ChatBox.cs:89-100)
# Map names from the 1.6.15 decomp literals (Utility/Game1/DebugCommands); building and cabin unique names come from the save.
STATIC_LOCATIONS = frozenset({
    "AbandonedJojaMart", "AdventureGuild", "AnimalShop", "Backwoods", "Beach", "BeachNightMarket", "BoatTunnel", "BusStop",
    "Caldera", "CommunityCenter", "Desert", "DesertFestival", "Farm", "FarmCave", "FarmHouse", "FishShop", "Forest",
    "Greenhouse", "Hospital", "IslandEast", "IslandHut", "IslandNorth", "IslandSouth", "IslandSouthEast", "Islandwest",
    "LeoTreeHouse", "LewisBasement", "ManorHouse", "MasteryCave", "Mine", "Mountain", "MovieTheater", "QiNutRoom",
    "Railroad", "SandyHouse", "SeedShop", "Sewer", "SkullCave", "SlimeHutch", "Sunroom", "Town", "Trailer", "Trailer_Big",
    "UndergroundMine", "VolcanoEntrance", "WitchSwamp", "WizardHouseBasement"})
ANIMAL_FIELDS = ("wasPet", "fullness", "happiness", "daysSinceLastLay", "currentProduce")


class Refused(Exception):
    pass


# ============================================================ save XML (pure)
def save_file(name=None):
    n = name or paths.SAVE_NAME
    return paths.SAVES / n / n


def _decode(raw):
    try:
        return raw.decode("utf-8-sig"), raw.startswith(BOM)
    except UnicodeError as exc:
        raise Refused("save must be valid UTF-8 XML") from exc


def _encode(text, bom):
    return (BOM if bom else b"") + text.encode("utf-8")


def _norm_value(key, value):
    kind = EDITABLE.get(key)
    if kind is None:
        raise Refused(f"{key!r} is not an editable element (allowed: {', '.join(EDITABLE)})")
    if kind == "calendar":
        raise Refused("calendar edits require the paired main-save/SaveGameInfo edit command")
    v = str(value).strip()
    if kind == "bool":
        if v.lower() not in ("true", "false"):
            raise Refused(f"{key} must be true|false, got {value!r}")
        return v.lower()
    if not re.fullmatch(r"-?\d+", v) or not -1 <= int(v) <= 120:
        raise Refused(f"{key} must be an integer -1..120, got {value!r}")
    return str(int(v))


def apply_edits(raw, edits):
    """(new bytes, [change rows]). edits = {key: value}; every key must occur exactly once as <key>v</key>.
    BOM, line endings and every other byte are kept."""
    text, bom = _decode(raw)
    changes = []
    for key, value in edits.items():
        v = _norm_value(key, value)
        pat = re.compile(rf"<{key}>([^<]*)</{key}>")
        hits = pat.findall(text)
        if len(hits) != 1:
            raise Refused(f"<{key}> occurs {len(hits)} times in the save (expected exactly 1): not editing")
        old = hits[0]
        changes.append(dict(key=key, old=old, new=v, changed=old != v))
        if old != v:
            text = pat.sub(f"<{key}>{v}</{key}>", text, count=1)
    return _encode(text, bom), changes


CC_MAIL_FLAGS = frozenset({"ccDoorUnlock", "canReadJunimoText"})


def farmer_mail_received(raw, value):
    """Add reviewed CC access/readability flags to exactly one serialized farmer.

    GameLocation.WarpCommunityCenter reads MasterPlayer.ccDoorUnlock; the physical
    note reads actor.canReadJunimoText. Crafts Room needs no completed bundle or
    additional mail (CommunityCenter.shouldNoteAppearInArea, area 1).
    Preserve every byte outside the selected mailReceived element.
    """
    import xml.etree.ElementTree as ET
    from xml.parsers import expat
    m = re.fullmatch(r"(-?[0-9]+):([A-Za-z]+(?:,[A-Za-z]+)*)", value)
    if not m or not -(2**63) <= int(m[1]) < 2**63:
        raise Refused("mail_received needs FARMER_ID:FLAG[,FLAG] (signed 64-bit ID)")
    flags = m[2].split(",")
    if len(flags) != len(set(flags)) or not set(flags) <= CC_MAIL_FLAGS:
        raise Refused("mail_received permits unique ccDoorUnlock,canReadJunimoText flags only")
    _decode(raw)
    if b"<!DOCTYPE" in raw.upper():
        raise Refused("DOCTYPE is unsupported in save XML")
    root = _root(raw)
    if root.tag != "SaveGame" or len(root.findall("player")) != 1 or len(root.findall("farmhands")) != 1:
        raise Refused("mail_received requires one SaveGame player and farmhands collection")
    farmers = [root.find("player"), *root.find("farmhands")]
    if any(f.tag != "Farmer" for f in farmers[1:]):
        raise Refused("unsupported farmhands collection")
    selected = [f for f in farmers if len(f.findall("UniqueMultiplayerID")) == 1
                and f.findtext("UniqueMultiplayerID") == str(int(m[1]))]
    if len(selected) != 1:
        raise Refused("mail_received farmer ID must match exactly one host/farmhand")
    farmer = selected[0]
    mails = farmer.findall("mailReceived")
    if len(mails) != 1 or mails[0].attrib or any(n.tag != "string" or n.attrib or len(n) for n in mails[0]):
        raise Refused("mail_received requires one plain string collection")
    mail = mails[0]
    before = [n.text or "" for n in mail]
    if any(before.count(flag) > 1 for flag in flags):
        raise Refused("duplicate requested mail flag in save")
    added = [flag for flag in flags if flag not in before]
    row = dict(key="mail_received", farmer_id=str(int(m[1])), farmer=farmer.findtext("name"),
               old=before, new=before + added, added=added, changed=bool(added))
    if not added:
        return raw, row
    parser = expat.ParserCreate()
    starts, stack, spans = [], [], {}

    def start(tag, attrs):
        starts.append(parser.CurrentByteIndex)
        stack.append(parser.CurrentByteIndex)

    def end(tag):
        first, close = stack.pop(), parser.CurrentByteIndex
        opening_end = raw.find(b">", first) + 1
        self_closed = raw[first:opening_end].rstrip().endswith(b"/>")
        spans[first] = (opening_end if self_closed else raw.find(b">", close) + 1, close, self_closed)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    first = dict(zip(root.iter(), starts))[mail]
    last, close, self_closed = spans[first]
    addition = b"".join(f"<string>{flag}</string>".encode("ascii") for flag in added)
    replacement = (b"<mailReceived>" + addition + b"</mailReceived>" if self_closed
                   else raw[first:close] + addition + raw[close:last])
    new = raw[:first] + replacement + raw[last:]
    for flag in added:
        ET.SubElement(mail, "string").text = flag
    if ET.tostring(_root(new)) != ET.tostring(root):
        raise Refused("mail_received exact XML readback mismatch: no edit")
    return new, row


def parse_date(value, year):
    match = re.fullmatch(r"(spring|summer|fall|winter):([0-9]+)", str(value).strip())
    if not match:
        raise Refused("date must be spring|summer|fall|winter:DAY (1..28)")
    season, day = match[1], _int(match[2], 1, 28, "date day")
    # Utility.isGreenRainDay (4927-4935) recomputes one of these from the seed
    # on load; this small sunny/festival helper does not emulate that RNG.
    if season == "summer" and day in (5, 6, 7, 14, 15, 16, 18, 23):
        raise Refused("summer green-rain candidate date: sunny staging cannot guarantee this day")
    return season, day, _int(str(year), 1, 9999, "year")


def apply_date_edits(raw, date, *, info=False):
    """Stage a modern 1.6 save/Farmer preview using scoped XML byte spans.

    Source decisions and serialized shapes: FIXB65.md. No historical WorldDate,
    progression counters, world-state flags, or non-valley weather are rewritten.
    Unsupported/legacy layouts refuse before any backup or mutation.
    """
    import xml.etree.ElementTree as ET
    from xml.parsers import expat
    try:
        raw.decode("utf-8-sig")
        root = ET.fromstring(raw)
        if b"<!DOCTYPE" in raw.upper():
            raise Refused("DOCTYPE is unsupported in save XML")
    except (UnicodeError, ET.ParseError) as exc:
        raise Refused("date staging requires valid UTF-8 save XML") from exc
    if root.tag != ("Farmer" if info else "SaveGame"):
        raise Refused("unexpected date staging XML root")
    # Expat byte offsets include the BOM, unlike character offsets in decoded XML.
    parser = expat.ParserCreate()
    stack, spans, starts = [], {}, []

    def start(tag, attrs):
        stack.append(parser.CurrentByteIndex)
        starts.append(parser.CurrentByteIndex)

    def end(tag):
        first, last = stack.pop(), parser.CurrentByteIndex
        self_closed = raw[first:raw.find(b">", first) + 1].rstrip().endswith(b"/>")
        spans[first] = (last if self_closed else raw.find(b">", last) + 1, self_closed, last)

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    nodes = list(root.iter())
    offsets = dict(zip(nodes, starts))
    patches, appends = [], {}
    nil = "{http://www.w3.org/2001/XMLSchema-instance}nil"

    def one(parent, tag, optional=False):
        found = parent.findall(tag)
        if len(found) > 1 or (not found and not optional):
            raise Refused(f"date staging needs exactly one {parent.tag}/{tag}")
        return found[0] if found else None

    def append(parent, child):
        appends.setdefault(parent, []).append(ET.tostring(child, encoding="utf-8"))
        parent.append(child)

    def put(parent, tag, value, optional=False):
        node = one(parent, tag, optional)
        if node is None:
            node = ET.Element(tag)
            node.text = str(value)
            append(parent, node)
            return
        if list(node) or any(k != nil for k in node.attrib):
            raise Refused(f"unsupported date scalar {parent.tag}/{tag}")
        value = str(value)
        if node.text == value and not node.attrib:
            return
        first = offsets[node]
        last, self_closed, close = spans[first]
        if node.attrib or self_closed:
            replacement = f"<{tag}>{value}</{tag}>".encode()
            patches.append((first, last, replacement))
        else:
            patches.append((raw.find(b">", first) + 1, close, value.encode()))
        node.attrib.clear()
        node.text = value

    season, day, year = date
    index = SEASONS.index(season)
    # Stats is one-based (DebugCommands.Day), WorldDate.TotalDays is zero-based.
    days = (year - 1) * 112 + index * 28 + day

    def farmer(f):
        for tag, value in (("dayOfMonthForSaveGame", day), ("seasonForSaveGame", index),
                           ("yearForSaveGame", year)):
            put(f, tag, value, optional=True)
        stats = one(f, "stats")
        if stats.attrib:
            raise Refused("unsupported/nil farmer stats")
        # Pre-1.6 counters can be merged additively by MigrateStatFields. Do not
        # guess at migration state or create a second competing daysPlayed value.
        legacy = one(stats, "daysPlayed", optional=True)
        if legacy is not None and legacy.get(nil) != "true":
            raise Refused("legacy stats/daysPlayed: load and save in 1.6 before date staging")
        legacy_dict = one(stats, "stat_dictionary", optional=True)
        if legacy_dict is not None and len(legacy_dict):
            raise Refused("legacy stats/stat_dictionary: migrate before date staging")
        values = one(stats, "Values")
        if values.attrib:
            raise Refused("unsupported/nil stats/Values")
        entries = [v for v in values if v.findtext("key/string", "").lower() == "daysplayed"]
        if len(entries) > 1:
            raise Refused("duplicate case-insensitive daysPlayed stat")
        if entries:
            put(one(entries[0], "value"), "unsignedInt", days)
        else:
            entry = ET.fromstring(f"<item><key><string>daysPlayed</string></key>"
                                  f"<value><unsignedInt>{days}</unsignedInt></value></item>")
            append(values, entry)

    if info:
        farmer(root)
    else:
        weddings = one(root, "weddingsToday", optional=True)
        wedding = one(root, "weddingToday", optional=True)
        if (wedding is not None and wedding.text != "false") or (weddings is not None and len(weddings)):
            raise Refused("saved wedding day: date staging cannot reschedule weddings")
        countdown = one(root, "countdownToWedding", optional=True)
        if countdown is not None and countdown.text not in (None, "", "0"):
            raise Refused("legacy countdownToWedding: migrate before date staging")
        legacy = one(root, "stats", optional=True)
        if legacy is not None and legacy.get(nil) != "true":
            raise Refused("legacy SaveGame/stats overrides player stats: migrate before date staging")
        for tag, value in (("currentSeason", season), ("dayOfMonth", day), ("year", year)):
            put(root, tag, value)
        farmer(one(root, "player"))
        farmhands = one(root, "farmhands")
        if any(f.tag != "Farmer" for f in farmhands):
            raise Refused("unsupported farmhands collection")
        for f in farmhands:
            farmer(f)
        for tag in ("isRaining", "isSnowing", "isLightning", "isDebrisWeather"):
            put(root, tag, "false")
        # Deterministic sunny staging baseline; tomorrow is NOT today's festival.
        tomorrow = (index, day + 1) if day < 28 else ((index + 1) % 4, 1)
        forecast = "Festival" if tomorrow in FESTIVAL_DATES else "Sun"
        put(root, "weatherForTomorrow", forecast)
        contexts = one(root, "locationWeather")
        default = [v for v in contexts if v.findtext("key/string") == "Default"]
        if len(default) != 1:
            raise Refused("date staging requires exactly one Default locationWeather context")
        weather = one(one(default[0], "value"), "LocationWeather")
        for tag, value, wrapper in (("weather", "Festival" if (index, day) in FESTIVAL_DATES else "Sun", "string"),
                                    ("weatherForTomorrow", forecast, "string"),
                                    *((t, "false", "boolean") for t in
                                      ("isRaining", "isSnowing", "isLightning", "isDebrisWeather", "isGreenRain"))):
            put(one(weather, tag), wrapper, value)
            put(weather, tag[0].upper() + tag[1:], value)
    for parent, chunks in appends.items():
        first = offsets[parent]
        last, self_closed, close = spans[first]
        if self_closed:
            opening = raw[first:last].rstrip()
            replacement = opening[:-2] + b">" + b"".join(chunks) + f"</{parent.tag}>".encode()
            patches.append((first, last, replacement))
        else:
            patches.append((close, close, b"".join(chunks)))
    new = raw
    for first, last, value in sorted(patches, reverse=True):
        new = new[:first] + value + new[last:]
    if ET.tostring(ET.fromstring(new)) != ET.tostring(root):
        raise Refused("date XML span verification failed: no edit")
    return new


def exact_changes(before, after):
    """Untruncated leaf readback, including inserted preview/stat fields."""
    old, new = leaves(before), leaves(after)
    return [dict(path=p, old=old.get(p), new=new.get(p), changed=True)
            for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p)]


def _farm_item_spans(raw):
    """(root, farm index, Farm element, {(location index, domain): [(start, end) byte spans]}) for every
    Farm terrainFeatures/objects dictionary <item>. Expat byte offsets include the BOM, so spans index ``raw``."""
    import xml.etree.ElementTree as ET
    from xml.parsers import expat
    try:
        raw.decode("utf-8-sig")
        root = ET.fromstring(raw)
    except (UnicodeError, ET.ParseError) as exc:
        raise Refused("save is not a valid UTF-8 save XML") from exc
    locations = root.find("locations")
    if locations is None or any(node.tag != "GameLocation" for node in locations):
        raise Refused("save has no supported locations/GameLocation collection")
    farms = [(i, node) for i, node in enumerate(locations) if node.findtext("name") == "Farm"]
    if len(farms) != 1:
        raise Refused("save must contain exactly one Farm location")
    farm_index, farm = farms[0]
    parser = expat.ParserCreate()
    stack, spans = [], {}
    location_index = -1

    def start(tag, attrs):
        nonlocal location_index
        if len(stack) == 2 and stack[-1][0] == "locations":
            location_index += 1
        stack.append((tag, parser.CurrentByteIndex))

    def end(tag):
        if (len(stack) == 5 and [s[0] for s in stack[1:3]] == ["locations", "GameLocation"]
                and stack[-2][0] in ("terrainFeatures", "objects") and tag == "item"):
            offset = parser.CurrentByteIndex
            finish = raw.find(b">", offset) + 1 if raw[offset:offset + 2] == b"</" else offset
            spans.setdefault((location_index, stack[-2][0]), []).append((stack[-1][1], finish))
        stack.pop()
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    return root, farm_index, farm, spans


def clear_bin_area(raw, rect):
    """Remove only Tree/FruitTree and Stone/Twig/Weeds dictionary entries in the inclusive Farm rectangle.
    XML byte spans keep all surviving bytes (BOM, whitespace, other locations, inventories) intact.
    Loose dropped items, buildings, crops, chests, machines and unknown object types are preserved.
    """
    import xml.etree.ElementTree as ET
    if (len(rect) != 4 or any(type(n) is not int or not 0 <= n <= 10000 for n in rect)
            or rect[0] > rect[2] or rect[1] > rect[3]):
        raise Refused("--rect needs inclusive X0 Y0 X1 Y1 within 0..10000, in increasing order")
    root, farm_index, farm, spans = _farm_item_spans(raw)
    removed, cuts = [], []
    for domain in ("terrainFeatures", "objects"):
        container = farm.find(domain)
        if container is None:
            continue
        entries, ranges = list(container), spans.get((farm_index, domain), [])
        if len(entries) != len(ranges) or any(entry.tag != "item" for entry in entries):
            raise Refused("unsupported Farm dictionary XML: no edit")
        for entry, span in zip(entries, ranges):
            node = entry.find("value/TerrainFeature" if domain == "terrainFeatures" else "value/Object")
            if node is None:
                continue
            kind = (node.get("{http://www.w3.org/2001/XMLSchema-instance}type", "").split(":")[-1]
                    if domain == "terrainFeatures" else node.findtext("name"))
            if kind not in (("Tree", "FruitTree") if domain == "terrainFeatures" else ("Stone", "Twig", "Weeds")):
                continue
            try:
                x, y = (int(entry.findtext("key/Vector2/" + axis)) for axis in ("X", "Y"))
            except (TypeError, ValueError) as exc:
                raise Refused("tree/litter has no integer tile key: no edit") from exc
            if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                removed.append(dict(domain=domain, kind=kind, tile=[x, y]))
                cuts.append(span)
                # Removing the element byte span leaves its following whitespace in the document.
                index = list(container).index(entry)
                if index:
                    previous = list(container)[index - 1]
                    previous.tail = (previous.tail or "") + (entry.tail or "")
                else:
                    container.text = (container.text or "") + (entry.tail or "")
                container.remove(entry)
    new = raw
    for first, last in sorted(cuts, reverse=True):
        new = new[:first] + new[last:]
    if ET.tostring(ET.fromstring(new)) != ET.tostring(root):
        raise Refused("XML span verification failed: no edit")
    return new, removed


# S19 staging only. Vanilla Apple Sapling treeId -> Apple fruit itemId.
# FruitTree.cs:51-94, Item.cs:40-83 (1.6.15 serialization); only this reviewed pair is supported.
READY_FRUIT_TREES = {"633": ("613", "Apple")}
FARM_CLEAR_MAX_TILES = 16
FARM_CLEAR_LITTER = ("Twig", "Stone", "Weeds")
# Bush footprint width by LargeTerrainFeature <size> (small/medium/large; tea/walnut bushes are 1 wide).
BUSH_WIDTH = {0: 1, 1: 2, 2: 3, 3: 1, 4: 1}


def parse_farm_tiles(value):
    """`40,7;41,7` -> ((40, 7), (41, 7)): explicit, unique, bounded Farm tiles. No ranges, no wildcards."""
    tiles = []
    for part in (value or "").split(";"):
        m = re.fullmatch(r"\s*(\d{1,4})\s*,\s*(\d{1,4})\s*", part)
        if not m:
            raise Refused(f"farm_clear needs X,Y;X,Y tiles, got {part!r}")
        tiles.append((int(m[1]), int(m[2])))
    if len(set(tiles)) != len(tiles) or not 1 <= len(tiles) <= FARM_CLEAR_MAX_TILES:
        raise Refused(f"farm_clear needs 1..{FARM_CLEAR_MAX_TILES} unique tiles, got {value!r}")
    return tuple(tiles)


def _footprint(x, y, w, h):
    return {(x + dx, y + dy) for dx in range(max(1, w)) for dy in range(max(1, h))}


def _farm_check_footprints(farm, want):
    """Shared fail-closed building/clump/bush footprint check for bounded Farm edits."""
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    for b in (farm.find("buildings") if farm.find("buildings") is not None else ()):
        try:
            area = _footprint(*(int(b.findtext(k)) for k in ("tileX", "tileY", "tilesWide", "tilesHigh")))
        except (TypeError, ValueError) as exc:
            raise Refused("Farm building without an integer footprint: no edit") from exc
        if area & want:
            raise Refused(f"building {b.findtext('buildingType')!r} covers {sorted(area & want)}: no edit")
    for clump in (farm.find("resourceClumps") if farm.find("resourceClumps") is not None else ()):
        try:
            area = _footprint(*(int(clump.findtext(k)) for k in ("tile/X", "tile/Y", "width", "height")))
        except (TypeError, ValueError) as exc:
            raise Refused("Farm resource clump without an integer footprint: no edit") from exc
        if area & want:
            raise Refused(f"resource clump {clump.findtext('parentSheetIndex')} covers {sorted(area & want)}: no edit")
    for feature in (farm.find("largeTerrainFeatures") if farm.find("largeTerrainFeatures") is not None else ()):
        try:
            x, y = (int(float(feature.findtext("tilePosition/" + k))) for k in ("X", "Y"))
        except (TypeError, ValueError) as exc:
            raise Refused("Farm large terrain feature without an integer tile: no edit") from exc
        size = feature.findtext("size")
        width = BUSH_WIDTH.get(int(size), 3) if (size or "").isdigit() else 3
        if _footprint(x, y, width, 1) & want:
            raise Refused(f"{feature.get(xsi, 'large terrain feature')} covers {sorted(_footprint(x, y, width, 1) & want)}: no edit")


def validate_ready_fruit_tree(entry, x, y, tree_id):
    """Narrow 1.6.15 schema check for our ready-Apple dictionary entry, before backup.

    FruitTree.fruit is [XmlElement("fruit")] NetList<Item, NetRef<Item>>:
    each <fruit xsi:type="Object"> IS an Item, not a wrapper around <Item>.
    The offline .NET contract round-trip regression independently checks this rule.
    This validates only the generated subset, not arbitrary saves or game loading.
    """
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"

    def shape(node, tag, children=(), attrs=None):
        if (node is None or node.tag != tag or node.attrib != (attrs or {})
                or tuple(c.tag for c in node) != tuple(children)):
            raise Refused(f"fruit_tree serializer contract mismatch at {tag}: no edit")

    shape(entry, "item", ("key", "value"))
    key, value = entry
    shape(key, "key", ("Vector2",))
    shape(key[0], "Vector2", ("X", "Y"))
    shape(value, "value", ("TerrainFeature",))
    tree = value[0]
    tree_values = dict(growthStage="4", treeId=tree_id, daysUntilMature="0", fruit=None,
                       struckByLightningCountdown="0", health="10", flipped="false", stump="false",
                       greenHouseTileTree="false", growthRate="1")
    shape(tree, "TerrainFeature", tree_values, {xsi: "FruitTree"})
    fruit_id, fruit_name = READY_FRUIT_TREES[tree_id]
    fruit_values = dict(category="-79", name=fruit_name, parentSheetIndex=fruit_id, itemId=fruit_id,
                        isRecipe="false", quality="0", stack="1")
    fruit = tree.find("fruit")
    shape(fruit, "fruit", fruit_values, {xsi: "Object"})
    for parent, values in ((key[0], dict(X=str(x), Y=str(y))), (tree, tree_values), (fruit, fruit_values)):
        for tag, text in values.items():
            if text is not None:
                node = parent.find(tag)
                shape(node, tag)
                if node.text != text:
                    raise Refused(f"fruit_tree serializer value mismatch at {tag}: no edit")


def fruit_tree_ready(raw, value):
    """Insert one ready Apple tree on an empty Farm 3x3; preserve all other bytes.

    `fruit_tree=Farm:X,Y:633:ready`. No clearing/replacement, arbitrary XML or mod IDs.
    Games-closed/backup/atomic-write/exact readback are owned by cmd_edit, as for farm_clear.
    """
    import xml.etree.ElementTree as ET
    from xml.parsers import expat
    m = re.fullmatch(r"Farm:([0-9]{1,3}),([0-9]{1,3}):([0-9]+):ready", value)
    if m is None or m[3] not in READY_FRUIT_TREES:
        raise Refused("fruit_tree needs Farm:X,Y:633:ready (reviewed Apple tree only)")
    x, y = int(m[1]), int(m[2])
    if not (1 <= x < 200 and 1 <= y < 200):
        raise Refused("fruit_tree tile must be within 1..199")
    if b"<!DOCTYPE" in raw.upper():
        raise Refused("DOCTYPE is unsupported in save XML")
    root, farm_index, farm, _ = _farm_item_spans(raw)
    if root.tag != "SaveGame" or len(farm.findall("terrainFeatures")) != 1:
        raise Refused("fruit_tree needs one Farm terrainFeatures dictionary")
    area = _footprint(x - 1, y - 1, 3, 3)
    _farm_check_footprints(farm, area)
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    for domain in ("terrainFeatures", "objects"):
        containers = farm.findall(domain)
        if len(containers) != 1:
            raise Refused(f"fruit_tree needs one Farm {domain} dictionary")
        for entry in containers[0]:
            try:
                if entry.tag != "item":
                    raise ValueError("not item")
                tx, ty = (int(entry.findtext("key/Vector2/" + axis)) for axis in ("X", "Y"))
            except (TypeError, ValueError) as exc:
                raise Refused(f"unsupported Farm {domain} tile key: no edit") from exc
            node = entry.find("value/TerrainFeature")
            tree = node is not None and node.get(xsi) in ("Tree", "FruitTree")
            if (tx, ty) in area or tree and abs(tx - x) <= 2 and abs(ty - y) <= 2:
                raise Refused(f"fruit_tree occupied/nearby tree at {(tx, ty)}: no edit")
    fruit_id, fruit_name = READY_FRUIT_TREES[m[3]]
    # Explicit namespace on the inserted subtree works even if the save uses another prefix.
    chunk = (f'<item xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><key><Vector2><X>{x}</X><Y>{y}</Y>'
             f'</Vector2></key><value><TerrainFeature xsi:type="FruitTree"><growthStage>4</growthStage>'
             f'<treeId>{m[3]}</treeId><daysUntilMature>0</daysUntilMature><fruit xsi:type="Object">'
             f'<category>-79</category><name>{fruit_name}</name><parentSheetIndex>{fruit_id}</parentSheetIndex>'
             f'<itemId>{fruit_id}</itemId><isRecipe>false</isRecipe><quality>0</quality><stack>1</stack>'
             '</fruit><struckByLightningCountdown>0</struckByLightningCountdown><health>10</health>'
             '<flipped>false</flipped><stump>false</stump><greenHouseTileTree>false</greenHouseTileTree>'
             '<growthRate>1</growthRate></TerrainFeature></value></item>').encode("utf-8")
    expected = ET.fromstring(chunk)
    validate_ready_fruit_tree(expected, x, y, m[3])
    parser = expat.ParserCreate()
    stack, bounds = [], []
    location_index = -1

    def start(tag, attrs):
        nonlocal location_index
        if stack == ["SaveGame", "locations"]:
            location_index += 1
        stack.append(tag)
        if stack == ["SaveGame", "locations", "GameLocation", "terrainFeatures"] and location_index == farm_index:
            bounds.append(parser.CurrentByteIndex)

    def end(tag):
        if stack == ["SaveGame", "locations", "GameLocation", "terrainFeatures"] and location_index == farm_index:
            bounds.append(parser.CurrentByteIndex)
        stack.pop()

    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(raw, True)
    if len(bounds) != 2:
        raise Refused("unsupported Farm terrainFeatures XML span")
    first, close = bounds
    opening_end = raw.find(b">", first) + 1
    opening = raw[first:opening_end]
    if opening.rstrip().endswith(b"/>"):
        new = raw[:first] + opening.rstrip()[:-2] + b">" + chunk + b"</terrainFeatures>" + raw[opening_end:]
    else:
        new = raw[:close] + chunk + raw[close:]
    farm.find("terrainFeatures").append(expected)
    if ET.tostring(ET.fromstring(new)) != ET.tostring(root):
        raise Refused("fruit_tree XML span verification failed: no edit")
    return new, dict(key="fruit_tree", old=None, new=value, changed=True, location="Farm", tile=[x, y],
                     treeId=m[3], fruit_item_id=fruit_id, fruit_count=1,
                     readback=dict(rest_identical=True, bom_kept=new.startswith(BOM) == raw.startswith(BOM),
                                   serializer_contract="1.6.15-ready-apple"))


def farm_clear(raw, tiles):
    """Remove ONLY wild Trees and litter at explicit Farm tiles; keep every surviving byte.

    Any other terrain/object or building/clump/bush footprint refuses the whole edit.
    Every listed tile must hold something removable; staging only, never evidence.
    """
    import xml.etree.ElementTree as ET
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    want = set(tiles)
    root, farm_index, farm, spans = _farm_item_spans(raw)
    _farm_check_footprints(farm, want)
    removed, cuts, hit = [], [], set()
    for domain in ("terrainFeatures", "objects"):
        container = farm.find(domain)
        if container is None:
            continue
        entries, ranges = list(container), spans.get((farm_index, domain), [])
        if len(entries) != len(ranges) or any(entry.tag != "item" for entry in entries):
            raise Refused("unsupported Farm dictionary XML: no edit")
        doomed = []
        for entry, span in zip(entries, ranges):
            try:
                tile = tuple(int(entry.findtext("key/Vector2/" + axis)) for axis in ("X", "Y"))
            except (TypeError, ValueError) as exc:
                raise Refused(f"Farm {domain} entry without an integer tile key: no edit") from exc
            if tile not in want:
                continue
            node = entry.find("value/TerrainFeature" if domain == "terrainFeatures" else "value/Object")
            if domain == "terrainFeatures":
                kind = node.get(xsi, "") if node is not None else ""
                ok = kind == "Tree"
                row = dict(domain=domain, kind=kind, tile=list(tile),
                           treeType=node.findtext("treeType") if node is not None else None,
                           growthStage=node.findtext("growthStage") if node is not None else None,
                           stump=node.findtext("stump") if node is not None else None)
            else:
                kind = node.findtext("name") if node is not None else None
                ok = (node is not None and node.get(xsi) is None and kind in FARM_CLEAR_LITTER
                      and node.findtext("type") == "Litter" and node.findtext("bigCraftable") == "false")
                row = dict(domain=domain, kind=kind, tile=list(tile),
                           itemId=node.findtext("itemId") if node is not None else None,
                           type=node.findtext("type") if node is not None else None)
            if not ok:
                raise Refused(f"farm_clear refuses {domain} {kind or '?'} at {tile}: only wild Tree and "
                              f"{'/'.join(FARM_CLEAR_LITTER)} litter may be removed; no edit")
            removed.append(row)
            hit.add(tile)
            cuts.append(span)
            doomed.append(entry)
        for entry in doomed:
            index = list(container).index(entry)
            if index:
                previous = list(container)[index - 1]
                previous.tail = (previous.tail or "") + (entry.tail or "")
            else:
                container.text = (container.text or "") + (entry.tail or "")
            container.remove(entry)
    if hit != want:
        raise Refused(f"farm_clear: nothing removable at {sorted(want - hit)} (stale tile list?): no edit")
    new = raw
    for first, last in sorted(cuts, reverse=True):
        new = new[:first] + new[last:]
    if ET.tostring(ET.fromstring(new)) != ET.tostring(root):
        raise Refused("XML span verification failed: no edit")
    if new[:3] != raw[:3] and raw.startswith(BOM):
        raise Refused("BOM lost: no edit")
    return new, removed


def farm_clear_readback(old_raw, new_raw, tiles):
    """Independent check: the Farm dictionaries lost exactly the entries keyed on ``tiles``; nothing else changed."""
    import xml.etree.ElementTree as ET
    old_root, new_root = ET.fromstring(old_raw), ET.fromstring(new_raw)
    want, gone = set(map(tuple, tiles)), []
    for location in old_root.find("locations"):
        if location.findtext("name") != "Farm":
            continue
        for domain in ("terrainFeatures", "objects"):
            container = location.find(domain)
            for entry in list(container) if container is not None else ():
                tile = tuple(int(entry.findtext("key/Vector2/" + a)) for a in ("X", "Y"))
                if tile in want:
                    gone.append((domain, tile))
                    container.remove(entry)
    return dict(removed=[dict(domain=d, tile=list(t)) for d, t in gone],
                rest_identical=ET.tostring(old_root) == ET.tostring(new_root),
                bom_kept=new_raw.startswith(BOM) == old_raw.startswith(BOM),
                bytes_removed=len(old_raw) - len(new_raw))


def leaves(raw):
    """{path: text} for every leaf element (paths carry sibling indices: a/b[2]/c)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw.decode("utf-8-sig"))
    out = {}

    def walk(el, path):
        kids = list(el)
        if not kids:
            out[path] = (el.text or "").strip()
            return
        seen = {}
        for k in kids:
            tag = k.tag.split("}")[-1]
            seen[tag] = seen.get(tag, 0) + 1
        idx = {}
        for k in kids:
            tag = k.tag.split("}")[-1]
            idx[tag] = idx.get(tag, 0) + 1
            walk(k, f"{path}/{tag}[{idx[tag]}]" if seen[tag] > 1 else f"{path}/{tag}")
    walk(root, root.tag)
    return out


def diff_leaves(old_raw, new_raw, limit=50):
    a, b = leaves(old_raw), leaves(new_raw)
    changed = [(p, a[p], b[p]) for p in a if p in b and a[p] != b[p]]
    added = [p for p in b if p not in a]
    removed = [p for p in a if p not in b]
    return dict(changed=len(changed), added=len(added), removed=len(removed),
                first=[dict(path=p, old=o[:80], new=n[:80]) for p, o, n in changed[:limit]],
                first_added=added[:limit // 5], first_removed=removed[:limit // 5])


def facts(raw):
    """Route-A checklist facts (research §5 step 5) from save bytes."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw.decode("utf-8-sig"))

    def t(path):
        return root.findtext(path)

    def calendar(f):
        return dict(preview={k: f.findtext(k) for k in
                             ("dayOfMonthForSaveGame", "seasonForSaveGame", "yearForSaveGame")},
                    daysPlayed=[i.findtext("value/unsignedInt") for i in f.findall("stats/Values/item")
                                if i.findtext("key/string", "").lower() == "daysplayed"])
    farmhands = []
    for f in root.findall("farmhands/Farmer"):
        farmhands.append(dict(name=f.findtext("name"), money=f.findtext("money"), maxItems=f.findtext("maxItems"),
                              foragingLevel=f.findtext("foragingLevel"), houseUpgradeLevel=f.findtext("houseUpgradeLevel"),
                              experiencePoints=[x.text for x in f.findall("experiencePoints/int")], **calendar(f)))
    return dict(allowChatCheats=t("allowChatCheats"), mine_lowestLevelReached=t("mine_lowestLevelReached"),
                date=dict(day=t("dayOfMonth"), season=t("currentSeason"), year=t("year")),
                host=dict(name=t("player/name"), money=t("player/money"), maxItems=t("player/maxItems"),
                          houseUpgradeLevel=t("player/houseUpgradeLevel"), **calendar(root.find("player"))),
                farmhands=farmhands,
                weather={k: t(k) for k in ("isRaining", "isSnowing", "isLightning", "isDebrisWeather",
                                           "weatherForTomorrow")},
                locationWeather={i.findtext("key/string"): {
                    k: i.findtext("value/LocationWeather/" + k) for k in
                    ("Weather", "WeatherForTomorrow", "IsRaining", "IsSnowing", "IsLightning", "IsDebrisWeather",
                     "IsGreenRain")} for i in root.findall("locationWeather/item")})


def _root(raw):
    import xml.etree.ElementTree as ET
    try:
        return ET.fromstring(raw.decode("utf-8-sig"))
    except (UnicodeError, ET.ParseError) as exc:
        raise Refused("save must be valid UTF-8 XML") from exc


def building_rows(raw):
    """[(buildingType, uniqueName, (tileX, tileY))] for every Building interior with a unique name (cabins included)."""
    rows = []
    for b in _root(raw).iter("Building"):
        indoors = b.find("indoors")
        unique = ((indoors.findtext("uniqueName") if indoors is not None else None) or "").strip()
        if unique:
            rows.append((b.findtext("buildingType") or "?", unique, (b.findtext("tileX"), b.findtext("tileY"))))
    return rows


def save_location_names(raw):
    """Names /debug warp may use: the static map names plus each GameLocation's NameOrUniqueName (building interiors
    and cabins included), as the game's Utility.fuzzyLocationSearch bank keys them."""
    root = _root(raw)
    elements = list(root.iter("GameLocation")) + [b.find("indoors") for b in root.iter("Building")
                                                  if b.find("indoors") is not None]
    names = set(STATIC_LOCATIONS)
    for el in elements:
        name = ((el.findtext("uniqueName") or "").strip() or (el.findtext("name") or "").strip())
        if name:
            names.add(name)
    return names


def coop_barn_animals(root):
    """[(FarmAnimal element, buildingType)] for every animal in a Coop/Barn interior, in document order."""
    out = []
    for b in root.iter("Building"):
        kind = b.findtext("buildingType") or ""
        if "Coop" not in kind and "Barn" not in kind:
            continue
        indoors = b.find("indoors")
        if indoors is None:
            raise Refused(f"{kind} building has no indoors element: unexpected save shape, no edit")
        for item in indoors.findall("animals/item"):
            animal = item.find("value/FarmAnimal")
            if animal is None:
                raise Refused(f"{kind} animals entry without value/FarmAnimal: unexpected save shape, no edit")
            out.append((animal, kind))
    return out


def _leaf_pattern(tag):
    return re.compile(rf"<{tag}(?:\s[^>]*)?/>|<{tag}(?:\s[^>]*)?>[^<]*</{tag}>")


def animal_produce_plan(species, age, current, farm):
    """(planned currentProduce or None, daysSinceLastLay or None, status). Never invents a value: produce is set only
    from the installed FarmAnimals data (one unconditional entry, age >= DaysToMature, isAdult FarmAnimal.cs 1190-1197)."""
    if current:
        return None, None, f"kept currentProduce {current}"
    if farm is None:
        return None, None, "skipped: FarmAnimals.xnb unreadable (no DaysToMature / produce id)"
    data = farm.get(species)
    if data is None:
        return None, None, f"skipped: no FarmAnimals entry for {species!r}"
    if age < data["days_to_mature"]:
        return None, None, f"skipped: baby (age {age} < DaysToMature {data['days_to_mature']})"
    produce = data["produce"]
    if len(produce) != 1 or produce[0]["condition"] is not None or produce[0]["minimum_friendship"]:
        return None, None, f"skipped: {len(produce)} produce entries (need exactly one unconditional)"
    # 99 is the game's own value for a new adult (FarmAnimal.cs growFully, line 1268)
    return produce[0]["item_id"], "99", f"set {produce[0]['item_id']}"


def animals_ready(raw, farm=None):
    """(new bytes, rows) for `edit --set animals=ready`. Byte-span edits inside each Coop/Barn <FarmAnimal> only.
    Always: wasPet=false, fullness=255, happiness=255 (FarmAnimal.cs 1.6.15 wasPet line 117, fullness 111, happiness 108;
    255 = the constructor value, lines 360-361). Produce (currentProduce, daysSinceLastLay): see animal_produce_plan.
    farm = FarmAnimals data from xnb_items.build_farm_animals (None when unreadable). Refuses on unexpected shape;
    the leaf diff must contain only the FarmAnimal fields planned."""
    text, bom = _decode(raw)
    root = _root(raw)
    targets = coop_barn_animals(root)
    if not targets:
        raise Refused("no FarmAnimal in a Coop/Barn interior: nothing to make ready, no edit")
    # Real 1.6 saves serialize a bare <FarmAnimal> (no attributes); fixtures may carry xmlns attributes.
    spans = list(re.finditer(r"<FarmAnimal(?:\s[^>]*[^/])?>.*?</FarmAnimal>", text, re.S))
    everyone = list(root.iter("FarmAnimal"))
    if len(spans) != len(everyone):
        raise Refused("FarmAnimal spans in the text do not match the XML elements: no edit")
    where = {id(el): i for i, el in enumerate(everyone)}
    cuts, rows, expected = [], [], 0
    for animal, kind in targets:
        for tag in ("type", "age", "wasPet", "fullness", "happiness", "daysSinceLastLay"):
            if len(animal.findall(tag)) != 1:
                raise Refused(f"FarmAnimal in {kind} has {len(animal.findall(tag))} <{tag}> (expected 1): no edit")
        if len(animal.findall("currentProduce")) > 1:
            raise Refused(f"FarmAnimal in {kind} has several <currentProduce>: no edit")
        age = _int(animal.findtext("age"), 0, 100000, "FarmAnimal age")
        species = (animal.findtext("type") or "").strip()
        current = (animal.findtext("currentProduce") or "").strip()
        plan = {"wasPet": "false", "fullness": "255", "happiness": "255"}
        if animal.find("currentProduce") is None:
            # Real 1.6 saves omit a null <currentProduce>; never insert serializer elements. Fed + happy animals
            # get their produce from the native overnight dayUpdate instead (sleep one night after this edit).
            status = "skipped: <currentProduce> absent in save (produce comes from the next native dayUpdate)"
        else:
            produce, days, status = animal_produce_plan(species, age, current, farm)
            if produce is not None:
                plan.update(currentProduce=produce, daysSinceLastLay=days)
        span = spans[where[id(animal)]]
        body = span.group(0)
        for tag, value in plan.items():
            hits = list(_leaf_pattern(tag).finditer(body))
            if len(hits) != 1:
                raise Refused(f"<{tag}> occurs {len(hits)} times in a {kind} FarmAnimal (expected 1): no edit")
            body = body[:hits[0].start()] + f"<{tag}>{value}</{tag}>" + body[hits[0].end():]
            expected += (animal.findtext(tag) or "").strip() != value
        cuts.append((span.start(), span.end(), body))
        rows.append(dict(building=kind, type=species, age=age, planned=plan, produce=status))
    new_text = text
    for first, last, body in sorted(cuts, reverse=True):
        new_text = new_text[:first] + body + new_text[last:]
    new = _encode(new_text, bom)
    changed = exact_changes(raw, new)
    fields_only = all(re.sub(r"\[\d+\]$", "", c["path"].rsplit("/", 1)[-1]) in ANIMAL_FIELDS and "FarmAnimal" in c["path"]
                      for c in changed)
    if not fields_only or len(changed) != expected:
        raise Refused("animal readiness verify failed (unexpected leaf changes): no edit")
    return new, rows


def farm_hay(raw, value):
    """(new bytes, row) for `edit --set hay=N`: the Farm GameLocation's own <piecesOfHay> (1.6 keeps silo hay per
    location; building interiors carry their own, untouched). Capped at the Farm buildings' summed <hayCapacity>
    (one Silo = 240). Byte-span edit of exactly one leaf; refuses on unexpected shape."""
    v = str(value).strip()
    if not re.fullmatch(r"\d+", v):
        raise Refused(f"hay must be a non-negative integer, got {value!r}")
    text, bom = _decode(raw)
    root = _root(raw)
    xsi = "{http://www.w3.org/2001/XMLSchema-instance}type"
    locations = root.find("locations")
    farms = [el for el in (locations if locations is not None else ()) if el.get(xsi) == "Farm"]
    if len(farms) != 1:
        raise Refused(f"{len(farms)} Farm GameLocation elements in the save (expected 1): no edit")
    farm = farms[0]
    own = farm.findall("piecesOfHay")
    if len(own) != 1:
        raise Refused(f"the Farm has {len(own)} own <piecesOfHay> (expected 1): no edit")
    capacity = 0
    for b in farm.findall("buildings/Building"):
        cap = (b.findtext("hayCapacity") or "0").strip()
        capacity += _int(cap, 0, 100000, "Building hayCapacity")
    n = int(v)
    if capacity <= 0:
        raise Refused("the Farm has no silo capacity (summed hayCapacity 0): no edit")
    if n > capacity:
        raise Refused(f"hay {n} exceeds the Farm silo capacity {capacity}: no edit")
    everyone = list(root.iter("piecesOfHay"))
    spans = list(_leaf_pattern("piecesOfHay").finditer(text))
    if len(spans) != len(everyone):
        raise Refused("<piecesOfHay> spans in the text do not match the XML elements: no edit")
    span = spans[[id(el) for el in everyone].index(id(own[0]))]
    old = (own[0].text or "").strip()
    new_text = text[:span.start()] + f"<piecesOfHay>{n}</piecesOfHay>" + text[span.end():]
    new = _encode(new_text, bom)
    changed = exact_changes(raw, new)
    if old != str(n) and (len(changed) != 1 or not changed[0]["path"].endswith("piecesOfHay")):
        raise Refused("hay verify failed (unexpected leaf changes): no edit")
    return new, dict(key="hay", old=old, new=str(n), changed=old != str(n), capacity=capacity)


# ============================================================ /debug scripts (pure)
def _int(v, lo, hi, what):
    if not re.fullmatch(r"-?\d+", str(v)) or not lo <= int(v) <= hi:
        raise Refused(f"{what} must be an integer {lo}..{hi}, got {v!r}")
    return int(v)


def _ints(args, usage, *specs, required=None):
    """args must be integers per specs [(lo, hi, name)]; `required` of them at least (default all)."""
    need = len(specs) if required is None else required
    if not need <= len(args) <= len(specs):
        raise Refused(f"usage: {usage}")
    for v, (lo, hi, what) in zip(args, specs):
        _int(v, lo, hi, what)


def _words(args, usage, n=1, choices=None):
    if len(args) != n or (choices and args[0].lower() not in choices):
        raise Refused(f"usage: {usage}")


# `seenevent` (DebugCommands.SeenEvent, alias eventSeen) marks ONE already-known event seen for the typing farmer.
# Only events whose cutscene would interrupt a proof-save acceptance route are allowed (FINAL Part B proof save).
SEEN_EVENTS = {"100162": "first Mine entry (a native event id)"}


def _check_seen_event(args, _catalog):
    if len(args) != 1 or args[0] not in SEEN_EVENTS:
        raise Refused(f"usage: seenevent <id>; allowed ids: {', '.join(sorted(SEEN_EVENTS))}")
    return f"event {args[0]} = {SEEN_EVENTS[args[0]]}"


def _check_item(args, catalog):
    if not args:
        raise Refused("usage: item <qualified id> [count] [quality]")
    qid = args[0]
    if not re.fullmatch(r"\((O|BC|W|T)\)[A-Za-z0-9_.]+", qid):
        raise Refused(f"item id {qid!r} is not a qualified (O)/(BC)/(W)/(T) id")
    if catalog is None:
        raise Refused("no item catalog (xnb_items.py failed and no --catalog): item ids cannot be validated")
    if qid not in catalog:
        raise Refused(f"item id {qid} is not in the installed game's data")
    _ints(args[1:], "item <qid> [count] [quality]", (1, 999, "count"), (0, 4, "quality"), required=0)
    if len(args) > 2 and int(args[2]) not in QUALITIES:
        raise Refused(f"quality must be one of {QUALITIES}")
    return f"{qid} = {catalog[qid]}"


def _check_itemnamed(args, catalog):
    if not args:
        raise Refused('usage: itemnamed "<internal name>" [count] [quality]')
    if catalog is None:
        raise Refused("no item catalog (xnb_items.py failed and no --catalog): names cannot be validated")
    if args[0] not in {v for k, v in catalog.items() if k.startswith("(O)")}:
        raise Refused(f"{args[0]!r} is not an (O) internal name in the installed game's data (ItemNamed takes (O) only)")
    _ints(args[1:], "itemnamed <name> [count] [quality]", (1, 999, "count"), (0, 4, "quality"), required=0)
    return f"(O) {args[0]}"


def _none(cmd):
    def check(args, _catalog):
        if args:
            raise Refused(f"`{cmd}` takes no arguments, got {args}")
    return check


def _friendship(args, _catalog):
    if len(args) != 2 or not re.fullmatch(r"[A-Za-z]+", args[0]):
        raise Refused("usage: friendship <npc> N (0..3500; NPC names are not validated: check the result in game)")
    _int(args[1], 0, 3500, "points")


def _build(args, _catalog):
    if len(args) != 3:
        raise Refused('usage: build "<building type>" X Y (verify the exact name in game)')
    _int(args[1], 0, 200, "x")
    _int(args[2], 0, 200, "y")


CHECKS = {
    "money": lambda a, c: _ints(a, "money N", (0, 10_000_000, "money")),
    "experience": lambda a, c: (_words(a[:1], f"experience <{'|'.join(SKILLS)}> N", 1, SKILLS),
                                _ints(a[1:], "experience <skill> N", (0, 15000, "xp"))),
    "friendship": _friendship,
    "backpack": lambda a, c: _ints(a, "backpack N", (1, 24, "slots")),
    "minelevel": lambda a, c: _ints(a, "minelevel N", (1, 120, "level")),
    "houseupgrade": lambda a, c: _ints(a, "houseupgrade N", (0, 3, "level")),
    "build": _build,
    "spawncoopsandbarns": lambda a, c: _ints(a, "spawncoopsandbarns N", (1, 3, "count")),
    "finishallbuilds": _none("finishallbuilds"),
    "animal": lambda a, c: _words(a, "animal <name>"),
    "horse": lambda a, c: _ints(a, "horse [X Y]", (0, 200, "x"), (0, 200, "y"), required=0),
    "dog": _none("dog"),
    "cat": _none("cat"),
    "pole": lambda a, c: _ints(a, "pole N (1 Training, 2 Fiberglass, 3 Iridium, else Bamboo)", (0, 3, "pole")),
    "item": _check_item,
    "itemnamed": _check_itemnamed,
    "completecc": _none("completecc"),
    "allbundles": _none("allbundles"),
    "bundle": lambda a, c: _words(a, "bundle <key>"),
    "crafting": _none("crafting"),
    "cooking": _none("cooking"),
    "year": lambda a, c: _ints(a, "year N", (1, 99, "year")),
    "day": lambda a, c: _ints(a, "day N", (1, 28, "day")),
    "season": lambda a, c: _words(a, "season <spring|summer|fall|winter>", 1, SEASONS),
    "heal": _none("heal"),
    "energize": lambda a, c: _ints(a, "energize [N]", (1, 508, "stamina"), required=0),
    "mailfortomorrow": lambda a, c: _words(a, "mailfortomorrow <id>"),
    "growcrops": lambda a, c: _ints(a, "growcrops N", (1, 28, "days")),
    "seenevent": _check_seen_event,
    # ADR 242: bench staging on a disposable test save. Both act on the typing farmer only.
    # completequest marks one quest in her log complete so its reward waits (QuestLog detail page);
    # levelup opens the game's own LevelUpMenu (skill 0 farming .. 4 combat, 5 luck; level 5/10 = profession page).
    "completequest": lambda a, c: _ints(a, "completequest <quest id>", (1, 99999, "quest id")),
    "levelup": lambda a, c: _ints(a, "levelup <skill 0-5> <level 1-10>", (0, 5, "skill"), (1, 10, "level")),
    # fillbin puts five fixed items in the shipping bin; sleep answers the game's own Sleep_Yes for the typing farmer;
    # festival <id> sets that farmer's season/day/start time and warps into the festival map (DebugCommands.Festival).
    "fillbin": _none("fillbin"),
    "sleep": _none("sleep"),
    "festival": lambda a, c: _words(a, "festival <" + "|".join(FESTIVALS) + ">", 1, FESTIVALS),
}
FESTIVALS = ()
ALIASES = {"hu": "houseupgrade", "house": "houseupgrade", "eventseen": "seenevent"}


def _suggest(token, names):
    prefix = token.split("~")[0].strip().lower()
    hits = sorted(n for n in names if prefix and n.lower().startswith(prefix))
    if hits:
        return f"; did you mean {hits[0]!r}?" + (f" (also {', '.join(map(repr, hits[1:4]))})" if hits[1:] else "")
    return "; copy the exact name from `stage_save.py locations`"


def _check_warp(args, names):
    """`warp <location> [X Y]`: the location must be an exact name in the save (never a compact or truncated token)."""
    if len(args) not in (1, 3):
        raise Refused("usage: warp <location> [X Y]   (exact names: stage_save.py locations)")
    if names is None:
        raise Refused("the save's location names cannot be read: warp names cannot be validated")
    name = args[0]
    if "~" in name:
        raise Refused(f"{name!r} contains '~' (a compact token from the ARTI context, not a name)" + _suggest(name, names))
    if name not in names:
        raise Refused(f"{name!r} is not a location name in the save" + _suggest(name, names))
    if len(args) == 3:
        _int(args[1], 0, 200, "x")
        _int(args[2], 0, 200, "y")
    return f"location {name}"


def parse_script(text, catalog, target, allow_world=False, owner_ok=False, confirm_world=False, locations=None, *,
                 bench_clock=False):
    """[[{line, cmd, note}...] per block], [errors]. Nothing is typed when errors is not empty."""
    blocks, cur, errors = [], [], []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if line.startswith("#"):
            continue
        try:
            row = check_line(line, catalog, target, allow_world, owner_ok, confirm_world, locations,
                             bench_clock=bench_clock)
            cur.append(dict(row, n=n))
        except Refused as e:
            errors.append(f"line {n}: {line!r}: {e}")
    if cur:
        blocks.append(cur)
    if not blocks and not errors:
        errors.append("the script has no command lines")
    return blocks, errors


def check_line(line, catalog, target, allow_world=False, owner_ok=False, confirm_world=False, locations=None, *,
               bench_clock=False):
    if not line.isprintable() or len(line) > 200:
        raise Refused("unprintable or longer than 200 characters")
    try:
        parts = shlex.split(line)
    except ValueError as e:
        raise Refused(f"cannot split: {e}")
    if parts[0] == "/money":
        CHECKS["money"](parts[1:], catalog)
        return dict(line=line, cmd="money", note=None)
    if parts[0] != "/debug" or len(parts) < 2:
        raise Refused("only `/debug <cmd> ...` and `/money N` lines are allowed")
    cmd = ALIASES.get(parts[1].lower(), parts[1].lower())
    if cmd in REFUSED:
        raise Refused(f"`{cmd}` is refused: {REFUSED[cmd]}")
    if cmd == "warp":
        return dict(line=line, cmd=cmd, note=_check_warp(parts[2:], locations))
    if cmd == "time" and bench_clock and target == "host" and owner_ok:
        _ints(parts[2:], "time HHMM", (600, 2350, "game clock"))
        if int(parts[2]) % 100 not in range(0, 60, 10):
            raise Refused("time HHMM requires a ten-minute clock tick")
        return dict(line=line, cmd=cmd, note="disposable bench clock")
    if cmd not in CHECKS:
        raise Refused(f"`{cmd}` is not in the staging allowlist (SAVE_STAGING_RESEARCH.md §2)")
    if cmd in HOST_ONLY and target != "host":
        raise Refused(f"`{cmd}` is host-only (world state / 'Only the host'): use --target host")
    if cmd in CONFIRM_WORLD and not (owner_ok and confirm_world):
        raise Refused(f"`{cmd}` changes the whole Community Center: needs --owner-ok AND --confirm-world")
    if cmd in WORLD and target == "farmhand" and not allow_world:
        raise Refused(f"`{cmd}` changes world state: run it on the host (--target host) or pass --allow-world-on-farmhand")
    note = CHECKS[cmd](parts[2:], catalog)
    return dict(line=line, cmd=cmd, note=note if isinstance(note, str) else None)


# ============================================================ live ops (never used by the offline tests)
class LiveOps:
    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def read_bytes(self, p):
        return ipc.read_bytes(Path(p))

    def exists(self, p):
        return Path(p).exists()

    def write_atomic(self, p, data):
        p = Path(p)
        tmp = p.with_name(p.name + ".arti-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def copytree(self, src, dst):
        shutil.copytree(src, dst)

    def rmtree(self, p):
        shutil.rmtree(p)

    def unlink(self, p):
        Path(p).unlink()

    def files(self, d):
        return sorted(x for x in Path(d).iterdir() if x.is_file())

    def backups(self):
        return sorted(paths.PREP.glob("save-backup-*"))

    def game_running(self):
        pids = [wf.image_pids(e) for e in GAME_EXES]
        return None if any(p is None for p in pids) else sorted(x for p in pids for x in p)

    def trial_running(self):
        return wf.trial_running()

    def window(self, h):
        return wf.window(h)

    def read_text(self, p):
        try:
            return ipc.read_text(Path(p), encoding="utf-8-sig").strip()
        except OSError:
            return None

    def read_json(self, p):
        try:
            return json.loads(ipc.read_text(Path(p), encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def write_text(self, p, text):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        ipc.publish(Path(p), text, critical=True)

    def append_jsonl(self, p, row):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def mkdir(self, p):
        Path(p).mkdir(parents=True, exist_ok=False)

    def region_mad(self, a_png, b_png, box):
        """Mean abs diff (0..1) of one box in two PNGs; None without Pillow or a box (the caller then aborts)."""
        if not box:
            return None
        try:
            from PIL import Image, ImageChops, ImageStat
        except ImportError:
            return None
        a = Image.open(a_png).convert("RGB").crop(tuple(box))
        b = Image.open(b_png).convert("RGB").crop(tuple(box))
        return round(sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 / 255, 4)

    def staging_count(self):
        return sum(1 for d in paths.RUN.glob("staging-*") if d.is_dir()) if paths.RUN.exists() else 0

    def ui(self, *args):
        p = subprocess.run([sys.executable, os.path.join(HERE, "ui_op.py"), *map(str, args)], capture_output=True,
                           text=True, timeout=120)
        return p.returncode, p.stdout, p.stderr

    def catalog(self, path=None):
        if path:
            return json.loads(ipc.read_text(Path(path), encoding="utf-8"))
        import xnb_items
        try:
            return xnb_items.build_catalog()
        except (OSError, xnb_items.XnbError):
            return None

    def farm_animals(self):
        """DaysToMature and produce ids from the installed Content/Data/FarmAnimals.xnb; None when unreadable."""
        import xnb_items
        try:
            return xnb_items.build_farm_animals()
        except (OSError, xnb_items.XnbError):
            return None


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def need_game_closed(ops):
    running = ops.game_running()
    if running is None:
        raise Refused("cannot list the game processes (tasklist failed): refusing a save edit")
    if running:
        raise Refused(f"the game is running (pids {running}): close the HOST and the farmhand before a save edit")


def do_backup(ops, save_name=None):
    name = save_name or paths.SAVE_NAME
    src = paths.SAVES / name
    if not ops.exists(src / name):
        raise Refused(f"save {src / name} not found")
    base = paths.PREP / f"save-backup-{name.split('_')[0]}-{wf.stamp(ops.now())}"   # prep_trial approval globs this
    dst, i = base, 1
    while ops.exists(dst):
        i += 1
        dst = Path(f"{base}-{i}")
    ops.copytree(src, dst)
    manifest = {f.name: _sha(ops.read_bytes(f)) for f in ops.files(dst)}
    if (manifest.get(name) != _sha(ops.read_bytes(src / name))
            or any(digest != _sha(ops.read_bytes(src / filename)) for filename, digest in manifest.items())):
        raise Refused(f"backup {dst} does not match the save (sha256): not editing")
    ops.write_text(dst / "arti-backup-manifest.json", json.dumps(dict(utc=wf.utc(ops.now()), source=str(src),
                                                                     sha256=manifest), indent=1))
    steam = ops.exists(paths.SAVES / "steam_autocloud.vdf")
    return dict(backup=str(dst), files=len(manifest), save_sha256=manifest.get(name), steam_cloud_marker=steam)


def cmd_status(a, ops):
    import xml.etree.ElementTree as ET
    p = save_file(a.save)
    raw = ops.read_bytes(p)
    info = p.with_name("SaveGameInfo")
    preview = ET.fromstring(ops.read_bytes(info)) if ops.exists(info) else None
    print(json.dumps(dict(save=str(p), sha256=_sha(raw), game_running=ops.game_running(), **facts(raw),
                          SaveGameInfo=dict(preview={k: preview.findtext(k) for k in
                                                    ("dayOfMonthForSaveGame", "seasonForSaveGame", "yearForSaveGame")},
                                            daysPlayed=[i.findtext("value/unsignedInt")
                                                        for i in preview.findall("stats/Values/item")
                                                        if i.findtext("key/string", "").lower() == "daysplayed"])
                          if preview is not None else None), indent=1))
    return 0


def cmd_backup(a, ops):
    need_game_closed(ops)
    r = do_backup(ops, a.save)
    print(json.dumps(r, indent=1))
    if r["steam_cloud_marker"]:
        print("WARNING: steam_autocloud.vdf present: turn Steam Cloud off for Stardew before editing (research step 0)")
    return 0


def cmd_edit(a, ops):
    edits = {}
    for kv in a.set:
        k, sep, v = kv.partition("=")
        if not sep:
            raise Refused(f"--set needs KEY=VALUE, got {kv!r}")
        k = k.strip()
        if k in edits:
            raise Refused(f"duplicate --set key {k!r}")
        edits[k] = v
    if "year" in edits and "date" not in edits:
        raise Refused("--set year=N requires --set date=SEASON:DAY")
    animals = edits.pop("animals", None)
    if animals is not None and animals.strip().lower() != "ready":
        raise Refused(f"animals must be `ready`, got {animals!r}")
    hay = edits.pop("hay", None)
    clear = edits.pop("farm_clear", None)
    clear_tiles = parse_farm_tiles(clear) if clear is not None else None
    fruit_tree = edits.pop("fruit_tree", None)
    mail_received = edits.pop("mail_received", None)
    need_game_closed(ops)
    p = save_file(a.save)
    raw = ops.read_bytes(p)
    new, changes = apply_edits(raw, {k: v for k, v in edits.items() if k not in ("date", "year")})
    if mail_received is not None:
        new, mail_row = farmer_mail_received(new, mail_received)
        changes.append(mail_row)
    if fruit_tree is not None:
        new, tree_row = fruit_tree_ready(new, fruit_tree)
        changes.append(tree_row)
    if clear_tiles is not None:
        # PART_B_MINES_PLAN: staging only, never acceptance evidence. Exact tiles, trees/litter only.
        before = new
        new, clear_rows = farm_clear(new, clear_tiles)
        check = farm_clear_readback(before, new, clear_tiles)
        if not check["rest_identical"] or not check["bom_kept"] or len(check["removed"]) != len(clear_rows):
            raise Refused("farm_clear readback mismatch: no edit")
        changes.append(dict(key="farm_clear", old=clear, new="removed", changed=True, removed=clear_rows,
                            readback=check))
    if hay is not None:
        new, hay_row = farm_hay(new, hay)
        changes.append(hay_row)
    if animals is not None:
        new, animal_rows = animals_ready(new, ops.farm_animals())
        for row in animal_rows:
            if row["produce"].startswith("skipped"):
                print(f"ANIMALS SKIPPED produce: {row['building']} {row['type']} age {row['age']}: {row['produce']}",
                      file=sys.stderr)
        changes.append(dict(key="animals", old="(per Coop/Barn FarmAnimal)", new="ready", changed=True, animals=animal_rows))
    originals, planned = {p: raw}, {p: new}
    if "date" in edits:
        import xml.etree.ElementTree as ET
        try:
            year = edits.get("year", ET.fromstring(raw).findtext("year"))
            date = parse_date(edits["date"], year)
        except ET.ParseError as exc:
            raise Refused("invalid save XML") from exc
        info = p.with_name("SaveGameInfo")
        if not ops.exists(info):
            raise Refused("SaveGameInfo missing: date staging requires both files")
        originals[info] = ops.read_bytes(info)
        planned[p] = apply_date_edits(new, date)
        planned[info] = apply_date_edits(originals[info], date, info=True)
    file_plans = [dict(file=str(f), before_sha256=_sha(originals[f]), after_sha256=_sha(data),
                       changes=exact_changes(originals[f], data), diff=diff_leaves(originals[f], data, 1000000))
                  for f, data in planned.items()]
    plan = dict(save=str(p), changes=changes, before_sha256=_sha(raw), files=file_plans)
    if all(planned[f] == data for f, data in originals.items()):
        print(json.dumps(dict(plan, written=False, note="every value already set: nothing written, no backup needed"), indent=1))
        return 0
    if a.dry_run:
        print(json.dumps(dict(plan, written=False, dry_run=True), indent=1))
        return 0
    bk = do_backup(ops, a.save)                               # ALWAYS before the write
    need_game_closed(ops)
    for f, data in originals.items():
        if ops.read_bytes(Path(bk["backup"]) / f.name) != data or ops.read_bytes(f) != data:
            raise Refused(f"{f.name} changed during backup: no edit")
    written, attempted, error = [], [], None
    try:
        for f, data in planned.items():
            if data != originals[f]:
                # Check again between files. A two-file replace is not one filesystem transaction.
                need_game_closed(ops)
                if ops.read_bytes(f) != originals[f]:
                    raise Refused(f"{f.name} changed before write")
                attempted.append(str(f))
                ops.write_atomic(f, data)
                written.append(str(f))
        ok = all(ops.read_bytes(f) == data for f, data in planned.items())
    except (OSError, Refused) as exc:
        ok, error = False, str(exc)
    import xml.etree.ElementTree as ET
    readback = []
    for f, data in planned.items():
        row = dict(file=str(f))
        try:
            after = ops.read_bytes(f)
            ok = ok and after == data
            row.update(sha256=_sha(after), matches_expected=after == data,
                       diff=diff_leaves(originals[f], after, 1000000))
        except (OSError, UnicodeError, ET.ParseError) as exc:
            ok = False
            row["error"] = str(exc)
        readback.append(row)
    report = dict(plan, written=bool(attempted), written_files=written, attempted_files=attempted, backup=bk["backup"],
                  after_sha256=readback[0].get("sha256"), verified=ok, diff=readback[0].get("diff"),
                  readback=readback, error=error)
    ops.append_jsonl(paths.RUN / "staging-log.jsonl", dict(utc=wf.utc(ops.now()), tag="STAGING", op="edit", **report))
    print(json.dumps(report, indent=1))
    if not ok:
        print(f"EDIT VERIFY FAILED: restore from {bk['backup']}", file=sys.stderr)
        return 4
    return 0


def cmd_diff(a, ops):
    p = save_file(a.save)
    bks = ops.backups()
    src = Path(a.backup) if a.backup else (bks[-1] if bks else None)
    if src is None:
        raise Refused("no save-backup-* folder in PREP")
    old = ops.read_bytes(src / (a.save or paths.SAVE_NAME))
    info = p.with_name("SaveGameInfo")
    info_diff = (diff_leaves(ops.read_bytes(src / "SaveGameInfo"), ops.read_bytes(info), a.max)
                 if ops.exists(info) and ops.exists(src / "SaveGameInfo") else None)
    print(json.dumps(dict(save=str(p), backup=str(src), **diff_leaves(old, ops.read_bytes(p), a.max),
                          SaveGameInfo=info_diff), indent=1))
    return 0


# ============================================================ savestates (reusable staged saves for live tests)
SNAP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def snap_root():
    return paths.PREP / "savestates"


def snap_dir(name):
    if not SNAP_NAME.fullmatch(name or ""):
        raise Refused("snapshot NAME must be 1..64 of [A-Za-z0-9._-], starting with a letter or digit")
    return snap_root() / name


def _hash_folder(ops, folder):
    return {f.name: _sha(ops.read_bytes(f)) for f in ops.files(folder)}


def verify_snapshot(ops, d, sn):
    manifest = ops.read_json(d / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict) or not ops.exists(d / "save"):
        raise Refused(f"snapshot {d.name}: manifest.json or save/ missing or unreadable: no restore")
    if manifest.get("save_name") != sn or sn not in manifest["files"]:
        raise Refused(f"snapshot {d.name} is for save {manifest.get('save_name')!r}, not {sn!r}: no restore")
    if _hash_folder(ops, d / "save") != manifest["files"]:
        raise Refused(f"snapshot {d.name} does not match its manifest (file list or sha256): no restore")
    return manifest


def cmd_snapshot(a, ops):
    d = snap_dir(a.name)
    sn = a.save or paths.SAVE_NAME
    need_game_closed(ops)
    src = paths.SAVES / sn
    if not ops.exists(src / sn):
        raise Refused(f"save {src / sn} not found")
    if ops.exists(d):
        if not a.overwrite:
            raise Refused(f"snapshot {a.name} exists: pass --overwrite to replace it")
        ops.rmtree(d)
    source = _hash_folder(ops, src)
    ops.copytree(src, d / "save")
    copied = _hash_folder(ops, d / "save")
    if copied != source:
        ops.rmtree(d)
        raise Refused(f"snapshot copy of {src} does not match the save (sha256; the game may have written): removed")
    manifest = dict(name=a.name, utc=wf.utc(ops.now()), note=a.note or None, save_name=sn, source=str(src),
                    date=facts(ops.read_bytes(src / sn))["date"], files=copied)
    ops.write_text(d / "manifest.json", json.dumps(manifest, indent=1))
    ops.append_jsonl(paths.RUN / "staging-log.jsonl", dict(utc=manifest["utc"], tag="SAVESTATE", op="snapshot",
                                                           name=a.name, files=len(copied), date=manifest["date"]))
    print(json.dumps(manifest, indent=1))
    return 0


AFTER_SAVE_RE = re.compile(r"Context: after save, starting (\w+) (\d+) Y(\d+)")
BEFORE_SAVE = "Context: before save"
NIGHT_LOG_TAIL_BYTES = 8 << 20


def night_save_evidence(log_path, date):
    """Did the game's own night save for ``date`` complete? Read-only HOST SMAPI log check.

    Confirmed only when the newest SMAPI "Context: after save, starting <season> <day> Y<year>"
    names ``date`` and no newer "Context: before save" follows it (a save still in progress).
    Returns dict(confirmed=True, basis, log, line, text) or dict(confirmed=False, reason, log).
    """
    log = str(log_path)
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - NIGHT_LOG_TAIL_BYTES)
            f.seek(start)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError as exc:
        return dict(confirmed=False, reason="host_smapi_log_unreadable", log=log, error=type(exc).__name__)
    want = (str(date["season"]).lower(), int(date["day"]), int(date["year"]))
    for i in range(len(lines) - 1, -1, -1):
        if BEFORE_SAVE in lines[i]:
            return dict(confirmed=False, reason="night_save_not_completed", log=log)  # a newer save is running
        m = AFTER_SAVE_RE.search(lines[i])
        if m:
            if (m.group(1).lower(), int(m.group(2)), int(m.group(3))) == want:
                return dict(confirmed=True, basis="host_smapi_after_save", log=log,
                            line=(i + 1) if start == 0 else None, text=lines[i].strip()[:200])
            break
    return dict(confirmed=False, reason="night_save_not_completed", log=log)


def checkpoint_save(source, destination, expected_date, night_save=None):
    """Read-only live save copy. Never lock for write, restore, or alter the source.

    Compare the complete file list/hashes before and after copying; a locked or
    changing save is skipped. An incomplete copy is retained with a failure receipt.
    ``night_save`` (night_save_evidence) is recorded: a copy without a confirmed completed
    night save is labelled copy_state "taken_while_running" (PLAN D6 needs the owner's OK).
    """
    confirmed = bool(night_save and night_save.get("confirmed"))
    marks = dict(night_save=night_save or dict(confirmed=False, reason="not_checked"),
                 copy_state="after_night_save" if confirmed else "taken_while_running")
    import xml.etree.ElementTree as ET
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise Refused("checkpoint destination overlaps the live save")
    if destination.exists():
        raise Refused("checkpoint already exists; never overwrite")
    def contents():
        files = sorted(source.rglob("*"))
        if any(p.is_symlink() for p in files):
            raise Refused("checkpoint refuses symlinks in save")
        return {str(p.relative_to(source)): _sha(ipc.read_bytes(p)) for p in files if p.is_file()}
    try:
        before = contents()
        main = source / source.name
        date = facts(ipc.read_bytes(main))["date"]
        if ({k: str(v).lower() for k, v in date.items()} != {k: str(v).lower() for k, v in expected_date.items()}
                or source.name not in before or "SaveGameInfo" not in before):
            raise Refused("checkpoint save date/files do not match the observed night save")
        info = ET.fromstring(ipc.read_bytes(source / "SaveGameInfo"))
        preview = (info.findtext("yearForSaveGame"), info.findtext("seasonForSaveGame"), info.findtext("dayOfMonthForSaveGame"))
        expected = (str(expected_date["year"]), str(SEASONS.index(str(expected_date["season"]).lower())), str(expected_date["day"]))
        if preview != expected:
            return dict(tag="LIVE_CHECKPOINT", status="skipped", reason="save_info_not_synced", **marks)
        destination.mkdir(parents=True)
        shutil.copytree(source, destination / "save")
        copied = {str(p.relative_to(destination / "save")): _sha(ipc.read_bytes(p))
                  for p in (destination / "save").rglob("*") if p.is_file()}
        if before != contents() or copied != before:
            raise OSError("save changed during copy")
        manifest = dict(tag="LIVE_CHECKPOINT", status="copied", source=str(source), save_name=source.name,
                        date=date, files=copied, utc=wf.utc(time.time()), **marks)
    except (OSError, ET.ParseError):
        manifest = dict(tag="LIVE_CHECKPOINT", status="skipped", reason="save_locked_or_changing", **marks)
    if destination.exists():
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def cmd_checkpoint(a, _ops):
    expected = dict(year=a.date[0], season=a.date[1], day=a.date[2])
    expected["year"], expected["day"] = int(expected["year"]), int(expected["day"])
    # D6: the manual copy records whether the game's own night save is confirmed (else taken_while_running).
    evidence = night_save_evidence(Path(a.host_log) if a.host_log else paths.HOST_SMAPI_LOG, expected)
    report = checkpoint_save(paths.SAVES / (a.save or paths.SAVE_NAME), snap_dir(a.name), expected, night_save=evidence)
    print(json.dumps(report))
    return 0 if report["status"] == "copied" else 3


def cmd_restore(a, ops):
    d = snap_dir(a.name)
    sn = a.save or paths.SAVE_NAME
    need_game_closed(ops)
    if not ops.exists(d / "manifest.json"):
        raise Refused(f"no snapshot {a.name} under {snap_root()}")
    manifest = verify_snapshot(ops, d, sn)                # read-only: every refusal happens before any write
    live = paths.SAVES / sn
    bk = do_backup(ops, a.save)                           # safety backup of the current save, ALWAYS before the replace
    need_game_closed(ops)
    extra = sorted({f.name for f in ops.files(live)} - set(manifest["files"]))
    written = []
    for fname in manifest["files"]:
        ops.write_atomic(live / fname, ops.read_bytes(d / "save" / fname))
        written.append(fname)
    for fname in extra:
        ops.unlink(live / fname)
    ok = _hash_folder(ops, live) == manifest["files"]
    report = dict(name=a.name, save=str(live), backup=bk["backup"], written_files=written, removed_extra=extra,
                  verified=ok, date=facts(ops.read_bytes(live / sn))["date"] if ok else None)
    ops.append_jsonl(paths.RUN / "staging-log.jsonl", dict(utc=wf.utc(ops.now()), tag="SAVESTATE", op="restore", **report))
    print(json.dumps(report, indent=1))
    if not ok:
        print(f"RESTORE VERIFY FAILED: restore the save from {bk['backup']}", file=sys.stderr)
        return 4
    return 0


def cmd_list_snapshots(a, ops):
    root = snap_root()
    rows = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        m = ops.read_json(d / "manifest.json")
        if isinstance(m, dict):
            rows.append((d.name, m.get("date") or {}, m.get("utc"), m.get("note") or ""))
    for name, date, utc, note in rows:
        print(f"{name}\t{date.get('season')} {date.get('day')}, year {date.get('year')}\t{utc}\t{note}")
    if not rows:
        print("no snapshots")
    return 0


def cmd_clear_bin_area(a, ops):
    # This operation never uses save_file(), do_backup(), game_running() or any live surface.
    p = Path(os.path.abspath(a.copy_save))
    prep, live = Path(os.path.abspath(paths.PREP)), Path(os.path.abspath(paths.SAVES))
    if not p.is_relative_to(prep) or p.is_relative_to(live):
        raise Refused("COPY_SAVE must be a copy under PREP, outside the live Saves directory")
    p, prep = p.resolve(), prep.resolve()
    if not p.is_relative_to(prep) or p.is_relative_to(live):
        raise Refused("COPY_SAVE resolves outside PREP or into the live Saves directory")
    if not ops.exists(p):
        raise Refused("COPY_SAVE does not exist")
    raw = ops.read_bytes(p)
    new, removed = clear_bin_area(raw, tuple(a.rect))
    report = dict(op="clear-bin-area", label="STAGING", copy_save=str(p), rect=a.rect, removed=removed,
                  before_sha256=_sha(raw), written=False)
    if a.dry_run or not removed:
        print(json.dumps(dict(report, dry_run=a.dry_run), indent=1))
        return 0
    backup = p.with_name(p.name + ".pre-clear-bin-" + wf.stamp(ops.now()) + ".bak")
    i = 1
    while ops.exists(backup):
        i += 1
        backup = backup.with_name(backup.stem + f"-{i}" + backup.suffix)
    ops.write_atomic(backup, raw)
    if ops.read_bytes(backup) != raw or ops.read_bytes(p) != raw:
        raise Refused("copy/backup changed before the edit: no edit")
    ops.write_atomic(p, new)
    after = ops.read_bytes(p)
    report.update(written=True, backup=str(backup), after_sha256=_sha(after), verified=after == new)
    receipt = backup.with_suffix(".receipt.json")
    report["receipt"] = str(receipt)
    ops.write_text(receipt, json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return 0 if report["verified"] else 4


def _load_catalog(a, ops):
    return ops.catalog(getattr(a, "catalog", None))


def _save_locations(a, ops):
    """Exact warp names from the save (read-only); None when the save cannot be read (warp lines are then refused)."""
    try:
        return save_location_names(ops.read_bytes(save_file(a.save)))
    except (OSError, Refused):
        return None


def cmd_locations(a, ops):
    rows = building_rows(ops.read_bytes(save_file(a.save)))
    for kind, unique, (x, y) in rows:
        print(f"{kind:<12} {unique}  ({x}, {y})")
    if not rows:
        print("no building interiors or cabins with a unique name in this save")
    return 0


def bench_script_identity(a, ops, *, loaded=False):
    expected = getattr(a, "bench_save_id", None)
    if not expected:
        if getattr(a, "bench_clock", False):
            raise Refused("bench clock requires the disposable bench save ID")
        return
    import xml.etree.ElementTree as ET  # local like every other XML reader here (benchrun2 NameError, ADR 242)
    from scripts.free_play.bench import protected, safe_save
    selected = protected(save_file(a.save))
    safe_save(expected, selected.name)
    world = ET.fromstring(ops.read_bytes(selected)).findtext("uniqueIDForThisGame")
    if str(safe_save(world, selected.name)) != expected:
        raise Refused("bench staged XML save identity mismatch")
    if loaded:
        from scripts.m1_operator import witness_run
        from scripts.m1_operator.live_save import scene_save_identity
        diagnostics = {}
        witness_run.PS.sample(diagnostics=diagnostics)
        identity = scene_save_identity(diagnostics.get("scene_identity"), witness_run.Live().session())
        if identity["save_id"] != expected:
            raise Refused("bench loaded save identity mismatch")


def cmd_check_script(a, ops):
    bench_script_identity(a, ops)
    text = ipc.read_text(Path(a.file), encoding="utf-8")
    blocks, errors = parse_script(text, _load_catalog(a, ops), a.target, a.allow_world_on_farmhand, a.owner_ok,
                                  a.confirm_world, _save_locations(a, ops),
                                  bench_clock=getattr(a, "bench_clock", False))
    for e in errors:
        print("INVALID " + e)
    for i, b in enumerate(blocks, 1):
        for r in b:
            print(f"block {i} line {r['n']}: {r['line']}" + (f"   [{r['note']}]" if r.get("note") else ""))
    print(("SCRIPT OK" if not errors else "SCRIPT REFUSED") + f" ({sum(len(b) for b in blocks)} lines, {len(blocks)} blocks)")
    return 0 if not errors else 2


def target_window(a, ops):
    if a.target == "host":
        if not a.owner_ok:
            raise Refused("--target host types into the owner's HOST window: pass --owner-ok only with the owner's OK "
                          "(PLAN §6.2: HOST input waits for the owner)")
        h = ops.read_text(paths.HOST_HWND_FILE)
        w = ops.window(h) if h else {"exists": False}
        title = (w.get("title") or "").casefold()
        farmhand = ops.read_json(paths.FARMHAND_LAUNCH) or {}
        fh_hwnd = ops.read_text(paths.FH_HWND_FILE)
        # HOST also runs SMAPI. Its native role marker, pinned HWND and a
        # distinct process identify it; the SMAPI substring identifies neither role.
        if (not w.get("exists") or not w.get("pid")
                or "stardew valley" not in title or "[main player]" not in title
                or "[farmhand]" in title or str(h) == str(fh_hwnd)
                or w.get("pid") == farmhand.get("pid")):
            raise Refused(f"HOST window {h} not found (or it is the SMAPI farmhand)")
        return int(h), w
    h = ops.read_text(paths.FH_HWND_FILE)
    launch = ops.read_json(paths.FARMHAND_LAUNCH) or {}
    w = ops.window(h) if h else {"exists": False}
    if not w.get("exists") or w.get("pid") != launch.get("pid") or "[farmhand]" not in (w.get("title") or ""):
        raise Refused(f"farmhand window {h} is not the operator's joined farmhand (pid {w.get('pid')} vs launch "
                      f"{launch.get('pid')}, title {w.get('title')!r})")
    return int(h), w


def cmd_run_script(a, ops):
    bench_script_identity(a, ops)
    text = ipc.read_text(Path(a.file), encoding="utf-8")
    blocks, errors = parse_script(text, _load_catalog(a, ops), a.target, a.allow_world_on_farmhand, a.owner_ok,
                                  a.confirm_world, _save_locations(a, ops),
                                  bench_clock=getattr(a, "bench_clock", False))
    if errors:
        for e in errors:
            print("INVALID " + e)
        raise Refused(f"{len(errors)} invalid line(s): nothing typed")
    running = ops.trial_running()
    if running is not False:
        raise Refused("a trial runner is running (or the process list is unknown): never stage during a trial")
    save_raw = ops.read_bytes(save_file(a.save))
    f = facts(save_raw)
    if f["allowChatCheats"] != "true":
        raise Refused("the save has allowChatCheats != true: run `stage_save.py edit --set allowChatCheats=true` with "
                      "the game closed first")
    hwnd, w = target_window(a, ops)
    if getattr(a, "bench_save_id", None):
        bench_script_identity(a, ops, loaded=True)
    if getattr(a, "bench_clock", False):
        owned = ops.read_json(paths.RUN / "batch-host/host-launch.json") or {}
        if a.target != "host" or not owned.get("pid") or owned["pid"] != w["pid"]:
            raise Refused("bench clock requires the exact harness-owned HOST process")
    host = ops.read_text(paths.HOST_HWND_FILE)
    lines = [r for b in blocks for r in b]
    plan = [f"shot ; ui_op key t {hwnd} ; shot (chat strip must change >= {a.chat_min_mad}) ; key backspace x2 ; "
            f"type {r['line']!r} --hwnd {hwnd} ; key enter" for r in lines]
    if a.dry_run:
        print(json.dumps(dict(dry_run=True, target=a.target, hwnd=hwnd, blocks=len(blocks), plan=plan), indent=1))
        return 0
    label = a.label or "stage"
    out = paths.RUN / f"staging-{label}-{wf.stamp(ops.now())}"
    n = ops.staging_count() + 1
    ops.mkdir(out)
    rec = dict(schema="arti_staging.v1", label=label, n=n, utc_start=wf.utc(ops.now()), target=a.target, hwnd=hwnd,
               title=w.get("title"), script=str(a.file), script_sha256=_sha(text.encode("utf-8")),
               save_sha256_before=_sha(save_raw), save_facts_before=f, lines=[], shots=[], note=(
                   "staged state is never evidence (PLAN §5.2); the next evidence attempt starts with a fresh label"))
    shot_k = [0]

    def shot(tag):
        shot_k[0] += 1
        dest = out / f"{shot_k[0]:02d}-{tag}.png"
        rc, so, se = ops.ui("shot", hwnd, dest)
        rec["shots"].append(dict(tag=tag, path=str(dest), rc=rc))
        try:
            client = json.loads((so or "").strip().splitlines()[-1]).get("client")
        except (ValueError, IndexError, AttributeError):
            client = None
        return (str(dest), client) if rc == 0 and client else (None, None)

    def chat_opened(n):
        """`key t`, then the chat strip must change versus the shot taken just before (else nothing is typed)."""
        pre, client = shot(f"line{n}-pre")
        rc, so, se = ops.ui("key", "t", hwnd)
        if rc != 0:
            return rc, None
        ops.sleep(0.35)
        post, _ = shot(f"line{n}-chat")
        score = ops.region_mad(pre, post, chat_region(client)) if pre and post else None
        return rc, score

    def log(row):
        ops.append_jsonl(paths.RUN / "staging-log.jsonl", dict(utc=wf.utc(ops.now()), tag="STAGING", label=label, **row))

    code = 0
    try:
        shot("before")
        for bi, block in enumerate(blocks, 1):
            for r in block:
                rc, score = chat_opened(r["n"])
                if rc != 0 or score is None or score < a.chat_min_mad:
                    row = dict(block=bi, n=r["n"], line=r["line"], ok=False, rcs=[rc], chat_mad=score)
                    rec["lines"].append(row)
                    log(row)
                    raise Refused(f"line {r['n']}: the chat box was not seen open after T (strip MAD {score} < "
                                  f"{a.chat_min_mad}, key rc {rc}); nothing typed, no Enter sent")
                steps = [("key", "backspace", hwnd), ("key", "backspace", hwnd),
                         ("type", r["line"], "--hwnd", hwnd), ("key", "enter", hwnd)]
                rcs = [0]
                for st in steps:
                    rc, so, se = ops.ui(*st)
                    rcs.append(rc)
                    if rc != 0:
                        break
                    ops.sleep(0.3 if st[0] == "key" else 0.2)
                ok = all(x == 0 for x in rcs) and len(rcs) == len(steps) + 1
                row = dict(block=bi, n=r["n"], line=r["line"], note=r.get("note"), ok=ok, rcs=rcs, chat_mad=score)
                rec["lines"].append(row)
                log(row)
                if not ok:
                    raise Refused(f"line {r['n']} aborted (ui_op rc {rcs[-1]}: the target lost the foreground?); "
                                  "nothing more typed")
                ops.sleep(a.line_delay)
            shot(f"block{bi}")
    except Refused as e:
        code = 3
        rec["aborted"] = str(e)
    finally:
        if a.target == "farmhand" and host:
            rc, so, se = ops.ui("fg", host)
            rec["fg_host"] = dict(rc=rc, ok='"ok": true' in (so or ""))
        rec.update(utc_end=wf.utc(ops.now()), exit_code=code)
        ops.write_text(out / "staging.json", json.dumps(rec, indent=1, ensure_ascii=False, default=str))
        ops.write_text(out / f"STAGING_{n}.md", receipt_md(rec))
        print(f"STAGING {'DONE' if code == 0 else 'ABORTED'} receipt {out / f'STAGING_{n}.md'}")
    return code


def chat_region(client):
    """Client-relative chat strip inside a PrintWindow PNG: x 0..896, the bottom 64 px (None without a client box)."""
    if not client:
        return None
    x0, y0, x1, y1 = client
    return [x0, max(y0, y1 - CHAT_STRIP[1]), x0 + min(CHAT_STRIP[0], x1 - x0), y1]


def receipt_md(r):
    lines = [f"# STAGING_{r['n']} ({r['label']}): {'done' if not r.get('aborted') else 'ABORTED'}", "",
             f"- {r['utc_start']} -> {r.get('utc_end')}; target {r['target']} hwnd {r['hwnd']} ({r.get('title')})",
             f"- script {r['script']} sha256 {r['script_sha256'][:16]}; save sha256 before {r['save_sha256_before'][:16]}",
             f"- save before: cheats {r['save_facts_before']['allowChatCheats']}, mine {r['save_facts_before']['mine_lowestLevelReached']}, "
             f"date {r['save_facts_before']['date']}, host money {r['save_facts_before']['host']['money']}",
             f"- {r['note']}", "", "| block | line | command | ok | note |", "|---|---|---|---|---|"]
    lines += [f"| {x['block']} | {x['n']} | `{x['line']}` | {x['ok']} | {x.get('note') or ''} |" for x in r["lines"]]
    lines += ["", "Screenshots: " + ", ".join(s["path"].replace("\\", "/").rsplit("/", 1)[-1] for s in r["shots"])]
    if r.get("aborted"):
        lines += ["", f"ABORTED: {r['aborted']}"]
    lines += ["", "After an in-game save: `stage_save.py diff` (element diff vs the newest backup) and `stage_save.py status`."]
    return "\n".join(lines) + "\n"


def build_parser():
    ap = argparse.ArgumentParser(prog="stage_save.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", help="save folder name (default m1_paths.SAVE_NAME / $ARTI_M1_SAVE_NAME)")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    sub.add_parser("status", help="read-only save facts").set_defaults(f=cmd_status)
    sub.add_parser("backup", help="copy the save folder to PREP (game closed)").set_defaults(f=cmd_backup)
    e = sub.add_parser("edit", help="BOM-preserving element edits (game closed; backup first)")
    e.add_argument("--set", action="append", required=True, metavar="KEY=VALUE")
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(f=cmd_edit)
    d = sub.add_parser("diff", help="element diff vs a backup")
    d.add_argument("backup", nargs="?")
    d.add_argument("--max", type=int, default=50)
    d.set_defaults(f=cmd_diff)
    sn = sub.add_parser("snapshot", help="copy the save folder to PREP/savestates/NAME (game closed)")
    sn.add_argument("name")
    sn.add_argument("--note")
    sn.add_argument("--overwrite", action="store_true")
    sn.set_defaults(f=cmd_snapshot)
    cp = sub.add_parser("checkpoint", help="read-only LIVE_CHECKPOINT after a verified night save; skip locked files")
    cp.add_argument("name")
    cp.add_argument("--date", nargs=3, required=True, metavar=("YEAR", "SEASON", "DAY"))
    cp.add_argument("--host-log", help="HOST SMAPI log for the night-save check (default m1_paths.HOST_SMAPI_LOG)")
    cp.set_defaults(f=cmd_checkpoint)
    rs = sub.add_parser("restore", help="replace the save with a snapshot (game closed; safety backup first)")
    rs.add_argument("name")
    rs.set_defaults(f=cmd_restore)
    sub.add_parser("list-snapshots", help="list savestates").set_defaults(f=cmd_list_snapshots)
    sub.add_parser("locations", help="building interiors and cabins: exact unique names for /debug warp").set_defaults(
        f=cmd_locations)
    b = sub.add_parser("clear-bin-area", help="clear bounded trees/litter in a save COPY under PREP only")
    b.add_argument("copy_save", metavar="COPY_SAVE")
    b.add_argument("--rect", nargs=4, type=int, default=[69, 12, 74, 16], metavar=("X0", "Y0", "X1", "Y1"))
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(f=cmd_clear_bin_area)
    for name, fn in (("check-script", cmd_check_script), ("run-script", cmd_run_script)):
        s = sub.add_parser(name, help="validate a /debug script" if name == "check-script" else "type a /debug script")
        s.add_argument("file")
        s.add_argument("--target", choices=("farmhand", "host"), default="farmhand")
        s.add_argument("--allow-world-on-farmhand", action="store_true")
        s.add_argument("--catalog", help="JSON {qualified id: name} instead of reading the installed XNBs")
        s.add_argument("--owner-ok", action="store_true",
                       help="the owner allowed this (HOST input; completecc / allbundles with --confirm-world)")
        s.add_argument("--confirm-world", action="store_true", help="explicit confirm for completecc / allbundles")
        s.add_argument("--bench-clock", action="store_true", help="checked time command on a disposable harness HOST")
        s.add_argument("--bench-save-id", help="bind a bench script to the disposable native save ID")
        if name == "run-script":
            s.add_argument("--chat-min-mad", type=float, default=0.03,
                           help="minimum change of the chat strip after T (HYPOTHESIS; scores are in the receipt)")
            s.add_argument("--dry-run", action="store_true")
            s.add_argument("--label")
            s.add_argument("--line-delay", type=float, default=0.8)
        s.set_defaults(f=fn)
    return ap


def main(argv=None, ops=None):
    a = build_parser().parse_args(argv)
    try:
        return a.f(a, ops or LiveOps())
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
