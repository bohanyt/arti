"""Bounded goods selection; all effects still use the existing native skill/verifier."""
from dataclasses import replace

from arti_stardew_economy import hard_dispose_refusal

MAX_STACKS = 8


def batch_calls(selected, offered, rows, gifts):
    """Freeze at most eight chosen observed stacks; only tools and quest items are denied."""
    names = selected.batch or (selected.arg,)
    implicit = names == ("semua",)
    frozen_store = selected.name == 'simpan_ke_chest' and not selected.batch and type(selected.n) is int
    out = []
    for row in rows:
        qid = row.get("qualified_item_id")
        arg = qid if selected.name == "simpan_ke_chest" else row.get("name")
        if not implicit and arg not in names:
            continue
        if hard_dispose_refusal(row):
            continue
        slot, count, quality = row.get("slot"), row.get("stack"), row.get("quality")
        if frozen_store and quality != selected.quality:
            continue
        if (type(slot) is not int or slot < 0 or type(count) is not int or not 0 < count <= 999
                or type(quality) is not int or quality not in (0, 1, 2, 4)):
            continue
        call = next((c for c in offered if c.name == selected.name and c.arg == arg
                     and (c.name != "simpan_ke_chest" or c.quality == quality)), None)
        if call is None or any(c.slot == slot for c in out):
            continue
        out.append(replace(call, n=count, quality=quality, slot=slot, batch=()))
    # A misspelled/missing explicit name never authorizes a partial substitute.
    if not implicit and any(name not in {c.arg for c in out} for name in names):
        return []
    if frozen_store and (sum(c.n for c in out) != selected.n or len(out)>MAX_STACKS):
        return []  # a stale/changed quantity never expands the chosen deposit
    return out[:MAX_STACKS]


def stack_current(call, rows, *, implicit=False, gifts=()):
    return any(r.get("slot") == call.slot and r.get("quality") == call.quality
               and r.get("stack") == call.n
               and (r.get("qualified_item_id") if call.name == "simpan_ke_chest" else r.get("name")) == call.arg
               and hard_dispose_refusal(r) is None
               for r in rows)
