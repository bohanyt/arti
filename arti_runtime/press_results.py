"""Per-spot count of repeated identical answers to settled presses.

Facts only. Nothing here filters, ranks or alters any offer or input.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

SETTING, KEY, MAX_SPOTS, MIN_REPEATS, MAX_BYTES = "stardew_live7_press_results", "hasil_berulang", 24, 2, 200
PREFIXES = ("map-code:", "map-action:", "obj:", "xt:")
_STEPS = ((0, -1), (1, 0), (0, 1), (-1, 0))
_HEADS = dict(inventory="bag", money="money", location="place", quest="quest", mail="mail", letter="mail")


def enabled(config):
    return (config or {}).get(SETTING) is True


def _xy(v):
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(type(n) is int for n in v):
        return (v[0], v[1])
    return (v["x"], v["y"]) if isinstance(v, Mapping) and type(v.get("x")) is int and type(v.get("y")) is int else None


def is_press_call(call):
    arg = getattr(call, "arg", None)
    return getattr(call, "name", None) == "aksi" and (arg == "depan" or isinstance(arg, str) and arg.startswith(PREFIXES))


def _menu_info(state):
    delta = state.get("generic_readback") if isinstance(state, Mapping) else None
    dchanges = (delta.get("changes") or ()) if isinstance(delta, Mapping) else ()
    dmenu = next((c["after"]["value"] for c in dchanges if isinstance(c, Mapping) and c.get("path") == "menu"
                  and (c.get("before") or {}).get("value") is None and isinstance((c.get("after") or {}).get("value"), Mapping)
                  and c["after"]["value"].get("type")), None)
    ep = state.get("native_ui_episode") if isinstance(state, Mapping) else None
    ep_open = ep.get("opened") if isinstance(ep, Mapping) and isinstance(ep.get("opened"), Mapping) else None
    obs = [r for r in (ep.get("observations") or ()) if isinstance(r, Mapping)] if isinstance(ep, Mapping) else []
    linked = [r for r in obs if r.get("linked") is not False] or obs
    if dmenu is None and ep_open is None and not state.get("generic_menu_opened"):
        return None, None, ""
    unreadable = (dmenu is not None and dmenu.get("unreadable") is True) or any(r.get("unreadable") is True for r in linked)
    texts = [" ".join(r["text"].split()) for r in (*linked, dmenu) if isinstance(r, Mapping) and isinstance(r.get("text"), str) and r["text"].strip()]
    states = [s.strip() for r in (*linked, dmenu) if isinstance(r, Mapping) for s in (r.get("state"), r.get("state_digest")) if isinstance(s, str) and s.strip()]
    content = ("text", texts[0]) if texts else ("state", states[0]) if states else ""
    if unreadable:
        return "unreadable", False, content
    mtype = next((m for m in (linked[0].get("menu") if linked else None, ep_open.get("menu") if ep_open else None, dmenu.get("type") if dmenu else None) if isinstance(m, str) and m), "menu")
    return mtype, True, content


def _answer(state, before, after, location, items):
    """(digest, words) of an answer that changed nothing of hers; (None, None) when something did change."""
    mtype, readable, content = _menu_info(state)
    after_loc = location if location is not None else after.get("location")
    kinds = {k for k, on in (
        ("bag", bool(items) or ("inventory" in before and "inventory" in after and before["inventory"] != after["inventory"])),
        ("money", before.get("money") != after.get("money")),
        ("place", before.get("location") is not None and after_loc is not None and before.get("location") != after_loc
         or before.get("location_id") is not None and after.get("location_id") is not None and before.get("location_id") != after.get("location_id")),
        ("crops", before.get("crops") != after.get("crops")),
        ("quest", any(k in before and k in after and before[k] != after[k] for k in ("quests", "open_quests"))),
        ("mail", any(k in before and k in after and before[k] != after[k] for k in ("mail", "mailbox_count"))),
    ) if on}
    delta = state.get("generic_readback") if isinstance(state, Mapping) else None
    for cell in (delta.get("changes") or ()) if isinstance(delta, Mapping) else ():
        head = str(cell.get("path", "")).split(":", 1)[0] if isinstance(cell, Mapping) else ""
        if kind := _HEADS.get(head) or ("crops" if head.startswith("crop") else None):
            kinds.add(kind)
    if kinds:  # what changed may be another thing each time: never "always the same"
        return None, None
    raw = json.dumps(dict(menu=mtype, readable=readable, content=content), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    words = ("tidak ada yang berubah" if mtype is None else "menu yang teksnya tidak terbaca" if readable is False
             else "menu yang isinya sama" if content else "menu yang sama")
    return hashlib.sha256(raw).hexdigest()[:16], words


def note(store, run, *, category, before, after, location, items, date, clock, context=None):
    """Record one settled press per (location id, target tile). Unknown after-read or tile counts nothing."""
    call = getattr(run, "call", None)
    state = run.state if isinstance(getattr(run, "state", None), Mapping) else {}
    if (not is_press_call(call) or not isinstance(before, Mapping) or not isinstance(after, Mapping) or category not in ("VERIFIED", "CLEAN_FAIL")
            or not (isinstance(state.get("generic_readback"), Mapping) or state.get("generic_menu_opened") or isinstance(state.get("native_ui_episode"), Mapping))):
        return None
    loc = before.get("location_id") or before.get("location") or ((context or {}).get("location_id") if isinstance(context, Mapping) else None) or location
    pending = state.get("generic_pending_args")
    tile = ((pending["tile_x"], pending["tile_y"]) if isinstance(pending, Mapping) and type(pending.get("tile_x")) is int and type(pending.get("tile_y")) is int
            else _xy(getattr(call, "tile", None)) if getattr(call, "arg", None) != "depan" else None)
    if not isinstance(loc, str) or not loc or tile is None:
        return None
    digest, summary = _answer(state, before, after, location, items)
    key = f"{loc}:{tile[0]},{tile[1]}"
    prev = store.pop(key, None)
    if digest is None:
        return (loc, tile)
    same = isinstance(prev, Mapping) and prev.get("digest") == digest
    day_label = f"Y{date[0]} {date[1]} {date[2]}" if isinstance(date, (list, tuple)) and len(date) == 3 else (date.strip()[:40] if isinstance(date, str) else "")
    store[key] = dict(location=loc, tile=[tile[0], tile[1]], digest=digest, summary=summary, count=prev["count"] + 1 if same else 1,
                      since_day=prev["since_day"] if same else day_label,
                      since_clock=prev["since_clock"] if same else (clock if type(clock) is int and 0 <= clock <= 2600 else None))
    while len(store) > MAX_SPOTS:
        store.pop(next(iter(store)))
    return (loc, tile)


def tag_no_effect(no_effect, call, spot):
    name = call.name + (" " + str(call.arg) if call.arg not in (None, "") else "")
    if isinstance(no_effect.get(name), dict):
        no_effect[name]["spot"] = spot


def page_fact(store, *, facts=None, context=None, snapshot=None, calls=()):
    """World-page fact for the faced/stood offered spot plus at most one highest-count spot here (<=200 B)."""
    if not store or getattr(facts, "menu_open", False):
        return None, set()
    loc = ((context or {}).get("location_id") if isinstance(context, Mapping) else None) or getattr(facts, "location", None)
    rows = [(i, k, r) for i, (k, r) in enumerate(store.items()) if isinstance(loc, str) and loc and isinstance(r, Mapping)
            and r.get("location") == loc and type(r.get("count")) is int and r["count"] >= MIN_REPEATS and _xy(r.get("tile")) is not None]
    if not rows:
        return None, set()
    import arti_stardew_skills as S
    from arti_stardew_generic_world import parse_targets
    targets = S._g(snapshot, "generic_world_targets", ()) or parse_targets((context or {}).get("mines_ui"))
    grab = next(((t["tile_x"], t["tile_y"]) for t in targets or () if isinstance(t, Mapping) and t.get("kind") == "generic_action"
                 and type(t.get("tile_x")) is int and type(t.get("tile_y")) is int), None)
    offered = {t for c in calls or () if is_press_call(c) and c.arg != "depan" if (t := _xy(getattr(c, "tile", None))) is not None}
    if grab is not None and any(getattr(c, "name", None) == "aksi" and getattr(c, "arg", None) == "depan" for c in calls or ()):
        offered.add(grab)
    motor = S._g(snapshot, "motor")
    stand, facing = _xy(S._g(motor, "tile")) or _xy((context or {}).get("actor_tile") if isinstance(context, Mapping) else None), S._g(motor, "facing")
    faced = (stand[0] + _STEPS[facing][0], stand[1] + _STEPS[facing][1]) if stand is not None and type(facing) is int and 0 <= facing < 4 else None
    here = list(dict.fromkeys(t for t in (grab, faced, stand) if t is not None))
    hits = [(here.index(tuple(r["tile"])), -r["count"], -i, k, r) for i, k, r in rows if tuple(r["tile"]) in offered and tuple(r["tile"]) in here]
    first = min(hits)[3:] if hits else None
    rest = [(-r["count"], -i, k, r) for i, k, r in rows if first is None or k != first[0]]
    chosen = [p[1] for p in (first, min(rest)[2:] if rest else None) if p is not None]
    parts = [f"petak {r['tile'][0]},{r['tile'][1]}: ditekan {r['count']}x sejak {' '.join(p for p in (r.get('since_day') or '', '%02d:%02d' % divmod(r['since_clock'], 100) if type(r.get('since_clock')) is int else '') if p) or '?'}, jawabannya selalu sama: {r['summary']}." for r in chosen]
    if len(parts) == 2 and len((parts[0] + " " + parts[1]).encode("utf-8")) <= MAX_BYTES:
        return parts[0] + " " + parts[1], {(r["location"], tuple(r["tile"])) for r in chosen}
    raw = parts[0].encode("utf-8")
    return (parts[0] if len(raw) <= MAX_BYTES else raw[:MAX_BYTES].decode("utf-8", errors="ignore").rstrip()), {(chosen[0]["location"], tuple(chosen[0]["tile"]))}


def load(raw):
    """A resume value is trusted only as far as its shape; at most MAX_SPOTS spots, oldest evicted."""
    out = {}
    for row in (raw.values() if isinstance(raw, Mapping) else ()):
        if not isinstance(row, Mapping):
            continue
        loc, tile = row.get("location"), _xy(row.get("tile"))
        digest, summary, count, sday, sclk = (row.get(k) for k in ("digest", "summary", "count", "since_day", "since_clock"))
        if (isinstance(loc, str) and 0 < len(loc) <= 120 and tile is not None and isinstance(digest, str) and 0 < len(digest) <= 64
                and isinstance(summary, str) and 0 < len(summary) <= 120 and type(count) is int and count >= 1
                and isinstance(sday, str) and len(sday) <= 40 and (sclk is None or type(sclk) is int and 0 <= sclk <= 2600)):
            key = f"{loc}:{tile[0]},{tile[1]}"
            out.pop(key, None)
            out[key] = dict(location=loc, tile=[tile[0], tile[1]], digest=digest, summary=summary, count=count, since_day=sday, since_clock=sclk)
            while len(out) > MAX_SPOTS:
                out.pop(next(iter(out)))
    return out


def export(store):
    return {k: dict(v, tile=list(v["tile"])) for k, v in (store or {}).items() if isinstance(v, Mapping)}
