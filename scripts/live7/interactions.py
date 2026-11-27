"""Named native interaction facts. Walking/facing use the existing generic primitives."""
from collections.abc import Mapping
import threading
from types import MappingProxyType
from arti_stardew_world_extras import stand_tiles

_OPEN_REACH_CACHE: dict = {}
_OPEN_REACH_LOCK = threading.RLock()
_UNSET = object()


def _xy(v):
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(type(n) is int for n in v):
        return (v[0], v[1])
    return (v["x"], v["y"]) if isinstance(v, Mapping) and type(v.get("x")) is int and type(v.get("y")) is int else None


def _cardinal_stands(x, y):
    from arti_stardew_command import TILE_MAX
    return [(a, b) for a, b in ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y)) if 0 <= a <= TILE_MAX and 0 <= b <= TILE_MAX]


def open_reach(grid, start):
    """Bounded content-keyed reuse of open_paths(grid, start) across one page's repeated calls."""
    start = _xy(start)
    cell = grid.get(start) if isinstance(grid, Mapping) and 0 < len(grid) <= 4096 and start in grid else None
    walkable = {**grid, start: "."} if cell in ("#", "x") else grid if cell == "." else None
    if walkable is None:
        return {}
    key = (start, tuple(walkable.items()))
    with _OPEN_REACH_LOCK:
        if (hit := _OPEN_REACH_CACHE.get(key)) is not None:
            return hit
    from arti_stardew_goal_routes import open_paths
    parents = MappingProxyType(open_paths(walkable, start))
    with _OPEN_REACH_LOCK:
        if len(_OPEN_REACH_CACHE) >= 8:
            _OPEN_REACH_CACHE.pop(next(iter(_OPEN_REACH_CACHE)))
        _OPEN_REACH_CACHE[key] = parents
    return parents


def _fresh_reach(context, *, here=None):
    if not isinstance(context, Mapping) or context.get("ui_complete") is False:
        return None
    here = _xy(context.get("actor_tile") if here is None else here)
    if here is None:
        return None
    loc, mines_ui = context.get("location_id"), context.get("mines_ui")
    raw_meta = mines_ui.get("nav_grid") if isinstance(mines_ui, Mapping) else None
    if isinstance(raw_meta, Mapping):
        ml = raw_meta.get("location")
        if (raw_meta.get("stale") is True or (isinstance(ml, str) and ml and isinstance(loc, str) and loc and ml != loc)
                or (raw_meta.get("actor_tile") is not None and _xy(raw_meta.get("actor_tile")) != here)):
            return None
    grid = context.get("nav_grid")
    if grid is None and isinstance(raw_meta, Mapping):
        from arti_stardew_world_extras import nav_grid as parse_nav_grid
        grid = parse_nav_grid(mines_ui, loc if isinstance(loc, str) and loc else None)
    parents = open_reach(grid, here) if isinstance(grid, Mapping) and here in grid else {}
    return (grid, parents) if parents else None


def _object_reachable(tile, stands, reach):
    if reach is None or tile is None:
        return True
    grid, parents = reach
    return any(s in parents for s in stands) or tile not in grid or not all(s in grid for s in _cardinal_stands(*tile))


def interrupted_travel_fact(policy, date, calls):
    trip = policy.interrupted_travel
    if trip is None or tuple(trip["date"]) != tuple(date):
        return None
    return dict(trip, currently_offered=any(c.name == trip["name"] and c.arg == trip["arg"] for c in calls),
        status="native event interrupted this trip; choose again from current offers")


# Native named targets for ordinary ACTION. Kinds only; tiles, rooms and bundles come from the mod.
ORDINARY_KINDS = ("shop_counter", "shipping_bin", "blacksmith", "animal_shop", "museum_counter", "community_note", "ladder_down", "ladder_up", "shaft")
CHEST_KIND = "chest"


def bundle_facts(row):
    """fix54F: what a published note area holds (native area + bundle names), never a deposit plan."""
    names = row.get("bundles")
    out = {"area": row["area"]} if type(row.get("area")) is int else {}
    if isinstance(names, (list, tuple)) and all(isinstance(n, str) for n in names):
        out["bundles"] = [n[:80] for n in names[:10]]
    return out


def bin_capture_tile(ref, target):
    """ADR 231 part A: the tile of the mod's faced capture of the shipping bin her row names, else None.

    The bin's catalogue row (``shipping_bins``) is ``xt:bin:<place>:<x>,<y>``, the building's top-left tile. The mod's
    faced capture of the same building is ``xt:shipping_bin:<place>:<gx>,<gy>:<name>`` at the grab tile, one of the
    2x1 tiles (``ShippingBin.doAction``: tileX..tileX+1, tileY). Attempt 87: the two never compared equal, so the
    capture was never offered to her ``aksi`` and every open ended ``shipping_bin_native_open_not_offered``."""
    import re
    m = re.fullmatch(r"xt:bin:(.+):(\d+),(\d+)", str(ref or ""))
    if m is None or not isinstance(target, Mapping) or target.get("kind") != "shipping_bin":
        return None
    place, x, y = m.group(1), int(m.group(2)), int(m.group(3))
    head = "xt:shipping_bin:%s:" % place
    source = str(target.get("source_fact_id") or "")
    if not source.startswith(head):
        return None
    tile = source[len(head):].split(":", 1)[0]
    if tile not in ("%d,%d" % (x, y), "%d,%d" % (x + 1, y)):
        return None
    gx, gy = (int(v) for v in tile.split(","))
    return (gx, gy) if (target.get("tile_x"), target.get("tile_y")) == (gx, gy) else None


def faced_named_target(call, target, named):
    """fix54F: the mod's own faced capture of the very target an aksi run named (same ref, kind, tile)."""
    if (getattr(call, "name", None) == "aksi" and call.arg and call.tile is not None
            and bin_capture_tile(call.arg, target) is not None):
        return any(row.get("ref") == call.arg and row.get("kind") == "shipping_bin"
                   and tuple(row.get("tile", ())) == tuple(call.tile) and row.get("status") in ("openable", "available")
                   for row in named)
    if (getattr(call, "name", None) != "aksi" or not call.arg or call.tile is None
            or not isinstance(target, Mapping) or target.get("kind") not in (*ORDINARY_KINDS, CHEST_KIND)
            or (target.get("tile_x"), target.get("tile_y")) != tuple(call.tile)):
        return False
    ref = target.get("source_fact_id")
    same_ref = ref == call.arg
    if not same_ref and target["kind"] in ("ladder_down", "ladder_up") and call.arg.startswith("xt:mine:"):
        # The native catalogue and faced capture name the same floor/kind/tile differently (ADR 146).
        same_ref = ref == call.arg.replace("xt:mine:", "xt:" + target["kind"] + ":", 1)
    if not same_ref:
        return False
    return any(row.get("ref") == call.arg and row.get("kind") == target["kind"]
               and tuple(row.get("tile", ())) == tuple(call.tile) and row.get("status") in ("openable", "available")
               for row in named)


def _lazy_reach(context, parent=None):
    if parent is not None:
        return parent
    box = [_UNSET]
    return lambda: box.__setitem__(0, _fresh_reach(context)) or box[0] if box[0] is _UNSET else box[0]


def ordinary_targets(context):
    ui = context.get("mines_ui") or {}
    get_reach = _lazy_reach(context)
    gifts = []
    for row in ui.get("openable_containers", ()):
        if not isinstance(row, Mapping):
            continue
        tile, raw_stands = _xy(row.get("tile")), row.get("stands")
        stands = ([s for s in (_xy(v) for v in raw_stands) if s is not None] if isinstance(raw_stands, (list, tuple))
                  else _cardinal_stands(*tile) if tile is not None else None)
        if isinstance(raw_stands, (list, tuple)) and not stands:
            continue
        if tile is not None and stands and not _object_reachable(tile, stands, get_reach()):
            continue
        gifts.append(dict(row, label=row["kind"], action="aksi", arg=row["ref"]))
    counters = [dict(ref=row["ref"], tile=[row["x"], row["y"]],
        label={"ladder_down": "Turun satu lantai", "shaft": "Turun lewat lubang (bisa lebih dari satu lantai)",
               "ladder_up": "Tangga naik: keluar Mines",
               "shop_counter": "Buka toko: lihat stok/harga, beli atau jual; toko menentukan barang yang diterima"}.get(row["kind"], row.get("label")),
        status="available" if row.get("ready") is True or row.get("kind") in ("ladder_down", "ladder_up", "shaft")
               else row.get("refused", "unknown"),
        kind=row["kind"], action="aksi", arg=row["ref"],
        **({"availability": row["availability"]} if isinstance(row.get("availability"), Mapping) else {}),
        **({"stands": stands} if (stands := stand_tiles(row)) is not None else {}),
        **(bundle_facts(row) if row.get("kind") == "community_note" else {}))
        for row in ui.get("extras_targets", ()) if isinstance(row, Mapping)
        and row.get("kind") in ORDINARY_KINDS]
    from arti_stardew_economy import bin_entries
    bins = [dict(ref=r["ref"], tile=r["tile"], stands=r["reach_tiles"], kind="shipping_bin", status="openable",
                 label="Buka kotak pengiriman: pilih barang/jumlah; dibayar besok") for r in bin_entries(ui)]
    typed = gifts + counters + bins + chest_targets(ui, context.get("known_chests"))
    return typed + action_tiles(context, typed, _get_reach=get_reach)


def action_tiles(context, typed=(), *, _get_reach=None):
    """Use scene affordance classes to offer the existing ACTION approach."""
    from arti_runtime.map_affordances import observed
    occupied = {tuple(row["tile"]) for row in typed if isinstance(row.get("tile"), (list, tuple))}
    get_reach = _lazy_reach(context, _get_reach)
    out = []
    claims, _ = observed(context)
    for ref, row in claims.items():
        x, y = row["tile"]
        if (x, y) in occupied or not (set(row["classes"]) & {"press", "pick_up"}):
            continue
        stands = _cardinal_stands(x, y)
        kind = "map_action" if row["cell"] else "scene_pick_up"
        if not stands or (kind == "scene_pick_up" and not _object_reachable((x, y), stands, get_reach())):
            continue
        occupied.add((x, y))
        out.append(dict(ref=ref, tile=[x, y], stands=[[a, b] for a, b in stands], kind=kind, status="available", label=None))
    return out


def chest_targets(ui, known=()):
    """FIX72D (a53: 5 decisions about looking in a chest, d7 J1197 to d12 J251, each answered with `inventori`): a placed
    chest is an ordinary ACTION target like a counter. Native's published chests, then the ones she remembers on this map
    beyond its radius. Opening is all it does; the chest's own menu offers what can move and native's arm admission decides."""
    from arti_stardew_economy import store_chests
    rows = {}
    for chest in store_chests(ui):
        rows[chest["ref"]] = dict(ref=chest["ref"], tile=chest["tile"], kind=CHEST_KIND,
                                  status="in_use" if chest.get("usable") is False else "openable")
    for chest in known or ():
        if isinstance(chest, Mapping) and chest.get("ref") not in rows:
            rows[chest["ref"]] = dict(ref=chest["ref"], tile=list(chest["tile"]), kind=CHEST_KIND, status="openable",
                                      remembered=True)
    return list(rows.values())
