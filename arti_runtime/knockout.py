"""ADR 191: native defeat owns a pause, never the session's lifetime."""
import hashlib
import json
import ast
import re
from arti_stardew_pause import game_now


def remembered(row, today):
    """Readable date/age for pre-ADR207 knockout receipts, without inventing a new event."""
    from arti_runtime.plan_dates import ordinal, label
    text = str(row.get('text') or '')
    match = re.search(r"\[\d+, ['\"]\w+['\"], \d+\]", text)
    date = row.get('date')
    if match:
        raw = ast.literal_eval(match[0])
        date = 'Y%s %s %s' % tuple(raw)
    elif isinstance(date, str) and date.count(':') == 2:
        date = 'Y%s %s %s' % tuple(date.split(':'))
    day, now = ordinal('stardew', date), ordinal('stardew', today)
    stamp = label('stardew', day) if day else 'tanggal tidak diketahui'
    if day is not None and now is not None:
        stamp += ' (%d hari lalu)' % (now-day)
    if match:
        return text[:match.start()].replace('Pingsan ', 'Pingsan terakhir ') + stamp + text[match.end():]
    return stamp + ': ' + text


def latest(rows):
    """The newest dated evidence wins even when rotating native frames arrive out of order."""
    from arti_runtime.plan_dates import ordinal
    def day(row):
        value = str(row.get('date') or '')
        if value.count(':') == 2:
            value = 'Y%s %s %s' % tuple(value.split(':'))
        elif value.startswith('['):
            try: value = 'Y%s %s %s' % tuple(ast.literal_eval(value))
            except (ValueError, SyntaxError, TypeError): pass
        clock = re.search(r'jam (\d{2}):(\d{2})', str(row.get('text') or ''))
        tick = int(clock[1])*60+int(clock[2]) if clock else 0
        return (ordinal('stardew', value) or 0, tick)
    return max(rows, key=day, default=None)


def fact(row, today=None):
    from arti_stardew_live7 import _clock
    before, after = row['before'], row['after']
    money = row.get('money_lost')
    items = row.get('items_lost')
    loss = ('%sg' % money if money is not None else 'uang belum diketahui')
    loss += '; barang ' + (', '.join('%s x%s' % p for p in list(items.items())[:5]) +
                           ('; +%s jenis' % (len(items)-5) if len(items)>5 else '') if items else
                           'tidak berkurang' if items is not None else 'belum diketahui')
    from arti_runtime.plan_dates import ordinal, label
    raw = before.get('date')
    date = 'Y%s %s %s' % tuple(raw) if isinstance(raw, (list, tuple)) and len(raw) == 3 else raw
    day = ordinal('stardew', date)
    age = ordinal('stardew', today) if isinstance(today, str) else None
    date = (label('stardew', day) if day else 'tanggal tidak diketahui')
    if age is not None and day is not None:
        date += ' (%d hari lalu)' % (age-day)
    return 'Pingsan terakhir %s %s jam %s; bangun %s jam %s; %s; waktu lewat %s menit.' % (
        date, before.get('location'), _clock(before['time_of_day']) if type(before.get('time_of_day')) is int else '?',
        after.get('location'), _clock(after['time_of_day']) if type(after.get('time_of_day')) is int else '?',
        loss, row.get('minutes_lost') if row.get('minutes_lost') is not None else 'belum diketahui')


def baseline(rt, facts, motor, context):
    return dict(location=motor.location, date=list(rt._date_tuple(motor)),
                native_revision=getattr(motor, 'revisi', None),
                time_of_day=motor.time_of_day, hp=getattr(motor, 'health', None),
                money=context.get('money'),
                inventory=dict(facts.inventory) if context.get('inventory_complete') else None,
                threats=[dict(kind=t.get('type') or t.get('name') or t.get('kind'),
                              ref=t.get('id') or t.get('ref'), distance=t.get('distance'))
                         for t in (context.get('combat') or {}).get('monsters', ())],
                threat_basis='nearby native monsters; the damage source is not identified by this observation')


def poll(rt, session):
    """Real owner boundary, before skills/watchdog. STOP and identity fences remain authoritative.

    Never press through a fade. Only the existing generic UI receiver may advance
    a native dialog. A fresh complete, released wake read settles the event.
    """
    p = session.campaign.live
    _, pid = rt._binding_view()
    motor = rt._state.latest(pid) if pid is not None else None
    if motor is None or rt._state.is_stale(pid):
        return p.knockout is not None
    hp = getattr(motor, 'health', None)
    run = session.current
    last = getattr(p, '_ko_last', None)
    relocated = (last and str(last['location']).startswith('UndergroundMine')
                 and motor.location == 'Mine' and hp == 10 and motor.active_menu == 'DialogueBox')
    died = type(hp) is int and hp <= 0 or relocated or run and run.state.get('mines_death')
    if p.knockout is None and died:
        # Cancel planner ownership, not a physical release/cleanup receipt.
        with rt._lock:
            if session is not rt._skill_session or session.status != 'active':
                return False
            rt._supersede_generation()
            rt._current_generation += 1
            rt._inflight_generation = None
            rt._pending_continuation = None
        before = last or dict((run.state.get('live7_baseline') or {}) if run else {})
        p.knockout = dict(native_event=True, before=before, at=game_now(rt), clock=before.get('time_of_day'),
                          from_location=before.get('location'), presses=0, native_zero=hp <= 0 if type(hp) is int else False)
        rt._journal(dict(ev='live7_knockout', hp=hp, before=before, action='native_event_continue'))
    if p.knockout is None:
        if type(hp) is not int or hp <= 0 or not str(motor.location).startswith('UndergroundMine'):
            return False
        facts, motor, context, held = rt._live7_read(session)
        if not held and facts is not None:
            p._ko_last = baseline(rt, facts, motor, context)
        return False
    k = p.knockout
    if not k.get('native_event'):
        k['before'].update(location=k.get('from_location'), time_of_day=k.get('clock'),
                           date=list(session.campaign.date))
        k['native_event'] = True  # rejoin the existing clinic recovery too
    if rt._action_busy or getattr(rt, '_results_pending', 0) or rt._inflight_generation is not None:
        return True
    facts, motor, context, held = rt._live7_read(session)
    if held or facts is None:
        # Identity/journal faults are handled by their existing stop path, never hidden by KO.
        return False if held and not str(held).startswith('runtime_not_ready') else True
    if type(hp) is not int or hp <= 0:
        return True
    state = rt.skill_post_row_state()
    if state.get('input_released') is not True:
        return True
    if facts.menu_open:
        if rt._live7_plain_dialogue(session) or rt._live7_knockout_event(session):
            return True
        handled = rt._live7_generic_ui(session)
        if handled == 'unsafe':
            rt._live7_uncertain_ui(session, 'knockout_ui_readback_unconfirmed')
        return True
    if state.get('free') is not True or state.get('food_clear') is False:
        return True
    after = baseline(rt, facts, motor, context)
    before = k['before']
    a, b = before.get('money'), after.get('money')
    inventory = before.get('inventory')
    items = ({q: n-after['inventory'].get(q, 0) for q, n in inventory.items()
              if n > after['inventory'].get(q, 0)} if inventory is not None and after['inventory'] is not None else None)
    from arti_stardew_live7 import minutes
    start, end = before.get('time_of_day'), after.get('time_of_day')
    elapsed = (minutes(end)-minutes(start) if type(start) is int and type(end) is int
               and before.get('date') == after.get('date') else None)
    row = dict(before=before, after=after, money_lost=max(0,a-b) if type(a) is int and type(b) is int else None,
               items_lost=items, minutes_lost=elapsed, native_zero=k.get('native_zero', False))
    row['id'] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:24]
    with rt._lock:
        if session is not rt._skill_session or session.status != 'active':
            return False
        p.knockouts = (p.knockouts + [row])[-8:]
        p.knockout = None
        p._ko_last = after
        rt._journal(dict(ev='knocked_out', **row))
        if run is not None:
            run.state.pop('mines_death', None)
            rt._skill_close_run_locked(session, run, ('fail', 'knocked_out'))
        p.wait_since = None
    remember(rt, session, context, row)
    return True


def remember(rt, session, context, row):
    """The existing scoped experience ledger; no new store or unbound world fallback."""
    before = row['before']
    from arti_runtime import day_record
    catalog = context.get('world_knowledge') or {}
    day_record.live(rt, before.get('date'), before.get('time_of_day'), 'knocked_out', day_record.knockout_text(row, catalog),
                    place=before.get('location'), witness=row.get('id'), scope=catalog.get('scope'))
    base = rt._live7_mind_base()
    if base is not None and catalog.get('scope'):
        import arti_mind
        from arti_stardew_live7 import mind_day, MIND_GAME
        try:
            arti_mind.record_discoveries(base, MIND_GAME, scope=catalog['scope'], discoveries=[dict(
                id='knocked_out:'+row['id'], kind='knocked_out', subject=before.get('location'),
                text=fact(row), source='native_knockout:'+row['id'], date=str(before.get('date')))],
                day=mind_day(session.campaign.date), day_label=str(session.campaign.date),
                scoped=(getattr(rt, '_config', None) or {}).get('live7_mind_scope', False) is True)
        except (OSError, ValueError) as exc:
            rt._journal(dict(ev='discovery_memory_error', error=type(exc).__name__))
