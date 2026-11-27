"""ADR 219: exact native presentations, existing ledger and shared seen receipts.

Capture is not delivery. Pending offsets advance only after an answered request.
Text stays in ledger facts (record_items' compact top-level text is not used).
"""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from functools import wraps

from arti_runtime.native_story import day_of
from arti_runtime.native_rows import native_rows
from arti_runtime.seen_receipts import SeenReceipts

SETTING = "stardew_game_text_delivery_enabled"
BLOCK = "yang baru muncul di game"
KINDS = {"letter_opened", "event_watched", "calendar_day_read", "door_message",
         "hud_notice", "mailbox_visible", "mailbox_empty_visible", "dialogue_read",  # the box seen with no flag
         "shipping_summary_seen",  # the night earnings screen as the game wrote it
         "quest_entry_read", "bundle_note_opened"}  # the journal entry or bundle page that was open


def _locked(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        import arti_mind as M
        with M._JOURNEY_LOCK, M._LEDGER_LOCK:
            return fn(*args, **kwargs)
    return call


def _rows(base, scope):
    """``arti_mind.ledger_rows(base, "stardew", scope=scope, kind="game_text")``'s rows, from one parsed snapshot of
    the ledger per file revision (ADR 231 part C). The rows are shared snapshots: a writer copies before changing."""
    import arti_mind as M
    from arti_runtime import jsonl_snapshot
    try:
        scope_hash = M._scope_hash(scope)
        rows = jsonl_snapshot.rows(M.game_dir(base, "stardew") / M.LEDGER_FILE)
    except (OSError, ValueError):
        return []
    return [r for r in rows if r.get("scope_hash") == scope_hash and r.get("kind") == "game_text"
            and isinstance(r,Mapping) and isinstance(r.get("facts"),Mapping) and r["facts"]]


def _save(base, scope, key, facts):
    import arti_mind as M
    M.record_items(base, "stardew", scope=scope, kind="game_text", day=facts["day"],
                   day_label=facts["date"], items=[dict(id=key, status="captured", facts=facts)])


@_locked
def capture(base, record):
    """Only an accepted, selected farmhand's authoritative current ui.text record.

    Runtime does actor/host selection before calling. Binding and replay checks are
    repeated here, so a persisted text pointer cannot cross save/actor boundaries.
    """
    if (base is None or record is None or record.domain != "ui.text" or not record.authoritative
            or not record.available or record.stale or record.requires_resync or not isinstance(record.facts,Mapping)):
        return None
    binding = record.facts.get("binding") or {}
    if (binding.get("player_id"), binding.get("screen_id")) != (record.player_id, record.screen_id):
        return None
    accepted_scope = None
    known, dirty = {}, {}
    occurrences = [(name, occ) for name, occ in record.facts.items()
                   if str(name).startswith("occ:") and isinstance(occ, Mapping)]
    # Native snapshots place active rows before history. That wire order is not dialogue order.
    occurrences.sort(key=lambda pair: pair[1].get("occurrence_seq", 0)
                     if type(pair[1].get("occurrence_seq")) is int else 0)
    for name, occ in occurrences:
        if not str(name).startswith("occ:") or not isinstance(occ, Mapping) or occ.get("replayed") is not False:
            continue
        p = occ.get("presented")
        if not isinstance(p, Mapping) or p.get("kind") not in KINDS or p.get("v") != 1:
            continue
        ticks, last = binding.get("capture_ticks"), occ.get("last_capture_ticks")
        if type(ticks) is not int or type(last) is not int or not 0 <= ticks - last <= 180 * 60:
            continue
        day = day_of(p.get("date"))
        if day is None or not p.get("save_id") or not p.get("id") or not p.get("key"):
            continue
        part, total, text = p.get("part"), p.get("parts"), occ.get("text")
        if (type(part) is not int or type(total) is not int or not 0 <= part < total <= 4096
                or not isinstance(text, str) or len(text) > 2048):
            continue
        scope = f"{p['save_id']}:{record.player_id}"
        accepted_scope = scope
        key = hashlib.sha256((p["key"] + "\0" + str(p.get("presentation", ""))).encode()).hexdigest()
        if scope not in known:
            known[scope] = {r["item_id"]: r["facts"] for r in _rows(base, scope)}
        old = known[scope].get(key)
        facts = deepcopy(old) if old else dict(p, day=day, source=occ.get("source"),
            speaker=occ.get("speaker"), chunks={}, delivered=0, capture_complete=True,
            native_seq=occ.get("occurrence_seq", 0), capture_ticks=occ.get("first_capture_ticks", last),
            native_ref=f"ui.text:{record.session_id}:{occ.get('occurrence_seq')}")
        facts["chunks"] = dict(facts.get("chunks") or {})
        facts["chunks"][str(part)] = text
        facts["capture_complete"] = facts.get("capture_complete") is True and occ.get("complete") is True
        if facts != old:
            known[scope][key] = facts
            dirty[(scope, key)] = facts
    for (scope, key), facts in dirty.items():
        _save(base, scope, key, facts)
    return accepted_scope


def _text(facts):
    chunks=facts.get("chunks") or {}
    parts=facts.get("parts")
    return "".join(chunks.get(str(i), "") for i in range(parts) if isinstance(chunks.get(str(i)),str)) if type(parts) is int and parts>0 else ""


@_locked
def texts(base, scope):
    """ADR 231 part C: item id -> row, from one ledger snapshot, for a caller that reads many pointers."""
    return {r["item_id"]: r for r in _rows(base, scope)}


@_locked
def read_text(base, scope, text_ref, *, index=None):
    """Read the exact dated text she received, by receipt pointer; never reinterpret it.

    ``index``: ``texts(base, scope)`` already read by the caller (ADR 231 part C: attempt 87 looked each pointer up
    with one full ledger read, about 855 reads per page)."""
    key = text_ref.removeprefix("game_text:")
    row = (index or {}).get(key) if index is not None else next((r for r in _rows(base, scope) if r["item_id"] == key), None)
    if row is None:
        return None
    f = row["facts"]
    if (not isinstance(f.get("chunks"),Mapping) or not f["chunks"] or type(f.get("parts")) is not int
            or any(not isinstance(v,str) for v in f["chunks"].values())):return None
    return dict(kind=f["kind"], id=f["id"], game_day=f["day"], date=f["date"],
                clock=f["clock"], place=f["place"], source=f["source"], speaker=f.get("speaker"),
                text=_text(f)[:f["delivered"]],
                complete=f.get("answered", False) and f["capture_complete"]
                    and f["delivered"] == len(_text(f)) and len(f["chunks"]) == f["parts"]
                    and hashlib.sha256(_text(f).encode()).hexdigest() == f["hash"])


@_locked
def prepare(base, scope, *, ceiling=1800, text_chars=900):
    """One short block; a long page resumes at its exact next character, without losing choices.

    ADR 231 part B2 (attempt 87: the 09:00 notice reached her at 12:10, behind the 07:30 mailbox signal and the
    08:20 door message, one text per answered page): after the oldest pending text, every other pending text that
    fits whole rides the same block, oldest first, so a captured text is on the first page built after its capture.
    The oldest text's own slice gives way first (it resumes at its exact next character); a row is never cut."""
    if type(text_chars) is not int or text_chars < 1:
        raise ValueError("text_chars")
    if base is None or not scope:
        return None
    store = SeenReceipts(base, scope)
    pending = sorted([r for r in _rows(base,scope) if isinstance(r["facts"].get("chunks"),Mapping)
        and r["facts"]["chunks"] and all(isinstance(v,str) for v in r["facts"]["chunks"].values())
        and type(r["facts"].get("parts")) is int
        and type(r["facts"].get("day")) is int and type(r["facts"].get("clock")) is int], key=lambda r: (r["facts"]["day"], r["facts"]["clock"],
                     r["facts"].get("capture_ticks", 0), r["facts"].get("native_seq", 0)))
    views = [v for v in (_pending_view(row, store) for row in pending) if v is not None]
    for i, first in enumerate(views):
        found = _block(scope, pending, first, views[i + 1:], ceiling, text_chars)
        if found is not None or first["missing_prefix"]:
            return found
    return None


def _block(scope, pending, first, rest, ceiling, text_chars):
    row, f, line, text, contiguous = first["row"], first["facts"], first["line"], first["text"], first["contiguous"]
    if first["missing_prefix"]:
        return dict(scope=scope, key=row["item_id"], start=0, end=0, gap=True,
                    block=dict(rows=[dict(line, text="")], sisa_teks_di_game_belum_terbaca=True,
                               sisa_teks_tersimpan_dan_terbaca=len(_text(f)), bagian_belum_diterima=f["parts"] - len(f["chunks"])))
    # Whole later texts (and empty signals) that can ride with it; a gap or a long remainder waits its own page.
    whole = [v for v in rest if not v["missing_prefix"] and (v["empty_signal"] or v["complete"]
                                                            and len(v["text"]) - v["start"] <= text_chars)]
    def block_for(rows, extra):
        more = [dict(v["line"], text=v["text"][v["start"]:]) for v in extra]
        taken = {v["row"]["item_id"] for v in extra}
        if first["empty_signal"]:
            return dict(rows=rows + more, sisa_teks_tersimpan_dan_terbaca=0, bagian_belum_diterima=0)
        return dict(rows=rows + more,
                    sisa_teks_tersimpan_dan_terbaca=len(text) - start - size,
                    bagian_belum_diterima=f["parts"] - contiguous,
                    sisa_teks_di_game_belum_terbaca=not f["capture_complete"] or contiguous < f["parts"],
                    halaman_teks_lain_tersimpan=sum(1 for r in pending if r["item_id"] != row["item_id"]
                        and r["item_id"] not in taken and r["facts"]["delivered"] < len(_text(r["facts"]))))
    def fits(block):
        return len(json.dumps({BLOCK: block}, ensure_ascii=True).encode()) <= ceiling
    def batch(block, extra, start_, end_):
        out = dict(scope=scope, key=row["item_id"], start=start_, end=end_, block=block)
        if extra:
            out["more"] = [dict(key=v["row"]["item_id"], start=v["start"], end=len(v["text"]),
                                **({"empty": True} if v["empty_signal"] else {})) for v in extra]
        return out
    start = f["delivered"]
    for keep in range(len(whole), -1, -1):
        extra = whole[:keep]
        if first["empty_signal"]:
            size = 0
            block = block_for([dict(line, text="")], extra)
            if fits(block) or not extra:  # an empty signal alone is always its own page (unchanged)
                return batch(block, extra, 0, 0)
            continue
        # JSON-escaped bytes, rather than character counts, are the real page cost.
        size = min(len(text) - start, text_chars)
        while size:
            block = block_for([dict(line, text=text[start:start + size])], extra)
            if fits(block):
                return batch(block, extra, start, start + size)
            size //= 2
    return None


def _pending_view(row, store):
    """The undelivered part of one captured text, or None when nothing of it waits (existing ADR 219 rules)."""
    f = row["facts"]
    # Never concatenate across a missing piece. Protocol loss remains visible.
    contiguous = 0
    while str(contiguous) in f["chunks"]:
        contiguous += 1
    text = "".join(f["chunks"][str(i)] for i in range(contiguous))
    start = f["delivered"]
    empty_signal = len(text) == 0 and f["parts"] == 1 and "0" in f["chunks"] and not f.get("answered")
    missing_prefix = contiguous == 0 and not f.get("gap_announced")
    if start >= len(text) and not empty_signal and not missing_prefix:
        return None
    line = {k: f.get(k) for k in ("kind", "id", "date", "clock", "place", "source", "speaker")}
    line.update(first=store.seen("text_read", f["key"]) is None, text_ref="game_text:" + row["item_id"])
    if empty_signal:
        line["first"] = store.seen(f["kind"], f["id"]) is None
    return dict(row=row, facts=f, line=line, text=text, start=start, contiguous=contiguous,
                empty_signal=empty_signal, missing_prefix=missing_prefix,
                complete=f["capture_complete"] is True and contiguous == f["parts"])


def inject(messages, batch):
    if not batch:
        return messages
    result = deepcopy(messages)
    body = json.loads(result[1]["content"])
    body[BLOCK] = batch["block"]
    mind = body.get('mind')
    ordered_plan = isinstance(mind, Mapping) and 'rencana_hari_ini' in mind
    if ordered_plan:
        # Keep the authored plan first inside mind after native text rides.
        body[BLOCK] = json.loads(json.dumps(body[BLOCK], sort_keys=True))
        body = dict(sorted(body.items()))
    result[1]["content"] = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=not ordered_plan)
    return result


@_locked
def answered(base, batch, messages):
    """No receipt for preflight, trim, failed provider calls, or a block absent from the answered page."""
    if not batch or base is None or json.loads(messages[1]["content"]).get(BLOCK) != batch["block"]:
        return []
    out = _settle(base, batch["scope"], batch["key"], batch.get("end"), gap=batch.get("gap"))
    for extra in batch.get("more") or ():  # ADR 231 part B2: the whole texts that rode the same answered page
        out += _settle(base, batch["scope"], extra["key"], extra["end"])
    return out


def _settle(base, scope, key, end, *, gap=False):
    row = next((r for r in _rows(base, scope) if r["item_id"] == key), None)
    if row is None:
        return []
    f = deepcopy(row["facts"])  # the snapshot row is shared (ADR 231 part C); the saved copy is the new row
    if not isinstance(f.get("chunks"),Mapping) or not f["chunks"] or type(f.get("parts")) is not int:return []
    f["answered"] = True
    if gap:
        f["gap_announced"] = True
    f["delivered"] = max(f["delivered"], end if type(end) is int else 0)
    _save(base, scope, key, f)
    if f["delivered"] < len(_text(f)) or len(f["chunks"]) < f["parts"] or not f["capture_complete"]:
        return []
    if hashlib.sha256(_text(f).encode()).hexdigest() != f["hash"]:
        return []
    store = SeenReceipts(base, scope)
    fields = dict(game_day=f["day"], clock=f["clock"], place=f["place"], source=f["source"],
                  text_ref="game_text:" + key)
    # Native identity for the other workers, content/page identity for first-time text.
    return [store.note(f["kind"], f["id"], **fields), store.note("text_read", f["key"], **fields)]


def attach(runtime, offer):
    if (runtime._config.get(SETTING) is True and (base := runtime._live7_mind_base()) is not None
            and (scope := getattr(runtime, "_game_text_scope", None))):
        state = getattr(runtime, "_state", None)
        if state is not None and scope.rsplit(":", 1)[-1] != str(state.pilih_target()):
            return
        current_scope = getattr(runtime, "_live7_native_story_scope", None)
        if current_scope and current_scope != scope:
            return
        from arti_runtime.page_budget import PageBudget
        budget = (getattr(getattr(runtime, "_skill_session", None), "page_budget", None)
                  or PageBudget.from_config(runtime._config))
        batch = (prepare(base, scope) if budget.scale == 1 else
                 prepare(base, scope, ceiling=1800 * budget.scale, text_chars=900 * budget.scale))
        if batch:
            offer["_game_text"] = batch


def accept_answer(runtime, request):
    batch = (request.skill_offer or {}).get("_game_text")
    if runtime._config.get(SETTING) is True and batch:
        messages = runtime._provider._render_messages(request)
        for receipt in answered(runtime._live7_mind_base(), batch, messages):
            runtime._journal(dict(ev="game_text_seen", receipt=receipt))
