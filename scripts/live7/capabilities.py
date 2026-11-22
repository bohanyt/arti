"""Native capability meaning, local preparation and paired effect credit (ADR 38).

Knowledge describes possibilities. Only fresh native targets authorize inputs.
"""
from collections.abc import Mapping
import json
import re


def owned_rods(context):
    """Join native FishingRod identities to the complete bag; names/IDs do not establish rod type."""
    fishing = context.get("fishing") or {}
    slot, item = fishing.get("rod_slot"), fishing.get("rod_item")
    if not context.get("inventory_complete"):
        return ()
    rows = fishing.get("rods", [[slot,item]])
    if (not isinstance(rows,(list,tuple)) or len(rows)>48 or any(not isinstance(r,(list,tuple)) or len(r)!=2
            or type(r[0]) is not int or r[0]<0 or not isinstance(r[1],str) or not r[1] for r in rows)):
        return ()
    identities = set(tuple(r) for r in rows)
    return tuple(dict.fromkeys(r["qualified_item_id"] for r in context.get("inventory", ())
        if isinstance(r, Mapping) and type(r.get("slot")) is int and isinstance(r.get("qualified_item_id"),str)
        and (r.get("slot"),r.get("qualified_item_id")) in identities
        and r["qualified_item_id"] and type(r.get("stack")) is int and r["stack"] > 0))


def fishing_path(snapshot, context, state=None):
    """Reuse Wave 1's bounded local shore search; the native location must allow fishing."""
    import arti_stardew_skills as S
    from arti_stardew_native_targets import fishing_search_target
    from arti_stardew_goal_routes import open_paths, open_path
    fishing = context.get("fishing") or {}
    grid = context.get("nav_grid") or {}
    if not context.get("ui_complete") or fishing.get("can_fish_here") is not True or not grid:
        return None
    here = S._wave1_xy(S._g(S._g(snapshot,"motor"),"tile"))
    target = fishing_search_target(grid,here,state if state is not None else {})
    return open_path(open_paths(grid,here),target) if target is not None else None


def capability_facts(facts, context):
    """A compact first-page index; full native catalog stays on the existing detail pages."""
    import arti_stardew_skills as S
    catalog = context.get("world_knowledge") or {}
    places = (catalog.get("world_graph") or {}).get("places") or ()
    services = [[p["id"],f] for p in places if isinstance(p,Mapping) and isinstance(p.get("id"),str)
                for f in p.get("facts",()) if isinstance(f,str) and f.startswith(("shop counter ","fishing:","entry hours ","entry status now:"))]
    result = dict(fishing=dict(rod_owned=S.PRECONDITIONS["rod_owned"](facts,None),
        sources=list(catalog.get("game_facts") or ()),
        local_water_edges=len((context.get("fishing") or {}).get("water_edges") or ())),
        commerce=dict(fact="Entry/door hours do not establish counter readiness; stock and prices come from the open native menu. Shipping pays next morning.",
            local_counters=[dict(ref=r.get("ref"),shop=r.get("label"),ready=r.get("ready"),refused=r.get("refused"),
                **({"availability": r["availability"]} if isinstance(r.get("availability"), Mapping) else {}))
                for r in (context.get("mines_ui") or {}).get("extras_targets",())
                if isinstance(r,Mapping) and r.get("kind")=="shop_counter"]),
        places=services)
    # Modded catalogs retain their complete meaning in the existing read-only world pages.
    # A large remote catalog cannot displace the current menu/skill's actionable facts.
    size = lambda: len(json.dumps(result,ensure_ascii=False,separators=(",",":")).encode())
    if size()>2400:
        result["places"] = dict(count=len(services),detail="world.world_details.places[].facts")
    if size()>2400:
        result["commerce"]["local_counters"] = dict(count=len(result["commerce"]["local_counters"]),detail="interact_targets")
    if size()>2400:
        result["fishing"]["sources"] = dict(count=len(result["fishing"]["sources"]),detail="game_facts",
            fact="Native rod sources and their conditions are on the read-only fact pages.")
    return result


def commerce_choice(view, chosen):
    """Name a chosen native stock/bag control, including a refused/no-effect attempt."""
    from scripts.live7.transactions import stock_control
    if stock_control(view, chosen) is not None:
        return "buy"
    if not isinstance(chosen, tuple) or chosen[0] != "ui_press" or chosen[1].get("btn") not in ("A", "X"):
        return None
    if view.held_item or (view.cursor_settle or {}).get("cursor"):
        return None
    cid = chosen[1].get("choice", chosen[1].get("snapped_id"))
    if any(str(r[0]) == str(cid) and r[7] for r in (view.shop or {}).get("sell", ())):
        return "sell"
    return None


def observe_disposal(owner, view, chosen, proof):
    """Credit the model's selected bag slot from authenticated before/after ledgers.

    The caller authenticates request, receiver, scope, release and fresh readback.
    The existing economy verifier owns paired sale/shipment semantics.
    """
    import arti_stardew_economy as E
    if (not isinstance(chosen,tuple) or chosen[0]!="ui_press" or chosen[1].get("btn") not in ("A","X")
            or proof.get("effect")!="changed" or not proof.get("request_id")):
        return None
    kind = "sell" if view.menu_type=="ShopMenu" else "ship" if view.menu_type=="ItemGrabMenu" else None
    if kind is None:
        return None
    cid = chosen[1].get("choice",chosen[1].get("snapped_id"))
    component = next((c for c in view.components if str(c["id"])==str(cid) and c.get("kind")=="slot"),None)
    path = re.search(r"inventory\[(\d+)\]$",(component or {}).get("field_path",""))
    item = (component or {}).get("value") or {}
    if path is None or not item.get("item") or (component or {}).get("accepts_item") is False:
        return None
    seen = owner.state.setdefault("disposal_requests",[])
    if proof["request_id"] in seen:
        return None
    # Both cursor stores must be empty: a pickup/swap is not a disposal.
    if any(s.get("cursor_item") or s.get("ui_held_item") or s.get("ui_held_stack",0)
           or (s.get("cursor_settle") or {}).get("cursor") for s in (proof["before"],proof["after"])):
        return None
    obj = E.EconomyObjective(kind,item["item"],None,item_id=item["item"],quality=item.get("quality"),slot=int(path[1]))
    obj.receiver_token=proof["before_receiver"]
    if E.note_generic_transfer(obj,proof)!="progress":
        return None
    seen.append(proof["request_id"])
    return dict(state="completed",kind=kind,item=obj.item_id,quality=obj.quality,slot=obj.slot,quantity=obj.done,
        inventory_delta=-obj.done,money_delta=proof["after"]["money"]-proof["before"]["money"],
        **({"shipping_delta":obj.done} if kind=="ship" else {}),request_id=proof["request_id"],
        source="generic_ui_native_readback")
