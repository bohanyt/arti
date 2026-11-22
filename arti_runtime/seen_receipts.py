"""Scoped native receipts in the existing first-seen ledger.

``SeenReceipts(base, scope)`` supplies the four-method API. Only ``note`` writes.
No text is invented here; text_ref points to the native text's existing owner.
Save backfill records when the save was inspected, not an invented historic date.
"""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
from arti_runtime.native_rows import native_rows
from arti_runtime.page_snapshot import plain_snapshot


def _day(value):
    from arti_runtime.plan_dates import label, ordinal
    absolute = value.get("absolute") if isinstance(value, Mapping) else value
    if isinstance(absolute, str):
        absolute = ordinal("stardew", absolute)
    if type(absolute) is not int or absolute < 1:
        raise ValueError("game_day must contain a positive project absolute day")
    return dict(absolute=absolute, text=label("stardew", absolute))


def _record(kind, identity, *, game_day, clock, place, source, text_ref=None):
    if not all(isinstance(v, str) and v for v in (kind, identity, source)):
        raise ValueError("kind, id and source must be nonempty native identifiers")
    row = dict(v=1, kind=kind, id=identity, game_day=_day(game_day),
               clock=clock, place=place, source=source, first=True)
    if text_ref is not None:
        row["text_ref"] = text_ref
    return row


def backfill_from_save(native_state):
    """Pure projection of explicit personal save flags; absent flags prove nothing.

    Reader contract: game_day, clock, place, source and optional places_visited,
    recipes_known, mail_read, shop_menus_opened. Lists contain native IDs; mappings
    contain native IDs with true/count-positive flags. Never use mail_received or
    world graph reachability. A known recipe proves its identity, not ingredients.
    """
    if not isinstance(native_state, Mapping):
        return []
    fields = (("places_visited", "place_entered"), ("recipes_known", "recipe_seen"),
              ("mail_read", "letter_opened"), ("shop_menus_opened", "shop_menu_seen"))
    rows = []
    for field, kind in fields:
        values = native_state.get(field) or ()
        if isinstance(values, Mapping):
            values = [key for key, flag in values.items()
                      if flag is True or type(flag) is int and flag >= (0 if field == "recipes_known" else 1)]
        for identity in sorted(set(v for v in native_rows(values) if isinstance(v, str) and v)):
            row = _record(kind, identity, game_day=native_state["game_day"],
                          clock=native_state.get("clock"), place=native_state.get("place"),
                          source=native_state["source"] + ":" + field)
            # Flags cannot prove the prose, ingredient rows, prices or exact visit day.
            row["backfilled"] = True
            row["identity_only"] = True
            rows.append(row)
    return rows


class SeenReceipts:
    """One save+actor view over experience_facts.with_memory's discoveries.jsonl.

    Unknown kinds are deliberately accepted. The enclosing ledger row uses the
    existing scope hash and identity convention; ``receipt`` holds the additive
    native-ID record, leaving legacy discovery consumers unchanged.
    """
    def __init__(self, base, scope, *, game="stardew"):
        if not isinstance(scope, str) or not scope:
            raise ValueError("a native save+actor scope is required")
        import arti_mind
        self.path = arti_mind.game_dir(base, game) / "discoveries.jsonl"
        self.scope = scope
        self.scope_hash = hashlib.sha256(scope.encode()).hexdigest()

    def _rows(self):
        from arti_runtime import jsonl_snapshot  # ADR 231 part C: parsed once per file revision, appends parsed alone
        return jsonl_snapshot.rows(self.path)

    def receipts(self, kind=None, since_day=None, *, id=None, rows=None):
        """Read-only deterministic projection; callers receive independent copies.

        ``rows`` is a ledger snapshot the caller already read (fix129A); kind/id
        select before copying, so unselected receipts are not copied.
        """
        rows = [plain_snapshot(r["receipt"],path="receipts.receipt") for r in native_rows(self._rows() if rows is None else rows)
                if isinstance(r,Mapping) and r.get("scope_hash") == self.scope_hash and isinstance(r.get("receipt"), Mapping) and r["receipt"].get("kind") and r["receipt"].get("id")
                and (kind is None or r["receipt"]["kind"] == kind) and (id is None or r["receipt"]["id"] == id)]
        return [r for r in rows if since_day is None or type((r.get("game_day") or {}).get("absolute")) is int and r["game_day"]["absolute"] >= since_day]

    def seen(self, kind, id):
        return next((r for r in reversed(self.receipts(kind)) if r["id"] == id), None)

    def index(self, rows=None):
        """Read-only (kind, id) -> receipts in file order, from one ledger snapshot.

        fix129A: a caller noting many receipts reads the file once. Every writer
        appends, so an exact repeat in this snapshot remains one; ``note`` still
        owns every write, its lock and its re-read.
        """
        found = {}
        for old in native_rows(self._rows() if rows is None else rows):
            receipt = old.get("receipt") if isinstance(old, Mapping) else None
            if isinstance(receipt, Mapping) and old.get("scope_hash") == self.scope_hash:
                found.setdefault((receipt.get("kind"), receipt.get("id")), []).append(receipt)
        return found

    @staticmethod
    def repeat(found, kind, id, *, game_day, clock, place, source, text_ref=None):
        """The receipt ``note`` would return for an exact repeat (first=False), else None. No write."""
        row = _record(kind, id, game_day=game_day, clock=clock, place=place,
                      source=source, text_ref=text_ref)
        for receipt in found.get((kind, id), ()):
            if all(receipt.get(key) == value for key, value in row.items() if key != "first"):
                return dict(deepcopy(receipt), first=False)
        return None

    def note(self, kind, id, *, game_day, clock, place, source, text_ref=None):
        """Persist distinct native observations; first is once per kind+native ID.

        An exact repeat returns first=False without writing. A later native read
        can add text_ref after identity-only save backfill, keeping the original
        receipt intact. No occurrence's game date or source is rewritten.
        """
        import arti_mind
        row = _record(kind, id, game_day=game_day, clock=clock, place=place,
                      source=source, text_ref=text_ref)
        with arti_mind._JOURNEY_LOCK:
            rows = self._rows()
            prior = []
            for old in rows:
                receipt = old.get("receipt")
                if (old.get("scope_hash") == self.scope_hash and isinstance(receipt, Mapping)
                        and (receipt.get("kind"), receipt.get("id")) == (kind, id)):
                    prior.append(receipt)
                    if all(receipt.get(key) == value for key, value in row.items() if key != "first"):
                        return dict(deepcopy(receipt), first=False)
            row["first"] = not prior
            import json
            occurrence = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            event_id = "seen_receipt:" + kind + ":" + id + ":" + hashlib.sha256(occurrence.encode()).hexdigest()
            identity = hashlib.sha256((self.scope + "\0" + event_id).encode()).hexdigest()
            envelope = dict(id=identity, kind="seen_receipt", subject=id,
                            scope_hash=self.scope_hash, source=source,
                            date=row["game_day"]["text"], day=row["game_day"]["absolute"],
                            receipt=row)
            # ADR 231 part C: one appended line (ADR 226's complete append), not a rewrite of the whole record per
            # receipt; the new envelope's id is new, so every reader's rows are the ones the rewrite gave.
            from arti_runtime import ipc
            ipc.append_text(self.path, json.dumps(envelope, ensure_ascii=False) + "\n")
        return deepcopy(row)

    def backfill_from_save(self, native_state):
        return backfill_from_save(native_state)
