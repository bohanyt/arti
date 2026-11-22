"""FIX76A (ADR 134; a55 d18 J675-J1686, d26 J1400-J1724): a menu page is a page of its own and she decides statelessly.

d18: 72 decisions in the bundle book, 10:10 to 24:00, Spring Foraging and back. d26: 4 h 20 in the bag menu. In multiplayer
the clock runs in a menu at the native rate (`Game1.shouldTimePass`: `IsMultiplayer` returns `!IsTimePaused`; 573 s for
10:40 to 24:00), and the page had no clock, no energy, no HP and no trace of her own presses: the same two page digests came
back identical, so every decision was her first. These are the true facts a person holds without thinking. Nothing here
closes a menu, counts against her, or tells her what to do.
"""
from arti_runtime.fact_names import control_label

VISIT_GAP_MINUTES = 60  # game minutes without this menu's page: it was closed, and this is another visit
MENU_FACT_CHARS = 340
PRESSES_SHOWN = 3


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
