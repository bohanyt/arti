"""ADR 194: experienced places to make room, beside their startable choices.
ADR 198: the same rows also name places where what she carries can be used."""
from collections.abc import Mapping
from arti_stardew_world import commerce_purpose, location_arg, places, travel_options
from arti_runtime.use_places import purpose_text


SPACE = ("inventory_full", "bag_full", "no_space", "inventory_cannot_accept")
LOCAL = {
    "chest": "Peti yang pernah dilihat: simpan atau ambil.",
    "shipping_bin": "Kotak pengiriman: pilih barang; dibayar sesudah tidur.",
    "shop_counter": "Konter: stok/harga, beli atau jual.",
}
ROOM_PREFIX = ("Peti yang pernah dilihat:", "Kotak pengiriman:", "Konter:",
               "Tanah bisa dicangkul", "Tanaman tertanam:", "Air dengan data pancing:")


def need_space(facts, policy):
    return facts.free_slots == 0 or any(
        any(token in str(r.get("reason", "")) for token in SPACE) for r in policy.history[-1:])


def _merge(space, use):
    by = {}
    for row in space + use:
        loc = row["location"]
        if loc in by:
            seen = {text for _, text in by[loc].get("activities") or ()}
            extra = [(k, t) for k, t in row.get("activities") or () if t not in seen]
            by[loc] = dict(by[loc], activities=list(by[loc].get("activities") or ()) + extra,
                           use_kinds=set(by[loc].get("use_kinds") or ()) | set(row.get("use_kinds") or ()))
        else:
            by[loc] = dict(row)
    return list(by.values())


def destinations(context, facts, policy, *, known=None, kinds=None, options=None):
    from arti_runtime.experience_facts import place_sources
    from arti_runtime.use_places import travel as use_travel
    known = place_sources(context.get("world_knowledge") or {}) if known is None else known
    if options is None:
        options = travel_options(context, facts.location, now=facts.time_of_day)
    space = []
    if need_space(facts, policy):
        space = [o for o in options
                 if o["location"] in known and any(text.startswith(
                     ("Peti yang pernah dilihat:", "Kotak pengiriman:", "Konter:")) for _, text in o["activities"])]
    use = []
    for option in use_travel(context, facts, known=known, kinds=kinds, options=options):
        texts = [("fact", purpose_text(k)) for k in option.get("use_kinds") or ()]
        use.append(dict(option, activities=list(option.get("activities") or ()) + texts))
    return _merge(space, use)


def withheld(context, facts, policy, *, known=None, kinds=None, offered=None):
    """Commerce and use-places she knows, with the native reason they are not a startable trip."""
    from arti_runtime.experience_facts import place_sources
    from arti_runtime.use_places import carried_uses, matches
    known = place_sources(context.get("world_knowledge") or {}) if known is None else known
    offered = {o["location"] for o in destinations(context, facts, policy)} if offered is None else offered
    kinds = carried_uses(context.get("inventory")) if kinds is None else kinds
    rows = []
    for p in places(context):
        if p["id"] not in known or p["id"] == facts.location or p["id"] in offered:
            continue
        purposes = list(commerce_purpose(p, context))
        if not need_space(facts, policy):
            purposes = []
        purposes.extend(purpose_text(k) for k in matches(p, kinds) if purpose_text(k) not in purposes)
        if not purposes:
            continue
        why = p.get("locked")
        if why is None and p.get("go_to_available") is False:
            why = "go_to unavailable"
        if why is None and not p.get("route"):
            why = "route unknown"
        rows.append(dict(tujuan=location_arg(p["id"]), guna=" ".join(purposes),
                         mengapa=str(why) if why else "tidak terjangkau"))
    return rows


def counters(context):
    """Room-wide native shop-counter readiness and hours; never a curated timetable."""
    rows = []
    for r in (context.get("mines_ui") or {}).get("extras_targets", ()):
        if not isinstance(r, Mapping) or r.get("kind") != "shop_counter":
            continue
        avail = r.get("availability") if isinstance(r.get("availability"), Mapping) else {}
        def hhmm(value):
            return None if type(value) is not int else "%02d:%02d" % (value // 100 % 24, value % 100)
        ready = r.get("ready") is True
        row = dict(siap=ready, buka_sekarang=avail.get("open_now"), buka=hhmm(avail.get("opens_at")),
                   tutup=hhmm(avail.get("closes_at")), penjaga=avail.get("shopkeeper_present"),
                   dasar=avail.get("hours_basis"))
        if not ready:
            if avail.get("open_now") is False:
                row["keadaan"] = "tutup"
            elif avail.get("shopkeeper_present") is False:
                row["keadaan"] = "penjaga tidak di tempat"
            else:
                row["keadaan"] = str(r.get("refused") or "tidak melayani")
        rows.append(row)
    return rows


def _use_page(context, facts, policy, calls):
    from arti_runtime.travel_time import estimate
    from scripts.live7.interactions import ordinary_targets
    offered = {(c.name, c.arg) for c in calls}
    from arti_runtime.experience_facts import place_sources
    from arti_runtime.use_places import carried_uses
    known = place_sources(context.get('world_knowledge') or {})
    kinds = carried_uses(context.get('inventory'))
    trips = destinations(context, facts, policy, known=known, kinds=kinds)
    rows = []
    for o in trips:
        if ("pergi_ke", o["arg"]) not in offered:
            continue
        purposes = [text for _, text in o["activities"] if text.startswith(ROOM_PREFIX)]
        eta = estimate(facts.location, o["route"], policy)
        rows.append(dict(tujuan=o["arg"], guna=" ".join(purposes), jalan_menit=eta["minutes"],
                         dasar=eta["basis"]))
    if need_space(facts, policy):
        for r in ordinary_targets(context):
            kind, ref = r.get("kind"), r.get("ref")
            if kind not in LOCAL or ("aksi", ref) not in offered:
                continue
            rows.append(dict(tujuan=ref, guna=r.get("label") or LOCAL[kind], jalan_menit=0, dasar="di peta ini"))
    rows.extend(withheld(context, facts, policy, known=known, kinds=kinds,
                         offered={o['location'] for o in trips}))
    return rows


def bag_summary(context, facts, own_bag=None):
    if not context.get('inventory_complete') or type(facts.free_slots) is not int:
        return None
    used = len({r['slot'] for r in context.get('inventory', ()) if type(r.get('slot')) is int})
    if (isinstance(own_bag, Mapping) and type(own_bag.get('used')) is int
            and own_bag.get('free') == facts.free_slots and own_bag.get('capacity') == own_bag['used'] + facts.free_slots):
        used = own_bag['used']
    return 'Tas %d/%d, sisa %d slot; menu tas memakai kapasitas yang sama.' % (
        used, used + facts.free_slots, facts.free_slots)


def item_terms(goods, *, prices=False):
    """A destination line, with every held kind/quality; never invent a missing price."""
    grouped = {}
    for row in goods:
        key = (row.get('qualified_item_id'), row.get('quality', 0), row.get('unit_price'))
        if not key[0]:
            continue
        name = row.get('name') or row.get('display_name') or key[0]
        if key[1]:
            name += ' q%s' % key[1]
        prior = grouped.get(key, (name, 0))
        grouped[key] = (name, prior[1] + row.get('stack', 0))
    return ', '.join('%s x%d%s' % (name, n, (' @%sg' % price if type(price) is int else ' @?g') if prices else '')
                     for (_, _, price), (name, n) in grouped.items())


def shipping_value(goods):
    rows = [r for r in goods if r.get('shippable') is not False and r.get('ship_policy') in (None, 'ok')]
    known = [r for r in rows if type(r.get('unit_price')) is int and r.get('price_source') != 'shop']
    if rows and not known:
        return 'harga belum terbaca; dibayar sesudah tidur'
    amount = sum(r['stack'] * r['unit_price'] for r in known)
    return 'terbaca %sg%s; dibayar sesudah tidur' % (amount, ' + harga belum terbaca' if len(known) != len(rows) else '')


def page(context, facts, policy, calls):
    """ADR 206: bag/container decisions need destinations even before the bag fills.

    Travel admission does not determine whether a known facility is a fact. A closed
    counter still exists, and the row must say that its acceptance/hours are unknown.
    """
    from arti_stardew_live7 import goods_facts, hard_dispose_refusal
    from arti_runtime.travel_time import estimate
    from arti_runtime.use_places import carried_uses, matches, purpose_text
    from scripts.live7.interactions import ordinary_targets
    if not (need_space(facts, policy) or any(c.name == 'inventori' for c in calls)
            or any(r.get('kind') in LOCAL for r in ordinary_targets(context))):
        return _use_page(context, facts, policy, calls)
    goods = goods_facts(context)
    sell = [r for r in goods if hard_dispose_refusal(r) is None]
    rows = []
    graph = (context.get('world_knowledge') or {}).get('world_graph') or {}
    kinds = carried_uses(context.get('inventory'))
    chests_by_place = {}
    for chest in getattr(getattr(policy, 'chest_memory', None), 'rows', {}).values():
        chests_by_place.setdefault(chest['location'], []).append(chest)
    for place in places(context):
        if place.get('experience_known') is False:
            continue
        loc = place['id']
        path = [] if loc == facts.location else place.get('motor_route') or place.get('route')
        if path is None:
            continue
        distance_info = estimate(facts.location, path, policy)
        distance = distance_info['minutes']
        native = place.get('facts') or ()
        sinks = []
        chests = chests_by_place.get(loc, ())
        if loc in context.get('seen_chest_places', ()) or chests:
            tiles = ','.join('%s,%s' % tuple(r['tile']) for r in chests)
            sinks.append('Peti yang pernah dilihat%s: isi tas, termasuk alat; ruang/penerimaan dicek saat buka' % (' ' + tiles if tiles else ''))
        if any(str(f).startswith('shipping bin:') for f in native):
            accepted = [r for r in sell if r.get('shippable') is not False and r.get('ship_policy') in (None, 'ok')]
            accepted = [dict(r, unit_price=None) if r.get('price_source') == 'shop' else r for r in accepted]
            sinks.append('Kotak pengiriman: %s; %s; penerimaan native dicek di kotak' %
                         (item_terms(accepted, prices=True), shipping_value(accepted)))
        if any(str(f).startswith('shop counter ') for f in native):
            refused = getattr(policy, 'shop_refused', {}).get(loc, ())
            candidates = [r for r in sell if r.get('qualified_item_id') not in refused]
            candidates = [dict(r, unit_price=None) if r.get('price_source') == 'shop' and r.get('price_location') != loc
                          else r for r in candidates]
            sinks.append('konter: %s; penerimaan/harga: menu toko' % item_terms(candidates, prices=True))
        if sinks:
            sinks.extend(purpose_text(k) for k in matches(place, kinds)
                         if k not in ('bin', 'shop'))
            arg = 'home' if loc == graph.get('home') else location_arg(loc)
            rows.append(dict(tujuan=arg, jalan_menit=distance, guna='; '.join(sinks),
                             dasar='antar peta; ' + distance_info['basis'] + ('; langkah lokal belum ditaksir' if not distance else ''),
                             **({'akses': str(place['locked'])} if place.get('locked') else {})))
    # Other uses (soil/water/crops) retain their original facts and routes.
    sink_args = {r['tujuan'] for r in rows}
    for original in _use_page(context, facts, policy, calls):
        if original['tujuan'] not in sink_args:
            rows.append(original)
        elif original.get('mengapa'):
            next(r for r in rows if r['tujuan'] == original['tujuan'])['mengapa'] = original['mengapa']
    return rows
