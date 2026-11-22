"""Names on factual rows. Native identities remain in receipts and action arguments.

Never manufacture a display name by stripping a qualified ID or splitting an
internal name. Missing source names are explicitly unknown.
"""
from collections.abc import Mapping
import re

UNKNOWN_ITEM = "barang (nama belum diketahui)"
QUALIFIED = re.compile(r"\([A-Za-z][A-Za-z0-9]*\)[^\s,;\"'<>\[\]{}()]+")
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{6}~[0-9a-f]+|\b[0-9a-f]{32,64}\b", re.I)
REF = re.compile(r"\b(?:obj|tf|bed|warp|bdoor|loc|building|clump|xt):\S+")
UNKNOWN_TARGET = "sasaran (nama belum diketahui)"


def display_name(value):
    if (isinstance(value, str) and value.strip() and not QUALIFIED.search(value)
            and not GUID.search(value) and not REF.search(value) and not value.startswith("[")):
        return value.strip()
    return None


def item_name(item, names=None, name=None):
    return (display_name(name) or display_name((names or {}).get(item))
            or next((n for n in (names or {}).values() if isinstance(item, str) and isinstance(n, str)
                     and n.casefold() == item.casefold()), None) or UNKNOWN_ITEM)


UNNAMED_CONTROL = "kontrol (nama belum diketahui)"  # the mod's row for a control with no text of its own (ModEntry.GenericUi.cs:104)
_FIELD = re.compile(r"(?:.*\.)?([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")
_FIELD_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
# A menu's list of all its controls says nothing about one of them: such a row stays explicitly unknown.
_NAMELESS_FIELDS = frozenset({"allClickableComponents"})


def field_name(path):
    """ADR 135: the native field a control lives in is a native name. `menu.geodeSpot` reads "geode spot",
    `menu.ingredientSlots[2]` "ingredient slots 2" (the native index), `menu.responseCC[0]` "response CC 0"."""
    match = _FIELD.fullmatch(path) if isinstance(path, str) else None
    if match is None or match[1] in _NAMELESS_FIELDS:
        return None
    words = [w if w.isupper() else w.lower() for w in _FIELD_WORD.findall(match[1])]
    return " ".join(words + ([match[2]] if match[2] is not None else [])) or None


def control_label(label, field_path):
    """The row's own text wins; only the mod's explicit unknown takes the field name (a native description stays beside it)."""
    if isinstance(label, str) and label.startswith(UNNAMED_CONTROL) and (name := field_name(field_path)):
        return name + label[len(UNNAMED_CONTROL):]
    return label


def ref_name(ref, names=None):
    """FIX77B (a56: `aksi sasaran (nama belum diketahui)` rode 23 reply turns with (BC)130 "Chest" in the same row's names): a target
    reads as the name the same native read gave it, by its whole ref or by the item ID the ref carries. No name, still unknown."""
    names = names or {}
    item = QUALIFIED.search(ref)
    return display_name(names.get(ref)) or (display_name(names.get(item[0])) if item else None) or UNKNOWN_TARGET


def target_name(ref, context):
    """The native name of a target she chose, from the scene or the mod's own row; a container with no name reads by its native kind."""
    context = context if isinstance(context, Mapping) else {}
    scene = (context.get("targets") or {}).get(ref) if isinstance(ref, str) else None
    if isinstance(scene, Mapping) and (name := display_name(scene.get("display_name") or scene.get("name"))):
        return name
    ui = context.get("mines_ui") if isinstance(context.get("mines_ui"), Mapping) else {}
    for row in ui.get("extras_targets", ()) or ():
        if isinstance(row, Mapping) and row.get("ref") == ref and (name := display_name(row.get("label"))):
            return name
    for row in ui.get("openable_containers", ()) or ():
        if isinstance(row, Mapping) and row.get("ref") == ref and isinstance(row.get("kind"), str):
            return row["kind"].replace("_", " ")  # gift_container: (BC)-1 has no display name on the wire (OpenableContainer.cs)
    return None


def text(value, names=None):
    """Render native fact text, including legacy receipts whose label was an ID."""
    if not isinstance(value, str):
        return ""
    value = REF.sub(lambda m: ref_name(m[0], names), value)
    value = QUALIFIED.sub(lambda m: item_name(m[0], names), value)
    return GUID.sub("identitas tempat belum diketahui", value)


def source_names(context):
    """Labels from the same native observation as an event, not a global item table."""
    names = {}
    for row in (context or {}).get("inventory", ()):
        if isinstance(row, Mapping) and (name := display_name(row.get("display_name") or row.get("name"))):
            names[row.get("qualified_item_id")] = name
    for row in ((context or {}).get("targets") or {}).values():
        if isinstance(row, Mapping) and (name := display_name(row.get("display_name") or row.get("name") or row.get("label"))):
            names[row.get("qualified_item_id") or row.get("item")] = name
    for row in ((context or {}).get("world_knowledge") or {}).get("discoveries", ()):
        if isinstance(row, Mapping) and row.get("kind") in ("item_seen", "item_obtained"):
            if name := display_name(str(row.get("text") or "").split(":", 1)[0]):
                names.setdefault(row.get("subject"), name)
    names.pop(None, None)
    return names


def moved_into_bag(moves):
    """{item: net count her verified chest moves put in the bag}; negative when they took it out of the bag."""
    net = {}
    for move in moves or ():
        net[move["item"]] = net.get(move["item"], 0) + move["taken"] - move["stored"]
    return net


def inventory_gains(before, after, names, moves=None):
    """Positive native bag deltas her verified chest moves do not explain; they do not establish how an item was obtained."""
    old, new = (before or {}).get("inventory"), (after or {}).get("inventory")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return []
    into_bag = moved_into_bag(moves)
    return [dict(item=q, name=item_name(q, names), quantity=n-old.get(q, 0)-into_bag.get(q, 0)) for q, n in new.items()
            if type(n) is int and type(old.get(q, 0)) is int and n-old.get(q, 0)-into_bag.get(q, 0) > 0]


MOVE_ITEMS = 4


def _move_items(rows, names):
    parts = [f"{item_name(item, names)} x{count}" for item, count in rows]
    return ", ".join(parts[:MOVE_ITEMS] + ([f"+{len(parts) - MOVE_ITEMS} jenis lagi"] if len(parts) > MOVE_ITEMS else []))


def moves_text(moves, names=None, *, stored_told=False):
    """FIX77B (a56 T131, T162, T169: her own parsnips out of her chest read `Parsnip +6`, and she called it a gain): what moved, from
    where to where, net per chest. A stack that went out and back says so. `stored_told`: the row already names what went in."""
    chests = {}
    for move in moves or ():
        chests.setdefault(move["source"], []).append(move)
    segments = []
    for source, rows in chests.items():
        chest = ref_name(source, names)
        taken = [(r["item"], r["taken"] - r["stored"]) for r in rows if r["taken"] > r["stored"]]
        stored = [(r["item"], r["stored"] - r["taken"]) for r in rows if r["stored"] > r["taken"]]
        both = [(r["item"], r["taken"]) for r in rows if r["taken"] and r["taken"] == r["stored"]]
        if taken:
            segments.append(f"dipindah dari {chest} ke tas: {_move_items(taken, names)}, bukan barang baru")
        if stored and not stored_told:
            segments.append(f"dipindah dari tas ke {chest}: {_move_items(stored, names)}")
        if both:
            segments.append(f"dari {chest} ke tas lalu kembali ke {chest}: {_move_items(both, names)}")
    return "; ".join(segments)


MACHINE_KEYS = frozenset({"id", "session_id", "request_id", "request_ids", "runtime_id", "observation_session", "ref", "target_ref", "aliases",
                          "source_fact_id", "container_token", "receiver_token", "names", "place_names"})
PLACE_KEYS = frozenset({"location", "place", "current_place", "event_place", "from_place", "to_place"})


def projection(value, names=None, places=None):
    """Read-only native facts for a human; the source receipt is never changed.

    Machine dictionaries keyed by item ID become rows, so two unknown kinds
    keep their separate counts instead of silently overwriting each other.
    """
    if isinstance(value, Mapping):
        names = dict(names or {}, **(value.get("names") or {}))
        places = dict(places or {}, **(value.get("place_names") or {}))
        if isinstance(value.get("item"), str) and (label := display_name(value.get("name"))):
            names[value["item"]] = label
        if value and all(isinstance(k, str) and QUALIFIED.fullmatch(k) for k in value):
            return [dict(name=item_name(k, names), value=projection(v, names, places)) for k, v in value.items()]
        result = {}
        for key, part in value.items():
            if key in MACHINE_KEYS:
                continue
            if key in PLACE_KEYS and isinstance(part, str):
                from arti_runtime.place_names import source_place
                result[key] = places.get(part) or source_place(part)
            else:
                result[key] = projection(part, names, places)
        return result
    if isinstance(value, (list, tuple)):
        return [projection(v, names, places) for v in value]
    return text(value, names) if isinstance(value, str) else value
