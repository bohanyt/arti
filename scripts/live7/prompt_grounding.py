"""Bounded presentation summaries; canonical facts and read pages stay unchanged (ADR 39)."""
from arti_runtime import page_budget
import copy
import json


# ADR 163: recent failures have a bounded, protected projection. The complete
# history remains in the canonical offer and its existing detail read.
BLOCKS = ('discoveries', 'game_facts', 'world')
FAILURE_ROWS = 4
FAILURE_BYTES = 512


def protected_failures(value):
    if not isinstance(value, list):
        return copy.deepcopy(value) if size(value) <= page_budget.scaled(FAILURE_BYTES) else bounded(value, page_budget.scaled(FAILURE_BYTES), 'detail_read')
    ordered = value
    if len(value) > page_budget.scaled(FAILURE_ROWS) and all(isinstance(r, dict) and isinstance(r.get('seconds_since_last_attempt'), (int, float))
                                         for r in value):
        ordered = sorted(value, key=lambda r: -r['seconds_since_last_attempt'])
    rows = copy.deepcopy(ordered[-page_budget.scaled(FAILURE_ROWS):])
    # Repeated scope/window descriptions and null optionals are not the failed
    # choice. Their exact source rows remain on the detail read.
    keep = ('skill', 'arg', 'n', 'slot', 'quality', 'tile', 'batch', 'reasons',
            'failed_attempts', 'state_changed_since_last_attempt')
    rows = [{k: v for k, v in r.items() if k in keep and (v is not None or k == 'state_changed_since_last_attempt')}
            if isinstance(r, dict) and 'skill' in r else r for r in rows]
    while len(rows) > 1 and size(rows) > page_budget.scaled(FAILURE_BYTES):
        rows.pop(0)
    return rows if size(rows) <= page_budget.scaled(FAILURE_BYTES) else bounded(rows, page_budget.scaled(FAILURE_BYTES), 'detail_read')
LIMITS = (2048, 1024, 512, 256, 128)
def limits():
    return page_budget.sizes(LIMITS)


# fix58B (ADR 85, a43: game_facts cut to "@t1" + "more 2" in 232 of 353 renders while world rode at ~1 KB): one limit rung bounds
# blocks by decision relevance. Old world detail and discoveries take the rung itself; recent failures twice it; the
# short native game facts (rest/pass-out time, how fishing works) four times it, so they are the last to shrink.
SCALE = {'world': 1, 'discoveries': 1, 'recent_failures': 2, 'game_facts': 4}


def block_limit(block, limit):
    return limit * SCALE.get(block, 1)


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf8'))


def bounded(value, limit, where):
    """Shrink the largest collection/text first, with exact remainder counts.

    Keep dictionary fields and at least one list member while possible. The marker
    points to an existing canonical read, never to an invented fact or action.
    """
    result = copy.deepcopy(value)
    while size(result) > limit:
        candidates = []

        def walk(node, parent, key):
            if isinstance(node, list):
                rows = node[:-1] if node and isinstance(node[-1], dict) and node[-1].get('where') == where and 'more' in node[-1] else node
                # fix58B (ADR 85): a list of fact lines keeps every line while any line can still be shortened
                # (a43 game_facts kept one long event line and lost the pass-out and fishing lines).
                lines = all(isinstance(r, str) or (isinstance(r, dict) and set(r) == {'text', 'more_chars', 'where'})
                            for r in rows)
                shortenable = lines and any(len(r) > 64 if isinstance(r, str) else len(r['text']) > 32 for r in rows)
                if len(rows) > 1 and not shortenable:
                    candidates.append((size(node), parent, key, rows[:max(1, len(rows) // 2)] + [dict(
                        more=len(rows) - max(1, len(rows) // 2) + (node[-1]['more'] if rows is not node else 0), where=where)]))
                for i, row in enumerate(rows):
                    walk(row, node, i)
            elif isinstance(node, dict):
                if 'more_chars' in node and node.get('where') == where and isinstance(node.get('text'), str):
                    text = node['text']
                    if len(text) > 32:
                        cut = len(text) // 2
                        candidates.append((size(node), parent, key, dict(text=text[:cut],
                            more_chars=node['more_chars'] + len(text) - cut, where=where)))
                    return
                for k, v in node.items():
                    if k not in ('more', 'more_chars', 'where'):
                        walk(v, node, k)
            elif isinstance(node, str) and len(node) > 64:
                cut = len(node) // 2
                candidates.append((size(node), parent, key, dict(text=node[:cut], more_chars=len(node) - cut, where=where)))

        root = [result]
        walk(result, root, 0)
        if not candidates:
            # A single huge row or a map with many scalar fields still has an
            # honest bounded representation, and the full value remains readable.
            count = len(value) if isinstance(value, (dict, list, str)) else 1
            return dict(more=count, where=where)
        _, parent, key, replacement = max(candidates, key=lambda row: row[0])
        parent[key] = replacement
        result = root[0]
    return result


def grounding_summary(block, value, limit, where):
    """ADR 163: measure the place/lock table after detaching its details."""
    if block == 'world' and isinstance(value, dict) and ('places' in value or value.get('catalog_paged')):
        compact = {k: v for k, v in value.items() if k != 'world_details'}
        if size(compact) <= limit:
            return compact
        if isinstance(compact.get('places'), list):
            # Every reachable id survives; descriptive locked-place detail can
            # follow the existing read when the whole table cannot fit.
            reachable = [r[0] for r in compact['places'] if isinstance(r, list) and len(r) >= 2 and r[1] is True]
            locked = [r for r in compact['places'] if isinstance(r, list) and len(r) >= 2 and r[1] is False]
            result = {k: v for k, v in compact.items() if k not in ('places', 'locks', 'legend')}
            result['reachable'] = reachable
            detail = dict(places=locked, locks=compact.get('locks', []))
            result['locked'] = bounded(detail, max(64, limit-size(result)-16), where)
            return result
    return bounded(value, limit, where)


def mark(payload, blocks, where=None):
    previous = payload.get('render_omitted') or {}
    payload['render_omitted'] = dict(previous, blocks=list(dict.fromkeys(list(previous.get('blocks', ())) + blocks)),
                                     truncated=True, **({'where': where} if where else {}))
