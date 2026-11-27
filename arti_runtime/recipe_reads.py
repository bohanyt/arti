"""Remember known recipes really shown on an answered crafting page."""
from collections.abc import Mapping; import contextlib, hashlib, json, re
SOURCE, UNKNOWN_STATE, MAX_LINES, MAX_LINE_CHARS, TEXT_MAX = "crafting page", "recipe_not_known", 8, 120, 512
def native_id(arg): return "Chest" if isinstance(arg, str) and arg.casefold() == "peti" else str(arg)
def _needs_text(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > TEXT_MAX: return None
    parts = [re.fullmatch(r"\s*(.+?)\s+\d+/(\d+)\s*", p) for p in raw.split(",")]
    return ", ".join("%s %s" % (" ".join(m[1].split()), m[2]) for m in parts) if all(m and int(m[2]) >= 1 for m in parts) else None
def parse(view):
    menu, c = getattr(view, "menu_type", None), getattr(view, "crafting", None)
    if getattr(view, "available", True) is False or (menu != "CraftingPage" and not (menu == "GameMenu" and getattr(view, "page_type", None) == "CraftingPage")) or not isinstance(c, Mapping) or c.get("cooking") is not False or not isinstance(c.get("recipes"), (list, tuple)): return ()
    descs, out, seen_ids, seen_names = (c.get("descriptions") if isinstance(c.get("descriptions"), Mapping) else {}), [], set(), set()
    for r in c["recipes"]:
        if not isinstance(r, (list, tuple)) or len(r) < 7 or type(r[0]) is not int or type(r[3]) is not int or r[3] < 1 or not r[4] or not all(isinstance(v, str) for v in (r[1], r[2], r[4], r[5], r[6])): return ()
        if r[4] == UNKNOWN_STATE: continue
        disp, nat, needs, raw_desc = " ".join(r[1].split()), r[6].strip(), _needs_text(r[5]), (r[7] if len(r) >= 8 and r[7] is not None else descs.get(r[6].strip()))
        if not disp or not nat or len(disp) > 128 or len(nat) > 128 or "\n" in r[6] or "\r" in r[6] or not needs or r[0] in seen_ids or nat in seen_names or (raw_desc is not None and (not isinstance(raw_desc, str) or len(raw_desc) > TEXT_MAX)): return ()
        head = "%s x%d: %s" % (disp, r[3], needs) if r[3] > 1 else "%s: %s" % (disp, needs)
        out.append((r[0], nat, "%s — %s" % (head, " ".join(raw_desc.split())) if raw_desc and raw_desc.strip() else head)); seen_ids.add(r[0]); seen_names.add(nat)
    return tuple(out)
def _unpack_groups(groups):
    if isinstance(groups, list): return [g for g in groups if isinstance(g, Mapping)]
    if not (isinstance(groups, Mapping) and isinstance(groups.get("cols"), list) and isinstance(groups.get("rows"), list)): return []
    cols, const, same, out = groups["cols"], groups.get("all_rows") or {}, groups.get("same_as") or {}, []
    for r in groups["rows"]:
        if isinstance(r, list) and len(r) >= len(cols):
            d = dict(const, **dict(zip(cols, r)), **(r[-1] if len(r) > len(cols) and isinstance(r[-1], Mapping) else {}))
            d.update({c: d[src] for c, src in same.items() if src in d}); out.append(d)
    return out
def shown_ids(messages, view):
    try: body = json.loads(messages[1]["content"])
    except (TypeError, ValueError, KeyError, IndexError): return set()
    ui, rcv = body.get("unknown_ui") if isinstance(body, Mapping) else None, getattr(view, "receiver_token", None)
    if not isinstance(ui, Mapping) or not isinstance(rcv, str) or not rcv or ui.get("receiver") != rcv or ("state_digest" in ui and getattr(view, "state_digest", None) is not None and ui.get("state_digest") != view.state_digest): return set()
    shared, ids = (body.get("shared_text") if isinstance(body.get("shared_text"), Mapping) else {}), set()
    for src in [ui.get("groups")] + ([ui["overview"].get("groups")] if isinstance(ui.get("overview"), Mapping) else []):
        for g in _unpack_groups(src):
            fields = g.get("fields")
            if g.get("kind") != "recipe" or not isinstance(fields, list) or "id" not in fields or "label" not in fields: continue
            id_i, lbl_i = fields.index("id"), fields.index("label")
            for row in g.get("rows") or ():
                if isinstance(row, list) and len(row) > max(id_i, lbl_i) and type(row[id_i]) is int and isinstance(row[lbl_i], str):
                    lbl = shared.get(row[lbl_i][1:], row[lbl_i]) if row[lbl_i].startswith("@t") else row[lbl_i]
                    if isinstance(lbl, str) and lbl.strip(): ids.add(row[id_i])
    return ids
def answered(base, scope, view, messages, *, day, clock, place, mind_scoped=False):
    if base is None or not scope or type(day) is not int or day < 1 or not (rows := parse(view)) or not (ids := shown_ids(messages, view)) or not (shown := [(nat, text) for cid, nat, text in rows if cid in ids]): return []
    import arti_mind as M; from arti_runtime import ipc, mind_scope; from arti_runtime.plan_dates import label; from arti_runtime.seen_receipts import SeenReceipts, _record
    date, store = label("stardew", day), SeenReceipts(base, scope)
    with M._JOURNEY_LOCK, (mind_scope.file_lock(store.path) if mind_scoped else contextlib.nullcontext()):
        existing = store._rows(); known_ids, by_recipe, lines, written = {r.get("id") for r in existing if isinstance(r, Mapping)}, store.index(existing), [], []
        for native, text in shown:
            tref = hashlib.sha256((scope + "\0crafting_page:%s:%s" % (native, hashlib.sha256(text.encode()).hexdigest()[:16])).encode()).hexdigest()
            prior = by_recipe.get(("recipe_seen", native), [])
            if any(r.get("text_ref") == tref for r in prior): continue
            if tref not in known_ids: lines.append(json.dumps(dict(id=tref, kind="text_read", subject=native, text=text, source=SOURCE, date=date, observed_day=day, day=day, day_label=date, scope_hash=store.scope_hash, native_seq=0, event_time=date, event_place=place), ensure_ascii=False) + "\n"); known_ids.add(tref)
            row = _record("recipe_seen", native, game_day=day, clock=clock, place=place, source=SOURCE, text_ref=tref); row["first"] = not prior
            ev = "seen_receipt:recipe_seen:%s:%s" % (native, hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
            lines.append(json.dumps(dict(id=hashlib.sha256((scope + "\0" + ev).encode()).hexdigest(), kind="seen_receipt", subject=native, scope_hash=store.scope_hash, source=SOURCE, date=date, day=day, receipt=row), ensure_ascii=False) + "\n")
            by_recipe[("recipe_seen", native)] = list(prior) + [row]; written.append(row)
        if lines: ipc.append_text(store.path, "".join(lines))
    return written
def accept_answer(runtime, request):
    """After an answered decision: a note that cannot be written is journaled and never costs her the decision."""
    try: return _accept_answer(runtime, request)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        if callable(getattr(runtime, "_journal", None)): runtime._journal(dict(ev="live7_recipe_seen_error", error=type(exc).__name__))
        return []


def _accept_answer(runtime, request):
    cfg = getattr(runtime, "_config", None) or {}
    view, render = getattr(runtime, "_live7_ui_seen_view", None), getattr(getattr(runtime, "_provider", None), "_render_messages", None)
    if cfg.get("live7_fact_line_enabled") is not True or "unknown_ui" not in (getattr(request, "skill_offer", None) or {}) or view is None or not callable(render) or not parse(view): return []
    from arti_runtime.plan_dates import ordinal
    motor = runtime._state.latest(pid) if (pid := runtime._binding_view()[1]) is not None else None
    absolute, base, scope = (ordinal("stardew", "Y%s %s %s" % tuple(getattr(motor, k, None) for k in ("year", "season", "day"))) if motor else None), runtime._live7_mind_base(), getattr(runtime, "_live7_discovery_scope", None)
    if base is None or not scope or not absolute: return []
    written = answered(base, scope, view, render(request), day=absolute, clock=getattr(motor, "time_of_day", None), place=getattr(motor, "location", None), mind_scoped=cfg.get("live7_mind_scope", False) is True)
    if written and callable(getattr(runtime, "_journal", None)): runtime._journal(dict(ev="live7_recipe_seen", count=len(written), recipes=[r["id"] for r in written], receiver=view.receiver_token))
    return written
def _format_line(w, row): body, suffix = " ".join(str(w.text(row) or "").split()), " (%s)" % w.provenance(row); return body + suffix if len(body) + len(suffix) <= MAX_LINE_CHARS else ((body[:MAX_LINE_CHARS - len(suffix) - 1].rstrip() + "…" + suffix) if MAX_LINE_CHARS - len(suffix) >= 12 else (body + suffix)[:MAX_LINE_CHARS - 1].rstrip() + "…")
def _latest_by_id(w):
    by_id = {}
    for row in w.matching("recipe_seen"):
        if isinstance(rid := row.get("id"), str) and rid: by_id.pop(rid, None); by_id[rid] = row
    return by_id
def dibaca_lines(w): return [_format_line(w, r) for r in list(_latest_by_id(w).values())[-MAX_LINES:]]
def buat_lines(w, offered_args):
    by_id = _latest_by_id(w); order = {rid: i for i, rid in enumerate(by_id)}
    hits = [(a, r) for a in offered_args if (r := by_id.get(native_id(a)) or by_id.get(a)) is not None]
    if len(hits) > MAX_LINES: keep = {a for a, r in sorted(hits, key=lambda p: order[p[1]["id"]])[-MAX_LINES:]}; hits = [(a, r) for a, r in hits if a in keep]
    return {a: _format_line(w, r) for a, r in hits}
