"""Journal/bag close-as-part-of-action, close-only self-close, unchanged-since."""

SPAN_MINUTES = 200
READ_SKILLS = ("cek_jurnal", "inventori")


def after_read(current, journal_choice=None):
    """True once the open journal/bag has been presented enough to act from."""
    call = getattr(getattr(current, "call", None), "name", None)
    if current is None or call not in READ_SKILLS or not current.state.get("generic_menu_opened"):
        return False
    if call == "cek_jurnal":
        return bool(journal_choice) or bool((current.state.get("journal_read") or {}).get("seen"))
    return True


def close_only_key(actions, journal_choice=None):
    """The single close key when wait plus one exit is all that remains."""
    from arti_runtime.journal_read import LEAVE
    if journal_choice and set(journal_choice.get("choices") or {}) == {LEAVE}:
        return LEAVE if LEAVE in actions else None
    rest = [k for k in actions if k != "wait"]
    if len(rest) == 1 and (rest[0] == LEAVE or rest[0] == "press:B" or rest[0].startswith("press:B")):
        return rest[0]
    return None


def world_after_close(pick):
    return pick is not None and getattr(pick, "name", None) not in (None, "ui_action", "read_boundary")


def world_calls(runtime, current, journal_choice=None):
    if not after_read(current, journal_choice):
        return []
    return list(getattr(runtime, "_live7_last_world_calls", ()) or ())


def _digest(view):
    if getattr(view, "menu_type", None) == "QuestLog":
        data = getattr(view, "journal", None) or {}
        return ("QuestLog", tuple(r.get("key") for r in data.get("entries") or ()),
                data.get("page"), data.get("revision"))
    slots = tuple(sorted((c.get("id"), (c.get("value") or {}).get("item"), (c.get("value") or {}).get("stack"))
                         for c in getattr(view, "components", ()) or () if c.get("kind") == "slot"))
    return (getattr(view, "menu_type", None), getattr(view, "state_digest", None), slots)


def unchanged_since(runtime, view, clock, date):
    last = (getattr(runtime, "_menu_looks", None) or {}).get(getattr(view, "menu_type", None))
    if not last or last.get("digest") != _digest(view) or last.get("date") != date:
        return None
    prev = last.get("clock")
    if type(prev) is int and type(clock) is int:
        delta = (clock // 100 * 60 + clock % 100) - (prev // 100 * 60 + prev % 100)
        if delta > SPAN_MINUTES or delta < 0:
            return None
        return "tidak berubah sejak %02d:%02d (jam game)" % (prev // 100 % 24, prev % 100)
    return "tidak berubah sejak bacaan sebelumnya"


def remember_look(runtime, view, clock, date):
    looks = vars(runtime).setdefault("_menu_looks", {})
    if getattr(view, 'menu_type', None) == 'GameMenu' and _content(view) is None:
        return
    last = looks.get(getattr(view, "menu_type", None))
    if last and last.get("digest") == _digest(view) and last.get("date") == date:
        # ADR 231 part E: "unchanged since" names when this unchanged menu was first seen. Re-anchoring on every build
        # moved the line at each game-clock tick (co-op menus do not stop the clock) while nothing in the menu changed.
        clock = last.get("clock")
    looks[getattr(view, "menu_type", None)] = dict(digest=_digest(view), clock=clock, date=date,
                                                content=_content(view))


def _content(view):
    if getattr(view, 'menu_type', None) == 'QuestLog':
        return {str(r.get('key')): repr({k: v for k, v in r.items() if k not in ('bounds', 'id')})
                for r in (getattr(view, 'journal', None) or {}).get('entries', ())}
    rows = {}
    if getattr(view, 'menu_type', None) == 'GameMenu' and not any(
            c.get('kind') == 'slot' and '.inventory.inventory[' in c.get('field_path', '')
            for c in getattr(view, 'components', ()) or ()):
        return None  # another GameMenu tab does not prove that the bag became empty
    for c in getattr(view, 'components', ()) or ():
        v = c.get('value') or {}
        if c.get('kind') != 'slot' or not v.get('item'):
            continue
        key = (v['item'], v.get('quality', 0))
        name = c.get('label') or v['item']
        rows[key] = (name, rows.get(key, ('', 0))[1] + v.get('stack', 0))
    return rows


def changes_since(runtime, view):
    last = (getattr(runtime, '_menu_looks', None) or {}).get(getattr(view, 'menu_type', None))
    if not last or 'content' not in last:
        return 'Bacaan menu pertama yang tercatat.'
    old, new = last['content'], _content(view)
    if old is None or new is None:
        return 'Isi tas belum terbaca pada tab ini.'
    if old == new:
        return 'Isi tidak berubah sejak menu ini terakhir dibuka.'
    if getattr(view, 'menu_type', None) == 'QuestLog':
        changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
        return 'Jurnal berubah: ' + ', '.join(changed)
    changed = []
    for k in sorted(old.keys() | new.keys()):
        before, after = old.get(k, ('', 0)), new.get(k, ('', 0))
        if before[1] != after[1]:
            changed.append('%s%s %d->%d' % (after[0] or before[0], ' q%s' % k[1] if k[1] else '', before[1], after[1]))
    return 'Tas berubah: ' + ', '.join(changed)


def recent_line(history):
    rows = list(history)[-3:]
    return ' | '.join('%s%s: %s' % (r.get('skill', '?'),
                       ' ' + str(r['arg'])[:32] if r.get('arg') is not None else '',
                       str(r.get('result') or r.get('reason') or r.get('status') or '?')[:48]) for r in rows)


def saved_from_decisions(rows):
    """How many archived decisions (a) world-after-read, (b) close-only, (c) unchanged, bed-wait would skip."""
    a = b = c = wait_saved = 0
    prev = None
    last_look = {}
    wait_run = False
    for row in rows:
        skill = row.get("skill") or row.get("name")
        arg = row.get("arg")
        key = (skill, arg)
        if skill == "ui_action" and arg == "tutup_jurnal":
            b += 1
            if prev and prev[0] in READ_SKILLS:
                nxt = row.get("next_skill")
                if nxt and nxt not in READ_SKILLS and nxt != "ui_action":
                    a += 1
                    b -= 1
        elif skill == "ui_action" and arg in ("press:B",) and prev and prev[0] in READ_SKILLS:
            nxt = row.get("next_skill")
            if nxt and nxt not in READ_SKILLS and nxt != "ui_action":
                a += 1
            else:
                b += 1
        elif skill == "ui_action" and arg == "wait" and row.get("sleep_waiting"):
            if wait_run:
                wait_saved += 1
            else:
                wait_run = True
        else:
            wait_run = False
        menu = row.get("menu_type")
        digest = row.get("digest")
        if skill in READ_SKILLS and menu and digest is not None:
            seen = last_look.get(menu)
            if seen == digest:
                c += 1
            last_look[menu] = digest
        prev = key
    return dict(world_after_read=a, close_only=b, unchanged=c, bed_wait=wait_saved)
