"""ADR 194: names and effects of the open receiver's published item buttons."""
from collections.abc import Mapping
from arti_stardew_economy import hard_dispose_refusal
from arti_stardew_cursor_item import held_disposal_refusal
from arti_runtime.fact_names import item_name


def describe(view, actions):
    rows = []
    held = (view.cursor_settle or {}).get("cursor") or {}
    for c in view.components:
        if c.get('field_path', '').endswith(('.fillStacksButton', '.organizeButton')):
            key = 'press:A:%s' % c['id']
            if key in actions:
                rows.append([key, 'Tambah ke stack yang sudah ada di peti (native; bisa banyak barang)'
                    if c['field_path'].endswith('.fillStacksButton') else 'Organize: rapikan urutan dan gabungkan stack; tidak mengosongkan tas'])
        value = c.get("value") or {}
        qid = value.get("item")
        if c.get("kind") == "slot" and qid:
            name = item_name(qid, name=c.get("label"))
            refusal = hard_dispose_refusal(dict(value, qualified_item_id=qid))
            base = "%s x%s" % (name, value.get("stack", 1))
            if refusal or c.get("protected_item") is True:
                rows.append(["refused:%s" % c["id"], base + ": alat/barang quest; jual, kirim, buang dan jatuhkan ditolak"])
            for key, word in (("press:A:%s" % c["id"], "angkat ke tangan"),
                              ("press:A:%s:shift" % c["id"], "pindahkan lewat menu")):
                if key not in actions or c.get("accepts_item") is False:
                    continue
                if view.menu_type == "ShopMenu":
                    if refusal or c.get("protected_item") is True:
                        continue
                    word = "jual stack ke toko" if not key.endswith(":shift") else "jual lewat menu toko"
                elif view.menu_type == "ItemGrabMenu" and key.endswith(":shift"):
                    word = "ambil dari peti" if "ItemsToGrabMenu" in str(c.get("field_path", "")) else "simpan ke peti"
                elif held:
                    word = "taruh/tukar barang di tangan dengan slot ini"
                rows.append([key, base + ": " + word])
            if c.get("accepts_item") is False:
                rows.append(["refused:%s" % c["id"], base + ": game tidak menerima barang ini di menu ini"])
        elif c.get("kind") in ("trash", "drop") and held:
            qid = held.get("qualified_item_id") or held.get("item")
            verb = "buang" if c["kind"] == "trash" else "jatuhkan ke tanah"
            offered = False
            for key in ("press:A:%s" % c["id"], "settle:%s" % c["id"]):
                if key in actions:
                    offered = True
                    rows.append([key, "%s x%s: %s" % (item_name(qid, name=held.get("name")),
                        held.get("stack", 1), verb)])
            why = held_disposal_refusal(held, c["kind"])
            if why and not offered:
                rows.append(["refused:%s" % c["id"], "%s x%s: %s ditolak (%s)" % (
                    item_name(qid, name=held.get("name")), held.get("stack", 1), verb, why)])
        elif c.get("kind") in ("use", "eat") and held:
            for key in ("press:A:%s" % c["id"], "press:X:%s" % c["id"]):
                if key in actions:
                    qid = held.get("qualified_item_id") or held.get("item")
                    rows.append([key, "%s x%s: pakai" % (item_name(qid, name=held.get("name")),
                        held.get("stack", 1))])
    for key, action in (actions.items() if isinstance(actions, Mapping) else ()):
        if isinstance(action, tuple) and action[0] == "container_transfer":
            wire = action[1]
            qid = wire.get("qualified_item_id")
            if hard_dispose_refusal(wire) is None:
                rows.append([key, "%s x%s: %s" % (item_name(qid, getattr(view, "item_names", None)),
                    wire.get("quantity") or wire.get("stack"),
                    "ambil dari peti" if wire.get("mode") == "take_out" else "simpan ke peti")])
        elif str(key).startswith("buy:"):
            parts = str(key).split(":")
            if len(parts) != 3:
                continue
            control, qty = parts[1], parts[2]
            stock = next((c for c in view.components if str(c.get("id")) == control), None)
            if stock:
                value = stock.get("value") or {}
                qid = value.get("item")
                label = item_name(qid, name=stock.get("label")) if qid else stock.get("label")
                rows.append([key, "%s x%s: beli" % (label, qty)])
            elif (view.shop or {}).get("stock"):
                row = next((r for r in view.shop["stock"] if str(r[8]) == control), None)
                if row:
                    rows.append([key, "%s x%s: beli" % (row[2], qty)])
    return rows
