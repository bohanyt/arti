"""FIX76A (ADR 134; a55 d18 J675-J1686, d26 J1400-J1724): a menu page is a page of its own and she decides statelessly.

d18: 72 decisions in the bundle book, 10:10 to 24:00, Spring Foraging and back. d26: 4 h 20 in the bag menu. In multiplayer
the clock runs in a menu at the native rate (`Game1.shouldTimePass`: `IsMultiplayer` returns `!IsTimePaused`; 573 s for
10:40 to 24:00), and the page had no clock, no energy, no HP and no trace of her own presses: the same two page digests came
back identical, so every decision was her first. These are the true facts a person holds without thinking. Nothing here
closes a menu, counts against her, or tells her what to do.
"""
import hashlib
import json
from dataclasses import dataclass, field

from arti_runtime.fact_names import control_label

VISIT_GAP_MINUTES = 60  # game minutes without this menu's page: it was closed, and this is another visit
MENU_FACT_CHARS = 340
PRESSES_SHOWN = 3


@dataclass
class ReceiverVisit:
    key: tuple
    pages: dict = field(default_factory=dict)
    last_verified_press: tuple | None = None
    cells: dict = field(default_factory=dict)
    read_renewals: int = 0
    renewed_since_new: bool = True

    def read(self, entries, key, identity, offered):
        repeated = entries.get(key) == identity or self.read_renewals >= offered + 2
        entries[key] = identity
        if not repeated:
            self.renewed_since_new = False
        return repeated


def identity(content):
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:12]


def page_identity(page, selected, global_actions, facts):
    """Use the pager's native labels and terms. The full digest still fences input."""
    ids = {row[g["fields"].index("id")] for g in selected.get("groups", ()) for row in g["rows"]}
    stock_ids = {arg.split(":")[1] for arg in (*global_actions, *selected.get("native_args", ()))
                 if arg.startswith("buy:")}
    text = [facts.get("text")] if page == 0 else []
    stock_rows = set()
    for path, value in selected.get("details", ()):
        if not path:
            text.append(value.get("text"))
            stock_rows.update(range(len((value.get("shop") or {}).get("stock", ()))))
        elif path[0] == "text":
            text.append(value)
        elif path[0] == "components" and (len(path) == 2 or path[2] in ("label", "value")):
            ids.add(path[1])
        elif path == ["shop"]:
            stock_rows.update(range(len(value.get("stock", ()))))
        elif path[:2] == ["shop", "stock"]:
            stock_rows.update([path[2]] if len(path) > 2 else range(len(value)))
    components = [(c["id"], c.get("label"), tuple((c.get("value") or {}).get(k) for k in ("item", "stack", "price")))
                  for c in facts["components"] if c["id"] in ids]
    stock = [(row[8], *row[1:6], row[9]) for i, row in enumerate((facts.get("shop") or {}).get("stock", ()))
             if row[8] in ids or str(row[8]) in stock_ids or i in stock_rows]
    return identity((page, components, stock, text))


def receiver_visit(owner, session, view):
    key = (session.session_id, view.receiver_token)
    visit = getattr(owner, "_live7_receiver_visit", None)
    if visit is None or visit.key != key:
        visit = owner._live7_receiver_visit = ReceiverVisit(key)
    return visit


def _minutes(clock):
    return clock // 100 * 60 + clock % 100 if type(clock) is int else None


def _clock(clock):
    return "%02d:%02d" % divmod(clock, 100)


def note_view(owner, view, clock, date):
    """This menu's visit, kept across its page changes. A receiver token changes under one open chest, so it is no key."""
    now = _minutes(clock)
    visit = owner.__dict__.get("_live7_menu_visit")
    if (not visit or visit["menu"] != view.menu_type or visit["date"] != date or visit["closed"]
            or now is None or visit["last"] is None or not 0 <= now - visit["last"] <= VISIT_GAP_MINUTES):
        visit = owner.__dict__["_live7_menu_visit"] = dict(menu=view.menu_type, date=date, since=clock, last=now, closed=False,
                                                           presses={}, seen={}, digest=None, reserve_at=None)
    visit["last"] = now
    digest = view.state_digest
    if digest and digest != visit["digest"]:
        visit["digest"] = digest
        visit["seen"][digest] = visit["seen"].get(digest, 0) + 1
    return visit


def note_press(owner, view, pick, *, closed):
    """Her own press, by the label the page showed. A read and a wait are not presses."""
    visit = owner.__dict__.get("_live7_menu_visit")
    if not visit or visit["menu"] != view.menu_type:
        return
    if closed:
        visit["closed"] = True
    elif str(pick) in ("press:B", "press:LB"):
        visit["stayed"] = str(pick)  # d18 J943, J1139: she chose to leave, B turned the page, and nothing said she was still inside
    if str(pick).startswith("press:"):
        ids = [p for p in str(pick).split(":")[1:] if p.isdigit()]
        label = next((control_label(c.get("label"), c.get("field_path")) for c in view.components if ids and str(c.get("id")) == ids[0]), None)
        key = "%s (%s)" % (pick, label[:24]) if isinstance(label, str) and label and "belum diketahui" not in label else str(pick)
        visit["presses"][key] = visit["presses"].get(key, 0) + 1


def menu_fact(visit, clock, *, energy=None, hp=None, reserve=None, bed_wait=False):
    """One line: the clock and her vitals on every menu page; this visit's time and presses once she has pressed something."""
    if type(clock) is not int:
        return None
    vitals = "".join((", energi %d" % energy if type(energy) in (int, float) else "", ", HP %d" % hp if type(hp) in (int, float) else ""))
    parts = ["Jam %s%s; jam game terus jalan selama menu terbuka." % (_clock(clock), vitals)]
    if bed_wait:
        # ADR 154: a return-home reserve on her bed-wait page misframes readiness as unfinished travel.
        return "Jam %s%s; kamu di tempat tidur, siap tidur dan menunggu pemain lain atau konfirmasi jaringan." % (_clock(clock), vitals)
    if (visit or {}).get("stayed"):
        parts.append("%s tadi tidak menutup menu ini; menu masih terbuka." % visit["stayed"])
    presses = (visit or {}).get("presses") or {}
    if presses:
        spent = max(0, (_minutes(clock) or 0) - (_minutes(visit["since"]) or 0)) if type(visit["since"]) is int else None
        top = sorted(presses.items(), key=lambda row: -row[1])[:PRESSES_SHOWN]
        parts.append("Menu ini terbuka sejak %s, %d jam %d menit, %d tekan: %s." % (
            _clock(visit["since"]), spent // 60, spent % 60, sum(presses.values()), ", ".join("%s x%d" % row for row in top)))
        again = visit["seen"].get(visit["digest"], 0)
        if again > 1:
            parts.append("Halaman ini sudah %d kali terbuka." % again)
    if isinstance(reserve, dict) and reserve.get('active') is True and type(reserve.get("triggers_at")) is int:
        parts.append("Cadangan pulang dari sini mulai %s%s." % (_clock(reserve["triggers_at"]), ", sudah lewat" if reserve.get("active") else ""))
    return " ".join(parts)[:MENU_FACT_CHARS]
