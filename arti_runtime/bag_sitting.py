"""One explicit bag request, native controls, paired receipts, one close (ADR 222).

The caller names the items. This module never decides what to keep. Each input
is re-pinned from the next native frame and uses the ordinary leased executor.
"""

from arti_runtime import wait_ledger as W
from collections import Counter
from copy import deepcopy
from contextlib import contextmanager
import re
import time

VERBS = ('simpan', 'ambil', 'kirim', 'jual')
MAX_STACKS = 36


@contextmanager
def seed_start(session, call, state):
    """Make the already chosen batch visible before synchronous owner ingress."""
    previous=getattr(session,'_bag_start_seed',None)
    if state is not None:
        session._bag_start_seed=(call,state)
    try:
        yield
    finally:
        if previous is None:
            session.__dict__.pop('_bag_start_seed',None)
        else:
            session._bag_start_seed=previous


def start_state(session, call):
    seed=getattr(session,'_bag_start_seed',None)
    return dict(seed[1]) if session.mode=='part_live' and seed and seed[0]==call else {}


def _side(component):
    path = component.get('field_path', '')
    if re.search(r'(?:^|\.)inventory\.inventory\[\d+\]$', path):
        return 'bag'
    if 'ItemsToGrabMenu' in path:
        return 'chest'
    return None


def catalogue(view, actions):
    """Only whole-stack gestures actually published on this receiver."""
    if (view is None or not view.generic or view.close_only and not view.kept_primitives or view.held_item
            or (view.cursor_settle or {}).get('cursor')):
        return []
    container = view.menu_container or {}
    kind = container.get('kind')
    if view.menu_type == 'ShopMenu':
        verb = 'jual'
    elif view.menu_type == 'ItemGrabMenu' and kind in ('chest', 'shipping_bin'):
        verb = 'simpan' if kind == 'chest' else 'kirim'
    else:
        return []
    rows = []
    for c in view.components:
        v, side = c.get('value') or {}, _side(c)
        qid, count = v.get('item'), v.get('stack')
        if (c.get('kind') != 'slot' or not qid or type(count) is not int or count <= 0
                or side not in ('bag', 'chest') or side == 'chest' and kind != 'chest'):
            continue
        action = 'ambil' if side == 'chest' else verb
        key = 'press:A:%s%s' % (c['id'], '' if action == 'jual' else ':shift')
        # Refused rows stay visible but cannot authorize a native press.
        if key not in actions or c.get('accepts_item') is False:
            continue
        match = re.search(r'\[(\d+)\]$', c.get('field_path', ''))
        rows.append(dict(verb=action, name=c.get('label') or qid, item=qid,
            quality=v.get('quality', 0), count=count, id=c['id'], key=key, side=side,
            slot=int(match[1]) if match else None))
    # A native wire-budget stub keeps transfers instead of generic widgets.
    # It is a working menu, unlike an uncertainty latch. Reuse those exact
    # published whole-stack wires; never reconstruct a click or quantity.
    for key, chosen in actions.items():
        if not isinstance(chosen, tuple) or chosen[0] != 'container_transfer':
            continue
        wire = chosen[1]
        mode = wire.get('mode', 'put')
        if mode not in ('put', 'quick', 'take_out') or wire.get('quantity', wire.get('stack')) != wire.get('stack'):
            continue
        action = 'ambil' if mode == 'take_out' else verb
        side = 'chest' if action == 'ambil' else 'bag'
        slot, qid, quality, count = (wire.get(k) for k in ('slot','qualified_item_id','quality','stack'))
        if any(r['verb'] == action and r['slot'] == slot and r['item'] == qid and r['quality'] == quality for r in rows):
            continue
        rows.append(dict(verb=action, name=(view.item_names or {}).get(qid) or qid, item=qid, quality=quality,
                         count=count, id=f'{side}:{slot}', key=key, side=side, slot=slot))
    if getattr(view, 'named_item_args', False):
        from arti_runtime.item_choice import display_labels
        rows = [row for row in rows if row['name'] != row['item']]
        for verb in VERBS:
            named = [row for row in rows if row['verb'] == verb]
            for row, label in zip(named, display_labels(named)):
                row['arg'] = label
    return rows


def choices(view, actions):
    rows = catalogue(view, actions)
    return {f"{r['verb']}: {r.get('arg', r['name'])}": ('bag_sitting', None) for r in rows}


def select(arg, rows):
    """A comma/semicolon list is an exact union of displayed names or IDs."""
    if not isinstance(arg, str) or ':' not in arg:
        return None
    verb, names = arg.split(':', 1)
    if verb not in VERBS:
        return None
    names = list(dict.fromkeys(n.strip() for n in re.split(r'[,;]', names)))
    if not names or any(not n for n in names):
        return None
    def names_for(row):
        return (row['arg'],) if 'arg' in row else (row['name'], row['item'])
    picked = [r for r in rows if r['verb'] == verb and any(n in names for n in names_for(r))]
    if any(not any(n in names_for(r) for r in picked) for n in names):
        return None
    if not picked or len(picked) > MAX_STACKS:
        return None
    return deepcopy(picked)


def valid(arg, view, shown_actions):
    # A list can combine only singles which survived this rendered page.
    if view is None or not isinstance(arg,str) or not arg.startswith(tuple(v+':' for v in VERBS)):
        return False
    rows = [r for r in catalogue(view, _native_actions(view))
            if f"{r['verb']}: {r.get('arg', r['name'])}" in shown_actions]
    return select(arg, rows) is not None


def _native_actions(view):
    from arti_stardew_generic_ui import controls
    return controls(view)


def fact(view, actions):
    rows = catalogue(view, actions)
    if not rows:
        return None
    return dict(arg='ui_action dengan arg="simpan: A, B" (atau ambil/kirim/jual sesuai pilihan); '
        'A/B adalah nama di args. Seluruh stack; hanya yang disebut pindah. '
        'Hasil per barang; menu menutup sesudah permintaan selesai.',
        items=[[r['verb'], r.get('arg', r['name']), r['count']] for r in rows],
        bulk='Add To Existing Stacks dipakai hanya bila semua barang yang dapat dipindah tombol itu kamu sebut; '
             'jika tidak tersedia, stack terpilih dipindah satu per satu dalam keputusan yang sama.')


def _identity(view):
    c = view.menu_container or {}
    return view.receiver_token, view.menu_type, c.get('kind'), c.get('source_fact_id')


def _totals(ledger, side):
    from arti_runtime.transfer_outcomes import _bag, _outside
    bag = _bag(ledger)
    if bag is None:
        return None
    if side == 'bag':
        return Counter({key: sum(r['stack'] for r in bag.values() if (r['item'], r['quality']) == key)
                        for key in {(r['item'], r['quality']) for r in bag.values() if r['item']}})
    if side == 'shipping':
        out = Counter()
        for r in ledger.get('shipping_items', ()):
            out[r.get('item'), r.get('quality', 0)] += r.get('stack', 0)
        return out
    keys = {(r.get('item'), r.get('quality', 0)) for r in ledger.get('ui_slots', ()) if r.get('item')}
    return Counter({k: _outside(ledger, k) for k in keys})


def verify(proof, selected, verb):
    """Paired counts; a command lifecycle or vanished bag stack is insufficient."""
    if proof is None:
        return None
    before, after = proof['before'], proof['after']
    from arti_runtime.transfer_outcomes import _held
    if _held(before) != {} or _held(after) != {}:
        return None
    b, a = _totals(before, 'bag'), _totals(after, 'bag')
    if b is None or a is None:
        return None
    allowed = {(r['item'], r['quality']) for r in selected}
    delta = {k: (a[k]-b[k]) * (1 if verb == 'ambil' else -1) for k in b.keys() | a.keys()}
    if any(n and (k not in allowed or n < 0) for k, n in delta.items()):
        return None
    moved = {k: n for k, n in delta.items() if n > 0}
    if verb in ('simpan', 'ambil', 'kirim'):
        side = 'shipping' if verb == 'kirim' else 'chest'
        x, y = _totals(before, side), _totals(after, side)
        outside = {k: (y[k]-x[k]) * (-1 if verb == 'ambil' else 1) for k in x.keys() | y.keys()}
        if {k:n for k,n in outside.items() if n} != moved:
            return None
    else:
        mb, ma = before.get('money'), after.get('money')
        if type(mb) is not int or type(ma) is not int or (bool(moved) and ma <= mb) or (not moved and ma != mb):
            return None
    return moved


def _fill(view, actions, selected):
    """Native fill can touch only caller-named item/quality pairs."""
    if not selected or selected[0]['verb'] != 'simpan':
        return None
    chest = {(c.get('value', {}).get('item'), c.get('value', {}).get('quality', 0))
             for c in view.components if _side(c) == 'chest' and (c.get('value') or {}).get('item')}
    if view.components_total != len(view.components):
        return None
    affected = {c['id']
                for c in view.components if _side(c) == 'bag'
                and ((c.get('value') or {}).get('item'), (c.get('value') or {}).get('quality',0)) in chest}
    named = {r['id'] for r in selected}
    # Native IDs/quality can be shared by distinct preserved-item variants.
    # Their source slots must all be named; separate presses retain attribution.
    if (not affected or not affected <= named
            or len({(r['item'],r['quality']) for r in selected})!=len(selected)):
        return None
    return next((f"press:A:{c['id']}" for c in view.components
                 if c.get('field_path', '').endswith('.fillStacksButton')
                 and f"press:A:{c['id']}" in actions), None)


def _execute_owned(runtime, session, arg, initial, selected):
    """Execute a frozen explicit selection without another provider call."""
    from arti_stardew_generic_ui import ui_readback, exit_action
    from arti_runtime.transfer_outcomes import publish_menu, remember
    goal = runtime._active
    identity, revision = _identity(initial), goal.revision
    pid = runtime._binding_view()[1]
    clock_basis = runtime._date_tuple(runtime._state.latest(pid))
    deadline = time.monotonic() + 60
    verb = selected[0]['verb']
    moved, requests, inputs = Counter(), [], 0
    moved_named, expected_named = Counter(), Counter()
    expected = Counter()
    for r in selected:
        expected[r['item'], r['quality']] += r['count']
        expected_named[r['item'],r['quality'],r['name']] += r['count']
    reason, closed = 'completed', False

    def owner_current():
        if (runtime._active is not goal or goal.revision != revision or session is not runtime._skill_session
                or session.status != 'active' or not runtime._live7_active()
                or runtime.live7_stop_file_check() or time.monotonic() >= deadline
                or runtime._date_tuple(runtime._state.latest(pid)) != clock_basis):
            raise ValueError('bag_sitting_interrupted')

    def observe(*, publishing=True):
        # A slow owned readback can outlive a motor/UI cache sample. Wait only
        # for a new native observation; never replay an input or relax freshness.
        until=min(deadline,time.monotonic()+2.5)
        while True:
            owner_current()
            v=runtime._live7_unknown_ui_view(session,publishing=publishing)
            if v is not None:return v
            why=getattr(runtime,'_live7_ui_view_miss',None)
            if why not in ('motor_stale','ui_context_old','ui_context_stale','ui_context_missing'):
                return None
            left=until-time.monotonic()
            if left<=0:return None
            wait=getattr(runtime._state,'wait_next',None)
            if callable(wait):
                motor=runtime._state.latest(pid)
                wait(pid,getattr(motor,'revisi',0),timeout=min(.25,left))
            else:
                W.call('poll', 78, time.sleep, min(.05,left))

    def fresh(*, publishing=True):
        v=observe(publishing=publishing)
        if v is None:
            raise ValueError('bag_sitting_view_unavailable:'+str(getattr(runtime,'_live7_ui_view_miss','unknown')))
        if _identity(v) != identity:
            raise ValueError('bag_sitting_receiver_changed')
        if v.held_item or (v.cursor_settle or {}).get('cursor'):
            raise ValueError('bag_sitting_cursor_changed')
        return v

    def press(key, view, selection, *, closing=False):
        nonlocal inputs, reason, identity
        labels={(r['item'],r['quality']):r['name'] for r in selection}
        if len(labels)!=len(selection):
            raise ValueError('bag_sitting_ambiguous_sources')
        available=getattr(runtime,'_store_close_input_available',None)
        if not closing and callable(available) and not available(reserve=2):
            raise ValueError('bag_sitting_close_reserved')
        actions = _native_actions(view)
        chosen = actions[key]
        before_container = (runtime._obs.get(pid, 'ui.context').facts or {}).get('container')
        def guard():
            now = fresh(publishing=True)
            if now.state_digest != view.state_digest or _native_actions(now).get(key) != chosen:
                raise ValueError('bag_sitting_target_changed')
        if chosen[0] in ('container_exit', 'container_transfer'):
            result = runtime._live7_container_close(chosen[1], view.receiver_token, guard, chosen[0])
        else:
            result, _ = runtime._run_ui_pad(*chosen, origin='bag_sitting', revalidate_extra=guard)
        inputs += 1
        next_view = observe(publishing=True)
        proof = ui_readback(view, result, next_view)
        current = getattr(session,'current',None)
        if current is not None and current.goal_id == getattr(goal,'goal_id',None):
            from arti_stardew_ui_effects import publish
            publish(runtime,session,view,result,chosen,goal=goal,current=current)
            if proof:
                current.state['generic_ui_readback']=proof
                if 'goods_remaining' in current.state:
                    current.state.update(goods_receiver=proof['after_receiver'],goods_menu=proof['after_menu'])
        if result.verified and result.cleanup and not result.uncertain:
            runtime._live7_ui_progress_seq=getattr(runtime,'_live7_ui_progress_seq',0)+1
        from scripts.live7.menu_visit import note_press
        note_press(runtime,view,key,closed=closing and runtime._state.latest(pid).active_menu is None)
        if closing:
            if proof:
                runtime._journal(dict(ev='generic_ui_readback',**proof))
            return bool(result.verified and result.cleanup and not result.uncertain
                        and runtime._state.latest(pid).active_menu is None)
        if chosen[0] == 'container_transfer':
            rec = runtime._obs.get(pid, 'ui.context')
            after_container = (rec.facts or {}).get('container')
            delta = verify_container(before_container, after_container, chosen[1], result, selection)
        else:
            delta = verify(proof, selection, verb)
        if delta is None:
            raise ValueError('bag_sitting_move_unverified:'+str(result.reason or result.outcome or 'readback_missing'))
        if any(moved[k] + n > expected[k] for k, n in delta.items()):
            raise ValueError('bag_sitting_quantity_changed')
        moved.update(delta)
        moved_named.update({(*k,labels[k]):n for k,n in delta.items()})
        requests.append(result.request_id)
        if next_view is not None and next_view.receiver_token != identity[0]:
            after_box=(runtime._obs.get(pid,'ui.context').facts or {}).get('container') or {}
            same_chest=(before_container and all(before_container.get(k) is not None
                and before_container.get(k)==after_box.get(k) for k in ('container_token','source_fact_id')))
            if (not same_chest or result.result_receiver_token!=next_view.receiver_token
                    or _identity(next_view)[1:]!=identity[1:]):
                raise ValueError('bag_sitting_receiver_changed')
            identity=_identity(next_view)  # only this authenticated native successor
        if proof:
            for row in publish_menu(runtime, chosen[1], result, view, next_view):
                remember(session, row)
            runtime._journal(dict(ev='generic_ui_readback', **proof))
        return delta

    def close_sitting():
        view=fresh()
        key=exit_action(view,_native_actions(view))
        return press(key,view,[],closing=True) if key else False

    try:
        view = fresh()
        current_rows = catalogue(view, _native_actions(view))
        if any(not any((r['verb'],r['id'],r['item'],r['quality'],r['count'],r['name']) ==
                       (s['verb'],s['id'],s['item'],s['quality'],s['count'],s['name']) for r in current_rows) for s in selected):
            raise ValueError('bag_sitting_selection_changed')
        bulk = _fill(view, _native_actions(view), selected)
        if bulk:
            press(bulk, view, selected)
        # Freeze the source slot and its quantity. A newly arriving item never
        # expands the selection, even if its label equals a caller-named item.
        for original in selected:
            view = fresh()
            key = original['item'], original['quality']
            if moved[key] >= expected[key]:
                continue
            rows = catalogue(view, _native_actions(view))
            current = next((r for r in rows if r['verb'] == verb
                            and (r['slot']==original['slot'] and r['side']==original['side']
                                 if original['slot'] is not None else r['id']==original['id'])
                            and (r['item'], r['quality']) == key and r['name']==original['name']), None)
            if current is None:
                reason = 'partial; selected stack unavailable'
                continue
            if current['count'] > min(original['count'],expected[key] - moved[key]):
                reason = 'partial; selected quantity changed'
                continue
            press(current['key'], view, [current])
        if moved != expected:
            reason = 'partial; requested quantities not fully moved'
        else:
            reason = 'completed'
        closed = close_sitting()
        if not closed:
            reason += '; close unverified'
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = str(exc)
        if reason=='bag_sitting_close_reserved':
            try:
                closed=close_sitting()
            except (ValueError,KeyError,TypeError,AttributeError):
                closed=False
    uncertain = 'unverified' in reason or 'receiver_changed' in reason or 'cursor_changed' in reason
    rows = [dict(item=k[0], name=k[2], quality=k[1], requested=n, moved=moved_named[k],
                 remaining=None if uncertain else n-moved_named[k],
                 state='unverified' if uncertain else 'done' if moved_named[k] == n else 'partial' if moved_named[k] else 'not_moved')
            for k,n in expected_named.items()]
    result = dict(ev='bag_sitting_result', arg=arg, verb=verb, items=rows, closed=closed,
        reason=reason, native_inputs=inputs, request_ids=requests,
        source='native_paired_counts', receiver=initial.receiver_token, at=time.time(),
        date=list(clock_basis), clock=getattr(runtime._state.latest(pid),'time_of_day',None))
    session._bag_sitting_result = result
    runtime._journal(result)
    return result


def verify_container(before, after, args, result, selected):
    from arti_runtime.transfer_outcomes import counts
    if (not result.verified or not result.cleanup or result.uncertain or result.lifecycle != 'completed'
            or result.outcome != 'TransferObserved' or not result.request_id or before is None or after is None
            or before.get('readback_token') == after.get('readback_token') or after.get('cursor') is not None):
        return None
    if (any(b.get(k) != args.get(k) for b in (before, after) for k in ('container_token','source_fact_id'))
            or before.get('receiver_token')!=args.get('receiver_token')
            or after.get('receiver_token')!=(result.result_receiver_token or args.get('receiver_token'))):
        return None
    keys = {(s['item']['qualified_item_id'],s['item']['quality']) for b in (before,after)
            for side in ('actor_slots','container_slots') for s in b.get(side,()) if s.get('item')}
    allowed = {(r['item'],r['quality']) for r in selected}
    direction = -1 if args.get('mode') == 'take_out' else 1
    moved = {}
    for qid, quality in keys:
        n = (counts(before,'actor_slots',quality)[qid]-counts(after,'actor_slots',quality)[qid])*direction
        other = (counts(after,'container_slots',quality)[qid]-counts(before,'container_slots',quality)[qid])*direction
        if n != other or n < 0 or n and (qid,quality) not in allowed:
            return None
        if n:
            moved[qid,quality] = n
    return moved


@W.span('settle_readback', 77, runtime=False)
def execute(runtime, session, arg, initial, selected):
    # Hold the existing controller busy flag across the sitting, including
    # inter-input readbacks. The old single-item finish rule must not close the
    # chest after the first member of an already-chosen batch.
    with runtime._lock:
        if runtime._action_busy or runtime._inflight_generation is not None:
            raise ValueError('bag_sitting_input_busy')
        runtime._action_busy = True
    try:
        result = _execute_owned(runtime, session, arg, initial, selected)
        if result['native_inputs']==0 and any(s in result['reason'] for s in ('_changed','_view_unavailable')):
            runtime._live7_menu_changed(session,result['reason'])  # ADR 210; no input was taken
        if arg == 'world_batch':
            finish_active(runtime, session, result)
        else:
            from arti_runtime import day_record
            if text := day_record.bag_text(result):
                motor = runtime._state.latest(runtime._binding_view()[1])
                day_record.live(runtime, result['date'], result['clock'], result['verb'], text,
                                place=getattr(motor, 'location', None),
                                witness=next(iter(result['request_ids']), None))
            status, reason = finish_verdict(result)
            complete = status == 'achieved'
            category = {'achieved':'VERIFIED', 'partial':'PARTIAL', 'blocked':'CLEAN_FAIL',
                        'uncertain':'UNCERTAIN'}[status]
            if getattr(session, 'campaign', None) is not None:
                history = session.campaign.live.history
                history.append(dict(skill='ui_action', arg=arg, outcome_class=category,
                    reason=reason, bag_sitting=result, productive=complete))
                del history[:-8]
            runtime._journal(dict(ev='live7_ui_choice', pick=arg, receiver=initial.receiver_token,
                verified=complete, cleanup=result['closed'], reason=reason,
                outcome_class=category,
                result_v1=dict(state={'achieved':'done', 'partial':'partial', 'blocked':'failed',
                                      'uncertain':'uncertain'}[status], changed='bag_sitting', bag_sitting=result),
                request_ids=result['request_ids']))
        return result
    finally:
        with runtime._lock:
            runtime._action_busy = False


def active_selection(current, view, actions):
    """The explicit world batch already named these stacks before opening."""
    if (current is None or 'goods_remaining' not in current.state
            or getattr(current,'status',None) != 'running' or current.pending is not None):
        return None
    call = current.call
    verb = {'simpan_ke_chest': 'simpan', 'kirim': 'kirim', 'jual': 'jual'}.get(call.name)
    if verb is None:
        return None
    wanted = [call, *current.state['goods_remaining']]
    rows = catalogue(view, actions)
    selected = []
    for c in wanted:
        matching = [r for r in rows if r['verb'] == verb and (r['item'] == c.arg or r['name'] == c.arg)
                    and r['quality'] == c.quality and r['count'] == c.n
                    and (c.slot is None or c.slot == r['slot'])]
        if len(matching) != 1:
            return None
        if matching[0] not in selected:
            selected.extend(matching)
    return selected


def handoff_ready(runtime, goal):
    """After an authenticated chest open, keep the explicit multi-stack sitting.

    Called after the original store chain's identity/motor/STOP checks. An
    unreadable receiver retains the original chain and its cleanup behavior.
    """
    session=getattr(runtime,'_skill_session',None)
    current=getattr(session,'current',None)
    if (getattr(session,'mode',None)!='part_live' or current is None
            or current.goal_id!=goal.goal_id or current.call.name!='simpan_ke_chest'
            or not current.state.get('goods_remaining')):
        return False
    try:
        view=runtime._live7_unknown_ui_view(session,publishing=True)
        return bool(active_selection(current,view,_native_actions(view))) if view is not None else False
    except (ValueError,TypeError,AttributeError,KeyError):
        return False


def finish_verdict(result):
    """One finish vocabulary for the whole request, including its close (ADR 222)."""
    if result['closed'] is not True or any(r['state'] == 'unverified' for r in result['items']):
        return 'uncertain', 'uncertain_bag_sitting'
    if result['items'] and all(r['state'] == 'done' for r in result['items']):
        return 'achieved', 'observed_bag_sitting'
    if any(r['moved'] > 0 for r in result['items']):
        return 'partial', 'observed_bag_sitting_partial'
    return 'blocked', result['reason']


def finish_active(runtime, session, result):
    from arti_stardew_runtime import FinishEvidenceView
    current, goal = session.current, runtime._active
    current.state['goods_remaining'] = []
    status, reason = finish_verdict(result)
    complete = status == 'achieved'
    observed = dict(bag_sitting=result, original_objective_satisfied=complete)
    current.state['result_observation'] = observed
    goal.last_finish = FinishEvidenceView(status, reason, observed)
    goal.progress_marker = 'terminal'
    goal.revision += 1
    current.pending = ('done' if complete else 'fail', reason)
    runtime._journal(dict(ev='finish_verified', goal_id=goal.goal_id, goal_revision=goal.revision,
        verdict=dict(status=status, reason=reason, observed=observed), origin='chosen_bag_sitting'))
