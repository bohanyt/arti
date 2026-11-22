"""Museum/geode witness STAGING on a template COPY: native item IDs, byte-preserving, reversible.

Owner 2026-11-01: if ARTI owns no artifacts, give the test some. This only adds items
and inventory room to the one established farmhand; it never steers what she does
with them. Every mode refuses the canonical template folder, run records and Saves.

    python -B -m scripts.free_play.stage_geode_witness plan --copy DIR --objects-data JSON
    python -B -m scripts.free_play.stage_geode_witness apply --copy DIR --objects-data JSON --backup DIR --record JSON [--dry-run]
    python -B -m scripts.free_play.stage_geode_witness verify --copy DIR --record JSON
    python -B -m scripts.free_play.stage_geode_witness rollback --copy DIR --record JSON

`--objects-data` is a native Data/Objects export: {"535": {"Name", "Type", "Category",
"Price", "Edibility"}, ...}. Item fields are never guessed from memory.

Deposit witness (WITDEPOSIT, ADR 106): `--bundle "Crafts Room/13"` also stages every ingredient of
that one bundle, read from the save's own `bundleData` row (what the game uses) or, when the save
has none, from `--bundles-data` (a native Data/Bundles export {"Crafts Room/13": "Sample Bundle/O 100 1/101 1 0 102 1 0/0///Sample Bundle", ...}). Ingredient ids and
quantities come only from those rows, never from a table in this file. The bundle's own
completion flags and the room's state are reported as facts; nothing about her choice is staged.

Isolated witness (FIX75D, ADR 132): `--stand-data` writes the farmhand's native rejoin fields so she joins
standing beside the action, and `--min-free N` keeps the bag at its size by swapping the last plain stacks
for the staged items. `--add none` stages no extra item. `scripts/free_play/witness_isolated.py` builds these.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from xml.parsers import expat

FARMHAND = "/SaveGame/farmhands/Farmer"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"
NIL = b'<Item xsi:nil="true" />'
DEFAULT_ADD = "535:2,101:1,72:1"  # Geode; Arrowhead (Arch); Diamond (Minerals)
DEPOSIT_ADD = "535:1,101:1"  # with --bundle: one Geode (Clint) and one Arrowhead (Arch, museum-eligible)
CC_AREAS = ("Pantry", "Crafts Room", "Fish Tank", "Boiler Room", "Vault", "Bulletin")  # areasComplete order
CC_TYPE = "CommunityCenter"
DEFAULT_BACKPACK = 24  # native Large Pack size
MAX_STACK = 999
GEODE_COST = 25  # GeodeMenu charge per crack
REFUSED_PARENTS = {"free-play-templates", "Saves"}
REFUSED_ANCESTORS = {"free-play-runs"}
# Field order of an observed 1.6 serializer Object (template farmhand Geode item).
OBJECT = ("<Item xsi:type=\"Object\"><isLostItem>false</isLostItem><category>{category}</category>"
          "<hasBeenInInventory>true</hasBeenInInventory><name>{name}</name><parentSheetIndex>{id}</parentSheetIndex>"
          "<itemId>{id}</itemId><specialItem>false</specialItem><isRecipe>false</isRecipe><quality>0</quality>"
          "<stack>{stack}</stack><SpecialVariable>0</SpecialVariable><tileLocation><X>0</X><Y>0</Y></tileLocation>"
          "<owner>0</owner><type>{type}</type><canBeSetDown>true</canBeSetDown><canBeGrabbed>true</canBeGrabbed>"
          "<isSpawnedObject>false</isSpawnedObject><questItem>false</questItem><isOn>true</isOn><fragility>0</fragility>"
          "<price>{price}</price><edibility>{edibility}</edibility><bigCraftable>false</bigCraftable>"
          "<setOutdoors>false</setOutdoors><setIndoors>false</setIndoors><readyForHarvest>false</readyForHarvest>"
          "<showNextIndex>false</showNextIndex><flipped>false</flipped><isLamp>false</isLamp>"
          "<minutesUntilReady>0</minutesUntilReady><boundingBox><X>0</X><Y>0</Y><Width>0</Width><Height>0</Height>"
          "<Location><X>0</X><Y>0</Y></Location><Size><X>0</X><Y>0</Y></Size></boundingBox><scale><X>0</X><Y>0</Y></scale>"
          "<uses>0</uses><destroyOvernight>false</destroyOvernight></Item>")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def manifest(folder):
    folder = Path(folder)
    return {p.relative_to(folder).as_posix(): sha(p.read_bytes()) for p in sorted(folder.rglob("*")) if p.is_file()}


def parse_add(text):
    if text.strip() == "none":
        return []
    out = []
    for part in text.split(","):
        item, qty = part.strip().split(":")
        item = item.removeprefix("(O)")
        if not item or int(qty) <= 0:
            raise ValueError("add entries are ID:positive-quantity")
        out.append((item, int(qty)))
    if len({i for i, _ in out}) != len(out):
        raise ValueError("duplicate item id in --add")
    return out


def native_object(objects, item):
    row = objects.get(item) or objects.get("(O)" + item)
    if not isinstance(row, dict):
        raise ValueError(f"(O){item} absent from the native objects export")
    fields = {}
    for key, kind in (("Name", str), ("Type", str), ("Category", int), ("Price", int), ("Edibility", int)):
        if type(row.get(key)) is not kind:
            raise ValueError(f"(O){item} native {key} missing")
        fields[key] = row[key]
    if any(c in fields["Name"] + fields["Type"] for c in "<>&"):
        raise ValueError(f"(O){item} name/type needs XML escaping; refuse")
    return fields


def object_xml(item, stack, fields):
    return OBJECT.format(id=item, stack=stack, name=fields["Name"], type=fields["Type"], category=fields["Category"],
                         price=fields["Price"], edibility=fields["Edibility"]).encode("utf-8")


def spans(data, prefix):
    """Byte spans of elements under `prefix`: path -> list of (begin, open_end, close_begin, end, attrs)."""
    parser, stack, open_, out = expat.ParserCreate(), [], [], {}

    def start(name, attrs):
        stack.append(name)
        begin = parser.CurrentByteIndex
        open_end = data.index(b">", begin) + 1
        open_.append((begin, open_end, data[open_end - 2:open_end] == b"/>", attrs))

    def end(name):
        path = "/" + "/".join(stack)
        begin, open_end, empty, attrs = open_.pop()
        if empty:
            close_begin = end_ = open_end
        else:
            close_begin = parser.CurrentByteIndex
            end_ = data.index(b">", close_begin) + 1
        if path == prefix or path.startswith(prefix + "/"):
            out.setdefault(path, []).append((begin, open_end, close_begin, end_, attrs))
        stack.pop()
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(data, True)
    return out


def farmhand(root):
    peers = root.findall("./farmhands/Farmer")
    nested = [e for e in root.findall(".//farmhand") if e.find("UniqueMultiplayerID") is not None]
    if len(peers) != 1 or nested:
        raise ValueError("exactly one established farmhand under /SaveGame/farmhands required")
    return peers[0]


def bundle_row(key, value):
    """Parse one Data/Bundles row the way the bridge does (ProgressRules.ParseBundle): key "Room/index",
    value "name/reward/triplets/color/slots/texture/label". Returns None for a malformed row."""
    k = (key or "").split("/")
    f = (value or "").split("/")
    if len(k) != 2 or not k[1].isdigit() or len(f) < 3:
        return None
    t = f[2].split()
    if not t or len(t) % 3:
        return None
    needs = []
    for i in range(0, len(t), 3):
        try:
            qty, quality = int(t[i + 1]), int(t[i + 2])
        except ValueError:
            return None
        needs.append(dict(item=t[i], quantity=qty, quality=quality))
    slots = int(f[4]) if len(f) > 4 and f[4].isdigit() and int(f[4]) > 0 else len(needs)
    label = f[6] if len(f) > 6 and f[6].strip() else f[0]
    return dict(key=key, room=k[0], index=int(k[1]), name=label, reward=f[1], needs=needs, slots=min(slots, len(needs)))


def bundle_facts(root, key, bundles_export=None):
    """Read the one bundle's definition and state from the parsed save: the save's `bundleData` row wins
    (it is what the game loads), else the native export. Completion flags and the room state are facts."""
    saved = {i.findtext("key/string"): i.findtext("value/string") or "" for i in root.findall("bundleData/item")}
    if key in saved:
        row, source = bundle_row(key, saved[key]), "save bundleData"
    elif bundles_export and key in bundles_export:
        row, source = bundle_row(key, bundles_export[key]), "bundles export"
    else:
        raise ValueError(f"bundle {key!r} is in neither the save's bundleData nor --bundles-data")
    if row is None:
        raise ValueError(f"bundle {key!r} row is malformed")
    cc = next((loc for loc in root.findall("locations/GameLocation") if loc.get(XSI + "type") == CC_TYPE), None)
    flags = None
    rooms = None
    if cc is not None:
        for item in cc.findall("bundles/item"):
            if item.findtext("key/int") == str(row["index"]):
                flags = [b.text == "true" for b in item.find("value/ArrayOfBoolean")]
        if cc.find("areasComplete") is not None:
            rooms = [c.text == "true" for c in cc.find("areasComplete")]
    area = CC_AREAS.index(row["room"]) if row["room"] in CC_AREAS else None
    room_complete = rooms[area] if rooms is not None and area is not None and area < len(rooms) else None
    return dict(row, source=source, slots_filled=flags, room_complete=room_complete,
                already_complete=None if flags is None else sum(flags) >= row["slots"])


def bundle_add(facts):
    """(item id, quantity) for every object ingredient of the bundle; refuses what this script cannot stage."""
    out = []
    for need in facts["needs"]:
        item = need["item"]
        if item.startswith("-"):
            raise ValueError(f"bundle {facts['key']!r} wants a category/money ingredient {item}; choose one with --add")
        if need["quality"] != 0:
            raise ValueError(f"bundle {facts['key']!r} wants quality {need['quality']} for (O){item}; this script stages quality 0 only")
        out.append((item.removeprefix("(O)"), need["quantity"]))
    return out


def merge_add(first, second):
    seen = {}
    for item, qty in [*first, *second]:
        seen[item] = seen.get(item, 0) + qty
    return list(seen.items())


def seen_event_edit(data, all_spans, event_ids):
    """ADR 135: the game's own record that this farmhand saw these events, in the serializer's form (`<eventsSeen><int>60367</int>`
    on the template's host row). Native first-entry scenes only; the ids come from a native Data/Events read, never from here."""
    if any(not str(e).isdigit() for e in event_ids):
        raise ValueError("a staged seen event must be a native integer id")
    found = all_spans.get(FARMHAND + "/eventsSeen", [])
    if len(found) != 1:
        raise ValueError("farmhand eventsSeen absent or ambiguous")
    held = {data[s[1]:s[2]].decode("utf-8") for s in all_spans.get(FARMHAND + "/eventsSeen/int", [])}
    rows = "".join(f"<int>{e}</int>" for e in event_ids if str(e) not in held)
    begin, open_end, close_begin, end, _ = found[0]
    if data[open_end - 2:open_end] == b"/>":
        return dict(kind="replace_element", at=begin, end=end, old=data[begin:end].decode("utf-8"), new=f"<eventsSeen>{rows}</eventsSeen>")
    return dict(kind="insert", at=close_begin, end=close_begin, old="", new=rows)


def plan(data, objects, add, backpack=DEFAULT_BACKPACK, farmhand_name=None, bundle=None, bundles_export=None,
         position=None, min_free=None, seen_events=None):
    """Return (staged bytes, edits, facts). Pure: no file IO. With `bundle` ("Room/index"), the bundle's
    ingredients are added ahead of `add` and the bundle's native state rides the facts. `position` and
    `min_free` are the isolated witness's stand and same-size bag (ADR 132); `seen_events` the first-entry
    scenes at that stand (ADR 135)."""
    before = ET.fromstring(data)
    bundle_info = None
    if bundle is not None:
        bundle_info = bundle_facts(before, bundle, bundles_export)
        add = merge_add(bundle_add(bundle_info), add)
    peer = farmhand(before)
    if farmhand_name is not None and peer.findtext("name") != farmhand_name:
        raise ValueError("farmhand name differs from --farmhand-name")
    items_el = peer.find("items")
    slots = list(items_el)
    old_max = int(peer.findtext("maxItems"))
    if len(slots) != old_max or backpack < old_max:
        raise ValueError("inventory length must equal maxItems and backpack may only grow")
    all_spans = spans(data, FARMHAND)
    if len(all_spans.get(FARMHAND, [])) != 1:
        raise ValueError("farmhand element ambiguous")
    item_spans = all_spans.get(FARMHAND + "/items/Item", [])
    if len(item_spans) != len(slots):
        raise ValueError("inventory slots and byte spans disagree")
    # Map each slot's own itemId/stack/quality spans (children lie within the slot span).
    def child(path, begin, end):
        found = [s for s in all_spans.get(FARMHAND + "/items/Item/" + path, []) if begin < s[0] and s[3] <= end]
        return found[0] if len(found) == 1 else None
    edits, facts, appended = [], dict(added=[], merged=[]), []
    for item, qty in add:
        fields = native_object(objects, item)
        merged = False
        for slot, span in zip(slots, item_spans):
            if (slot.get(XSI + "type") == "Object" and slot.findtext("itemId") == item
                    and slot.findtext("quality") == "0"):
                stack_span = child("stack", span[0], span[3])
                old = int(slot.findtext("stack"))
                if stack_span is None or old + qty > MAX_STACK:
                    raise ValueError(f"(O){item} existing stack cannot take {qty}")
                edits.append(dict(kind="replace_text", at=stack_span[1], end=stack_span[2],
                                  old=str(old), new=str(old + qty), item="(O)" + item))
                facts["merged"].append(dict(item="(O)" + item, name=fields["Name"], before=old, after=old + qty))
                merged = True
                break
        if not merged:
            appended.append(object_xml(item, qty, fields))
            facts["added"].append(dict(item="(O)" + item, name=fields["Name"], type=fields["Type"], quantity=qty,
                                       price=fields["Price"]))
    room = backpack - old_max
    if min_free is not None:
        if room:
            raise ValueError("--min-free keeps the bag's size; it does not combine with a bigger backpack")
        slot_edits, facts["filled_slots"], facts["dropped"] = swap_slots(data, slots, item_spans, appended, min_free,
                                                                          {item for item, _ in add})
        edits.extend(slot_edits)
        facts["swapped_slots"] = [e["slot"] for e in slot_edits]
    elif len(appended) > room:
        raise ValueError(f"{len(appended)} new stacks need room; backpack {backpack} adds only {room} slots")
    if room:
        max_span = all_spans[FARMHAND + "/maxItems"]
        if len(max_span) != 1:
            raise ValueError("farmhand maxItems ambiguous")
        edits.append(dict(kind="replace_text", at=max_span[0][1], end=max_span[0][2], old=str(old_max), new=str(backpack)))
        tail = b"".join(appended) + NIL * (room - len(appended))
        items_span = all_spans[FARMHAND + "/items"][0]
        edits.append(dict(kind="insert", at=items_span[2], end=items_span[2], old="", new=tail.decode("utf-8")))
    if position is not None:
        position_edits, facts["position"] = stand_edits(data, before, all_spans, position)
        edits.extend(position_edits)
    if seen_events:
        edits.append(seen_event_edit(data, all_spans, seen_events))
        facts["events_marked_seen"] = [str(e) for e in seen_events]
    result = data
    for edit in sorted(edits, key=lambda e: e["at"], reverse=True):
        if result[edit["at"]:edit["end"]].decode("utf-8") != edit["old"]:
            raise AssertionError("edit target bytes changed")
        result = result[:edit["at"]] + edit["new"].encode("utf-8") + result[edit["end"]:]
    after = ET.fromstring(result)
    revert(after, old_max, facts, before)
    if ET.tostring(after) != ET.tostring(before):
        raise AssertionError("non-whitelisted XML change")
    staged = farmhand(ET.fromstring(result))
    free = sum(1 for s in staged.find("items") if s.get(XSI + "nil") == "true")
    money = int(staged.findtext("money"))
    geodes = sum(int(s.findtext("stack")) for s in staged.find("items")
                 if s.get(XSI + "type") == "Object" and s.findtext("itemId") == "535")
    facts.update(farmhand=staged.findtext("name"), max_items=[old_max, backpack], free_slots_after=free,
                 money=money, geode_535_after=geodes, geode_cracks_affordable=money // GEODE_COST,
                 museum_pieces=len(list(before.iterfind(".//museumPieces/item"))),
                 geode_menu_room=free >= 2)  # GeodeMenu refuses a crack without a spare slot
    if bundle_info is not None:
        held = {}
        for s in staged.find("items"):
            if s.get(XSI + "type") == "Object" and s.findtext("quality") == "0":
                held[s.findtext("itemId")] = held.get(s.findtext("itemId"), 0) + int(s.findtext("stack"))
        fit = [dict(item="(O)" + n["item"], name=objects.get(n["item"], {}).get("Name"), need=n["quantity"],
                    held_after=held.get(n["item"], 0)) for n in bundle_info["needs"]]
        facts["bundle"] = dict(bundle_info, held_after=fit,
                               every_ingredient_held=all(f["held_after"] >= f["need"] for f in fit))
    return result, [dict(e, at=e["at"]) for e in sorted(edits, key=lambda e: e["at"])], facts


def days_played(root):
    """The master's own day count, which BedFurniture.ApplyWakeUpPosition compares with disconnectDay."""
    stats = root.find("player/stats")
    if stats is not None:
        for row in stats.findall("Values/item"):
            if row.findtext("key/string") == "daysPlayed":
                return int(row.findtext("value/unsignedInt"))
        if (stats.findtext("daysPlayed") or "").strip().isdigit():
            return int(stats.findtext("daysPlayed"))
    raise ValueError("the save's own daysPlayed cannot be read")


def text_edit(data, found, tag, new):
    """A whitelisted edit of one farmhand element's text; a self-closed element becomes a text element."""
    if len(found) != 1:
        raise ValueError(f"farmhand {tag} absent or ambiguous")
    begin, open_end, close_begin, end, _ = found[0]
    if data[open_end - 2:open_end] == b"/>":
        return dict(kind="replace_element", at=begin, end=end, old=data[begin:end].decode("utf-8"),
                    new=f"<{tag}>{new}</{tag}>")
    return dict(kind="replace_text", at=open_end, end=close_begin, old=data[open_end:close_begin].decode("utf-8"), new=str(new))


def stand_edits(data, root, all_spans, position):
    """ADR 132: the native rejoin fields place a farmhand (GameServer.checkFarmhandRequest sends the location,
    Client.setUpGame -> ApplyWakeUpPosition sets it). The location must exist in the save; nothing else changes."""
    location, x, y, facing = position["location"], position["x"], position["y"], position["facing"]
    if location not in {e.findtext("name") for e in root.findall("locations/GameLocation")}:
        raise ValueError(f"location {location!r} is not in the save")
    if any(type(v) is not int or v < 0 for v in (x, y)) or facing not in (0, 1, 2, 3):
        raise ValueError("stand needs tile x,y and a native facing 0..3")
    day = days_played(root)
    edits = [text_edit(data, all_spans.get(FARMHAND + "/disconnectDay", []), "disconnectDay", day),
             text_edit(data, all_spans.get(FARMHAND + "/disconnectLocation", []), "disconnectLocation", location),
             text_edit(data, all_spans.get(FARMHAND + "/disconnectPosition/X", []), "X", x * 64),
             text_edit(data, all_spans.get(FARMHAND + "/disconnectPosition/Y", []), "Y", y * 64),
             text_edit(data, all_spans.get(FARMHAND + "/FacingDirection", []), "FacingDirection", facing)]
    return edits, dict(position, tile=[x, y], pixels=[x * 64, y * 64], disconnect_day=day)


def swap_slots(data, slots, item_spans, appended, min_free, keep):
    """Keep the bag's size: the last plain stacks she does not need make room, then staged stacks fill free slots.
    Tools, weapons and quest items are never touched. Returns (edits, filled slots, dropped facts)."""
    free = [i for i, s in enumerate(slots) if s.get(XSI + "nil") == "true"]
    need = len(appended) + min_free - len(free)
    plain = [i for i in range(len(slots) - 1, -1, -1)
             if slots[i].get(XSI + "type") == "Object" and slots[i].findtext("itemId") not in keep
             and slots[i].findtext("questItem") != "true"]
    if need > len(plain):
        raise ValueError(f"{need} slots needed, only {len(plain)} plain stacks can make room")
    dropped = plain[:max(need, 0)]
    placed = dict(zip(sorted(free + dropped), appended))
    edits = []
    for index in sorted({*dropped, *placed}):
        begin, _, _, end, _ = item_spans[index]
        edits.append(dict(kind="replace_slot", at=begin, end=end, old=data[begin:end].decode("utf-8"),
                          new=placed.get(index, NIL).decode("utf-8"), slot=index))
    gone = [dict(slot=i, item="(O)" + slots[i].findtext("itemId"), name=slots[i].findtext("name"),
                 stack=int(slots[i].findtext("stack"))) for i in dropped]
    return edits, sorted(placed), gone


def revert(root, old_max, facts, before=None):
    """Undo the whitelisted changes on a parsed staged tree (for the equality proof)."""
    peer = farmhand(root)
    peer.find("maxItems").text = str(old_max)
    items = peer.find("items")
    if before is not None:
        old = farmhand(before)
        for index in facts.get("swapped_slots", ()):
            items.remove(items[index])
            items.insert(index, deepcopy(old.find("items")[index]))
        if facts.get("position"):
            for tag in ("disconnectDay", "disconnectLocation", "disconnectPosition", "FacingDirection"):
                at = list(peer).index(peer.find(tag))
                peer.remove(peer[at])
                peer.insert(at, deepcopy(old.find(tag)))
        if facts.get("events_marked_seen"):
            at = list(peer).index(peer.find("eventsSeen"))
            peer.remove(peer[at])
            peer.insert(at, deepcopy(old.find("eventsSeen")))
    for extra in list(items)[old_max:]:
        items.remove(extra)
    for m in facts["merged"]:
        slot = next(s for s in items if s.get(XSI + "type") == "Object" and "(O)" + s.findtext("itemId") == m["item"])
        slot.find("stack").text = str(m["before"])


def refuse_path(copy):
    copy = Path(copy).resolve()
    if copy.parent.name in REFUSED_PARENTS or REFUSED_ANCESTORS & {p.name for p in copy.parents}:
        raise ValueError(f"refuse {copy}: canonical template, live Saves or a run record; stage a COPY")
    save = copy / copy.name
    if not save.is_file():
        raise ValueError("copy folder must contain its save file named like the folder")
    return copy, save


def game_running():
    if os.name != "nt":
        return False
    tasks = subprocess.check_output(["tasklist"], text=True).lower()
    return "stardewmoddingapi" in tasks or "stardew valley.exe" in tasks


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def verify(copy, record_path):
    copy, save = refuse_path(copy)
    record = json.loads(Path(record_path).read_text(encoding="utf-8"))
    backup = Path(record["backup"])
    if manifest(backup) != record["before"]:
        raise AssertionError("backup hash mismatch")
    if manifest(copy) != record["after"]:
        raise AssertionError("staged copy drifted")
    objects = json.loads(Path(record["objects_data"]).read_text(encoding="utf-8"))
    if sha(Path(record["objects_data"]).read_bytes()) != record["objects_data_sha256"]:
        raise AssertionError("objects export changed")
    bundles_export = None
    if record.get("bundles_data"):
        if sha(Path(record["bundles_data"]).read_bytes()) != record["bundles_data_sha256"]:
            raise AssertionError("bundles export changed")
        bundles_export = json.loads(Path(record["bundles_data"]).read_text(encoding="utf-8"))
    expected, _, facts = plan((backup / copy.name).read_bytes(), objects, [tuple(x) for x in record["add"]],
                              record["backpack"], record.get("farmhand_name"), record.get("bundle"), bundles_export,
                              record.get("position"), record.get("min_free"), record.get("seen_events"))
    if save.read_bytes() != expected:
        raise AssertionError("save is not exactly the planned edits")
    return facts


def main(argv=None, running=game_running):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("plan", "apply", "verify", "rollback"))
    ap.add_argument("--copy", required=True, help="template COPY folder (named like its save)")
    ap.add_argument("--objects-data")
    ap.add_argument("--add", help=f"ID:qty,... (default {DEFAULT_ADD}; with --bundle {DEPOSIT_ADD})")
    ap.add_argument("--bundle", help='one bundle key, e.g. "Crafts Room/13"; stages all its ingredients too')
    ap.add_argument("--bundles-data", help="native Data/Bundles export, used only when the save has no bundleData")
    ap.add_argument("--backpack", type=int, default=DEFAULT_BACKPACK)
    ap.add_argument("--farmhand-name")
    ap.add_argument("--stand-data", help="JSON {location, x, y, facing, source}: her native rejoin position (ADR 132)")
    ap.add_argument("--min-free", type=int, help="keep the bag's size and leave this many free slots (ADR 132)")
    ap.add_argument("--seen-events", help="comma-separated native first-entry event ids this farmhand has seen (ADR 135)")
    ap.add_argument("--expect-sha", help="pinned save SHA-256 before staging")
    ap.add_argument("--backup")
    ap.add_argument("--record")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.mode in ("verify", "rollback"):
        if not a.record:
            ap.error("--record required")
        facts = verify(a.copy, a.record)
        if a.mode == "rollback":
            if running():
                raise RuntimeError("GAME_RUNNING")
            record = json.loads(Path(a.record).read_text(encoding="utf-8"))
            copy, save = refuse_path(a.copy)
            shutil.copy2(Path(record["backup"]) / copy.name, save)
            if manifest(copy) != record["before"]:
                raise AssertionError("rollback hashes differ")
            facts = dict(restored=True, hashes=manifest(copy))
        print(json.dumps(facts, indent=2))
        return 0
    if not a.objects_data:
        ap.error("--objects-data (native Data/Objects export) required")
    copy, save = refuse_path(a.copy)
    objects_bytes = Path(a.objects_data).read_bytes()
    data = save.read_bytes()
    if a.expect_sha and sha(data) != a.expect_sha:
        raise ValueError("save SHA differs from --expect-sha pin")
    add = parse_add(a.add or (DEPOSIT_ADD if a.bundle else DEFAULT_ADD))
    bundles_bytes = Path(a.bundles_data).read_bytes() if a.bundles_data else None
    position = json.loads(Path(a.stand_data).read_text(encoding="utf-8")) if a.stand_data else None
    seen_events = [e.strip() for e in a.seen_events.split(",")] if a.seen_events else None
    result, edits, facts = plan(data, json.loads(objects_bytes), add, a.backpack, a.farmhand_name, a.bundle,
                                json.loads(bundles_bytes) if bundles_bytes else None, position, a.min_free, seen_events)
    summary = dict(save_sha256_before=sha(data), save_sha256_after=sha(result), edit_count=len(edits), facts=facts)
    if a.mode == "plan" or a.dry_run:
        print(json.dumps(dict(summary, dry_run=True, edit_detail=[
            {k: (v if k != "new" or len(v) < 80 else v[:80] + "...") for k, v in e.items()} for e in edits]), indent=2))
        return 0
    if not a.backup or not a.record:
        ap.error("apply needs --backup and --record")
    if running():
        raise RuntimeError("GAME_RUNNING")
    backup, record_path = Path(a.backup).resolve(), Path(a.record).resolve()
    if backup.exists() or record_path.exists():
        raise ValueError("backup/record exists; refuse overwrite")
    before = manifest(copy)
    shutil.copytree(copy, backup)
    if manifest(backup) != before:
        raise AssertionError("backup copy mismatch")
    temp = save.with_name(save.name + ".staging-tmp")
    temp.write_bytes(result)
    if temp.read_bytes() != result:
        raise AssertionError("temp write mismatch")
    os.replace(temp, save)
    after = manifest(copy)
    if [k for k in before if before[k] != after[k]] != [copy.name] or before.keys() != after.keys():
        raise AssertionError("files other than the save changed")
    write_json(record_path, dict(utc=datetime.now(timezone.utc).isoformat(), tag="STAGING", copy=str(copy),
               backup=str(backup), objects_data=str(Path(a.objects_data).resolve()),
               objects_data_sha256=sha(objects_bytes), add=add, backpack=a.backpack, farmhand_name=a.farmhand_name,
               bundle=a.bundle, bundles_data=str(Path(a.bundles_data).resolve()) if a.bundles_data else None,
               bundles_data_sha256=sha(bundles_bytes) if bundles_bytes else None, position=position, min_free=a.min_free,
               seen_events=seen_events,
               before=before, after=after, edits=edits, **summary))
    print(json.dumps(dict(summary, verified=verify(copy, record_path)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
