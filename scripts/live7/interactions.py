"""Named native interaction facts. Walking/facing use the existing generic primitives."""
from collections.abc import Mapping
from arti_stardew_world_extras import stand_tiles


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


def ordinary_targets(context):
    ui = context.get("mines_ui") or {}
    gifts = [dict(row, label=row["kind"], action="aksi", arg=row["ref"])
             for row in ui.get("openable_containers", ()) if isinstance(row, Mapping)]
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
    return typed + action_tiles(context, typed)


def action_tiles(context, typed=()):
    """Use scene affordance classes to offer the existing ACTION approach."""
    from arti_runtime.map_affordances import observed
    from arti_stardew_command import TILE_MAX
    occupied = {tuple(row["tile"]) for row in typed}
    out = []
    claims, _ = observed(context)
    for ref, row in claims.items():
        x, y = row["tile"]
        if (x, y) in occupied or not (set(row["classes"]) & {"press", "pick_up"}):
            continue
        stands = [[a, b] for a, b in ((x, y + 1), (x - 1, y), (x, y - 1), (x + 1, y))
                  if 0 <= a <= TILE_MAX and 0 <= b <= TILE_MAX]
        if stands:
            occupied.add((x, y))
            kind = "map_action" if row["cell"] else "scene_pick_up"
            out.append(dict(ref=ref, tile=[x, y], stands=stands, kind=kind, status="available", label=None))
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
