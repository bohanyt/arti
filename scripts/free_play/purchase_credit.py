"""Offline cross-press purchase evidence; never changes runtime receipts (ADR 42)."""
from collections import Counter
import json

INTERSTITIAL = {'ui_inspect', 'live7_ui_choice', 'native_capability_choice', 'purchase_lifecycle',
                'generic_ui_readback', 'skill_offer', 'skill_boundary_render_bytes', 'trial_decision',
                'trial_reservation', 'skill_model_decision', 'screen_text',
                'live7_moment_emitted', 'discovery', 'native_capability_offer', 'skill_budget_rebuild'}


def bag(state):
    counts, slots = Counter(), set()
    rows = state.get('inventory')
    if not isinstance(rows, list) or not rows:
        raise ValueError('missing bag')
    for r in rows:
        slot, item, qty, quality = (r.get(k) for k in ('slot', 'item', 'stack', 'quality'))
        if (type(slot) is not int or slot < 0 or slot in slots or not isinstance(item, str)
                or type(qty) is not int or qty < 0 or type(quality) is not int
                or bool(item) != bool(qty)):
            raise ValueError('invalid bag')
        slots.add(slot)
        if qty:
            counts[(item, quality)] += qty
    capacity = state.get('capacity')
    if type(capacity) is not int or capacity <= 0 or slots != set(range(capacity)):
        raise ValueError('incomplete bag capture')
    return counts


def cursor(state):
    # ShopMenu-held items are distinct from the player's cursor_item field.
    c = state.get('cursor_settle') or {}
    if c.get('complete') is not True or state.get('cursor_item') != '' or state.get('cursor_stack') != 0:
        raise ValueError('incomplete cursor capture')
    item, qty = state.get('ui_held_item'), state.get('ui_held_stack')
    if item == '' and qty == 0 and c.get('status') == 'Empty' and c.get('cursor') is None:
        return None
    held = c.get('cursor') or {}
    if (isinstance(item, str) and item.startswith('(') and type(qty) is int and qty > 0
            and held.get('item') == item and held.get('stack') == qty and type(held.get('quality')) is int):
        return item, held['quality'], qty
    raise ValueError('ambiguous cursor')


def session(state):
    shop = state.get('shop') or {}
    key = (shop.get('id'), state.get('location'), state.get('receiver_epoch'))
    if (not all(isinstance(x, str) and x for x in key[:2]) or type(key[2]) is not int
            or type(shop.get('currency')) is not int or shop['currency'] != 0 or type(state.get('money')) is not int
            or shop.get('balance') != state['money']):
        raise ValueError('unbound gold shop')
    return key


def cross_press_purchases(rows):
    """Yield receipts only for an exact native debit -> cursor -> bag chain.

    Each result must follow its journaled ShopMenu request. Exact state-hash
    continuity, receiver, shop, epoch, inventory, money and item quality bind the
    two presses. Any intervening mutation/result or receiver change cancels it.
    """
    pending, request = None, None
    for n, row in rows:
        ev = row.get('ev')
        if ev == 'ui_press' and row.get('phase') == 'request':
            request = row.get('args') or {}
            if not isinstance(request, dict):
                pending, request = None, None
                continue
            if pending and (request.get('receiver_token') != pending['receiver'] or request.get('settle') is not True):
                pending = None
            continue
        if ev != 'ui_press' or row.get('phase') != 'result':
            quiet_fault = ev == 'FAULT' and (row.get('type') == 'render_block_omitted'
                or row.get('type') == 'render_failed' and row.get('code') == 'skill_render_budget_exceeded')
            if (ev not in INTERSTITIAL and not quiet_fault
                    or ev == 'generic_ui_readback' and pending and row.get('request_id') != pending['request_id']
                    or ev == 'ui_inspect' and pending and row.get('receiver_token') != pending['receiver']):
                pending = None
            continue
        old, pending = pending, None
        args, request = request, None
        try:
            rb = json.loads(row.get('readback', ''))
            a, b = rb['before'], rb['after']
            rid, receiver = row.get('request_id'), row.get('result_receiver_token')
            if (not isinstance(args, dict) or args.get('menu_type') != 'ShopMenu'
                    or row.get('result_menu_type') != 'ShopMenu' or args.get('receiver_token') != receiver
                    or not isinstance(receiver, str) or not receiver or not isinstance(rid, str) or not rid
                    or row.get('status') != 'completed' or row.get('verified') is not True
                    or row.get('outcome') != 'UiPressApplied'
                    or row.get('cleanup') is not True or rb.get('released') is not True
                    or session(a) != session(b)):
                continue
            ab, bb, ac, bc = bag(a), bag(b), cursor(a), cursor(b)
            if (old and args.get('settle') is True and receiver == old['receiver']
                    and rid != old['request_id'] and rb.get('before_state') == old['after_state']
                    and session(a) == old['session'] and a['money'] == b['money'] == old['money']
                    and ab == old['bag'] and ac == old['cursor'] and bc is None
                    and (rb.get('cursor_settle') or {}).get('Status') == 'Settled'
                    and rb['cursor_settle'].get('Conserved') is True
                    and rb['cursor_settle'].get('Moved') == ac[2]):
                expected = ab.copy()
                expected[ac[:2]] += ac[2]
                if bb == expected:
                    yield dict(request_ids=[old['request_id'], rid], refs=[old['line'], n],
                               item=ac[0], quantity=ac[2], money_delta=old['debit'])
            if (ac is None and bc is not None and ab == bb and b['money'] < a['money']
                    and rb.get('delivered') is True and rb.get('before_receiver') == rb.get('after_receiver') == receiver
                    and type(args.get('qty')) is int and args['qty'] == bc[2]
                    and isinstance(rb.get('after_state'), str) and rb['after_state']):
                pending = dict(request_id=rid, receiver=receiver, session=session(b),
                               after_state=rb['after_state'], money=b['money'], bag=bb, cursor=bc,
                               debit=b['money']-a['money'], line=n)
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
