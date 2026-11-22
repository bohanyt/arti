"""ADR 143: a controller close is a read-back interruption, never task completion.

fix137A (ADR 237 B): a menu that held her with no decision is closed by the game's own close; the close (or the game's
refusal) is one dated row in her ONE record, in the game's words, and on her next page."""
from collections.abc import Mapping
import json
import time

SHOWN_CHARS = 60


def menu_words(view):
    """The words the game showed on this menu page (its own text), and the page's own title where the menu has one."""
    shown = " ".join(str(getattr(view, "text", "") or "").split())
    data = getattr(view, "journal", None) or {}
    rows, entry = data.get("entries") or (), data.get("entry")
    page = (rows[entry].get("title") if type(entry) is int and 0 <= entry < len(rows) and isinstance(rows[entry], Mapping)
            else None)
    if shown:
        shown = shown[:SHOWN_CHARS] + ("..." if len(shown) > SHOWN_CHARS else "")
    return shown or None, page if isinstance(page, str) and page.strip() else None


def _menu_phrase(note):
    shown, page = note.get("shown"), note.get("page")
    if note.get("menu") == "minigame":
        return "minigame yang berjalan"  # ADR 243: a minigame left by its own way out is not called a menu
    phrase = 'menu yang menampilkan "%s"' % shown if shown else "menu yang terbuka"
    return phrase + (' (halaman "%s")' % page if page else "")


def note_program_close(session, view, *, bound_s, why, minigame=False):
    """Before the program presses the game's own close: which menu, which page, in the game's words. ADR 243: a
    minigame left by its own way out is named as one."""
    shown, page = menu_words(view)
    session._menu_program_close = dict(receiver=view.receiver_token, menu="minigame" if minigame else view.menu_type,
                                       shown=shown, page=page,
                                       bound_s=bound_s, why=why)


def _date_label(date):
    try:
        return "Y%s %s %s" % tuple(date)
    except TypeError:
        return str(date)


def _clock(clock):
    return "%02d:%02d" % divmod(clock, 100) if type(clock) is int else "?"


def _her_record(runtime, session, row, place):
    """Her ONE record (ADR 236 F): the existing scoped experience ledger, game date and clock."""
    from arti_runtime import day_record
    if type(row.get("clock")) is int:
        day_record.live(runtime, tuple(row["date"]), row["clock"], row["status"], row["text"], place=place,
                        witness=row.get("request_id") or row.get("receiver"))


def _head(note, verb):
    phrase = _menu_phrase(note)
    return "%s%s %s %d detik tanpa keputusan" % (phrase[:1].upper(), phrase[1:], verb, int(note.get("bound_s") or 0))


def program_close_text(note):
    return _head(note, "kubiarkan") + "; program menutupnya dengan tombol tutup game sendiri, tanpa jawabanku. Aku bebas bergerak lagi."


def held_text(note, answers=False):
    head = _head(note, "menahanku")
    if answers:
        return head + "; game tidak menutupnya tanpa jawaban, jadi menu itu masih terbuka dan jawabannya tetap pilihanku."
    return head + "; game tidak mau menutupnya, jadi menu itu masih terbuka."


def held(runtime, session, view, *, bound_s, answers, request_id=None):
    """The game refused the close (or the menu must be answered): the same row says so, once per menu, and the cycle
    reads `menu_held` (recoverable) from the session; nothing is pressed here."""
    current = getattr(session, "_menu_held", None)
    if current is not None and current.get("receiver") == view.receiver_token:
        return current
    shown, page = menu_words(view)
    note = dict(receiver=view.receiver_token, menu=view.menu_type, shown=shown, page=page, bound_s=bound_s)
    facts, motor, context, _ = runtime._live7_read(session)
    row = dict(ev="menu_recovery_outcome", at=time.time(), source="native_menu_deadline", receiver=view.receiver_token,
               request_id=request_id, menu=view.menu_type, shown=shown, page=page, status="menu_held",
               unanswered=True, text=held_text(note, answers), date=list(session.campaign.date),
               clock=getattr(motor, "time_of_day", None))
    session._menu_held = dict(row)
    session._menu_recovery_outcome = row
    session._menu_program_close = None
    runtime._journal(row)
    _her_record(runtime, session, row, getattr(motor, "location", None) or getattr(facts, "location", None))
    return row


def closed_by_proof(run, receiver):
    """The press's own native readback: closed (True), refused with the same menu up and nothing changed (False), or
    unknown (None, e.g. the close part turned a page back; the next cycle tries again)."""
    try:
        proof = json.loads(getattr(run, "readback", None) or "null")
    except (TypeError, ValueError):
        proof = None
    if isinstance(proof, Mapping) and proof.get("before_receiver") == receiver:
        if proof.get("effect") == "closed" or (proof.get("after_receiver") not in (None, receiver)
                                               and proof.get("result_menu") != proof.get("before_menu")):
            return True
        if (proof.get("after_receiver") == receiver and proof.get("effect") != "changed"
                and not getattr(run, "uncertain", False)):
            return False  # the same menu is up and the press changed nothing: the game refused its close
    return None  # not sent or refused by admission: no word from the game yet; the next cycle tries again


def record_closed(runtime, session, native, receiver):
    if (not native.verified or not native.cleanup or getattr(native, "uncertain", False)
            or not getattr(native, "request_id", None)):
        return None
    if (previous := getattr(session, "_menu_recovery_outcome", None)) and previous["request_id"] == native.request_id:
        return previous
    pending = getattr(session, "_menu_recovery_pending", None)
    if pending is None or pending[0].request_id != native.request_id:
        pending = session._menu_recovery_pending = (native, receiver, session.current)
    facts, motor, context, held = runtime._live7_read(session)
    state = runtime.skill_post_row_state()
    if (held or facts is None or not getattr(facts, "inventory_current", False)
            or state.get("menu") is not None or state.get("input_released") is not True
            or state.get("free") is not True):
        return None
    current = pending[2]
    # The last-resort path bypasses the ordinary UI choice handler. End the same
    # chest episode there too, retaining any standing paired/partial effects.
    if current is session.current and current is not None and current.call.name == "simpan_ke_chest":
        from scripts.live7.transfers import finish_closed_store
        finish_closed_store(runtime, session, runtime._active, reason="menu_closed_by_recovery")
    item = getattr(getattr(current, "call", None), "arg", None)
    inventory = dict(facts.inventory)
    base = (current.state.get("live7_baseline") or {}) if current is not None else {}
    from arti_runtime.fact_names import item_name, source_names
    names = {**base.get("names", {}), **source_names(context or {})}
    text = "Menu ditutup oleh pemulihan."
    note = getattr(session, "_menu_program_close", None)
    note = note if isinstance(note, Mapping) and note.get("receiver") == receiver else None
    if note is not None:
        text = program_close_text(note)  # fix137A (ADR 237 B): which menu, which page, closed unanswered
    interrupted = current is not None and current.pending is not None and current.pending[0] == "fail"
    if interrupted:
        label = "Penyimpanan" if current.call.name == "simpan_ke_chest" else "Tindakan"
        text = "%s belum selesai. %s" % (label, text)
    if isinstance(item, str) and item.startswith(("(O)", "(T)", "(W)")):
        text += " %s tersisa x%d di tas." % (item_name(item, names), inventory.get(item, 0))
    row = dict(ev="menu_recovery_outcome", at=time.time(), source="native_post_recovery_readback",
               receiver=receiver, request_id=native.request_id, skill=getattr(getattr(current, "call", None), "name", None),
               status="interrupted" if interrupted else "closed", item=item, bag_left=inventory.get(item, 0) if item else None,
               bag_unchanged=inventory == base.get("inventory"),
               text=text, date=list(session.campaign.date), clock=getattr(motor, "time_of_day", None))
    if note is not None:
        row.update(menu=note.get("menu"), shown=note.get("shown"), page=note.get("page"), unanswered=True,
                   bound_s=note.get("bound_s"), why=note.get("why"), menu_after=state.get("menu"))
        session._menu_program_close = None
        session._menu_held = None
    session._menu_recovery_outcome = row
    session._menu_recovery_pending = None
    runtime._journal(row)
    if note is not None:
        _her_record(runtime, session, dict(row, status="menu_closed"), getattr(motor, "location", None)
                    or getattr(facts, "location", None))
    return row


def fact(session, runtime=None):
    if runtime is not None and (pending := getattr(session, "_menu_recovery_pending", None)):
        record_closed(runtime, session, pending[0], pending[1])
    row = getattr(session, "_menu_recovery_outcome", None)
    if row is None:
        return None
    if row.get("unanswered"):
        return "%s %s %s" % (_date_label(row["date"]), _clock(row["clock"]), row["text"])  # ADR 237 B
    return row["text"] + " (dibaca %s %s)" % (row["date"], row["clock"])
