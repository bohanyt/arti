"""Frozen PM-1 stop-gate measurements for evaluator 1.3.8. Offline evidence only.

Counters are observations, not inferred success from intent, disappearance, or menu
changes. Positive event-only counts are lower bounds; absent instrumentation is
UNKNOWN. In particular mail read != letter task, bundle completion != item deposit,
team income != own income, and a partial semantic review cannot earn MET. The invented-facts
bar is scripts/free_play/invented_rule.py (ADR 117).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from scripts.free_play.invented_rule import invented_verdict

VERSION = '1.7.0'
REVIEW_SCHEMA = 'arti.free_play.semantic_review/1'


def measure(value=None, refs=(), *, missing=None, **details):
    return dict(value=value, evidence=list(dict.fromkeys(refs)),
                missing_signal=missing if value is None else None, **details)


def indexed(evidence, journals):
    """Only authorized native journal rows; never resolve arbitrary review paths."""
    out = []
    for path, rows in journals:
        date, at = None, None
        for n, row in rows:
            # Other journal domains encode date as arrays or colon strings.
            # The progress snapshot is the canonical native game-date anchor.
            if row.get('ev') == 'progress_snapshot':
                date = row.get('date') or date
            if isinstance(row.get('at'), (int, float)):
                at = row['at']
            out.append((f'{path.relative_to(evidence.run).as_posix()}:J{n}', row, date, at))
    return out


def native_progress(evidence, journals, native_text):
    rows = indexed(evidence, journals)
    from scripts.free_play.native_receipts import progress_receipts
    receipts, zero, coverage_refs, native_requests, window = progress_receipts(evidence, rows, native_text, journals)

    def observed(field, legacy=(), legacy_refs=(), missing='native progress receipt'):
        events = receipts[field]
        refs = [ref for ref, _ in events.values()] + list(legacy_refs)
        value = len(events) + len(legacy) if events or legacy else 0 if zero[field] else None
        return measure(value, refs or (coverage_refs if zero[field] else []), missing=missing,
                       lower_bound=not zero[field], coverage_complete=bool(zero[field]), coverage_window=window)
    snaps = [(ref, r.get('progress')) for ref, r, _, _ in rows
             if r.get('ev') == 'progress_snapshot']

    def delta(*keys):
        vals = []
        for ref, p in snaps:
            for k in keys:
                p = p.get(k) if isinstance(p, dict) else None
            vals.append((ref, p))
        if len(vals) >= 2 and all(type(v) is int for _, v in (vals[0], vals[-1])):
            a, b = vals[0], vals[-1]
            if b[1] >= a[1]:
                return measure(b[1] - a[1], [a[0], b[0]], baseline=a[1], end=b[1], source='native snapshots')
        if keys == ('stats', 'individual_money_earned'):
            from scripts.free_play.snapshot_endpoint import fallback_endpoint
            endpoint = fallback_endpoint(evidence, journals, window, keys, lambda v: type(v) is int and v >= 0, native_text)
            if endpoint and endpoint['value'] >= vals[0][1]:
                return measure(endpoint['value'] - vals[0][1], [vals[0][0], endpoint['ref']],
                    baseline=vals[0][1], end=endpoint['value'], source='native snapshots',
                    endpoint_fallback=True, endpoint_snapshot=endpoint, lower_bound=endpoint.get('lower_bound', False))
        return measure(missing='first and last native progress_snapshot.' + '.'.join(keys))

    seen, completed, quest_refs, letter_tasks, letter_refs = {}, set(), [], set(), []
    for ref, r, _, _ in rows:
        if r.get('ev') != 'quest_record' or not isinstance(r.get('item_id'), str):
            continue
        key, status = r['item_id'], r.get('status')
        # A first-seen completed flag may predate this run. Disappearance is not completion.
        if status == 'completed' and seen.get(key, (None,))[0] == 'active' and key not in completed:
            completed.add(key)
            quest_refs += [seen[key][1], ref]
            if (r.get('facts') or {}).get('letter_id'):
                letter_tasks.add(key)
                letter_refs += [seen[key][1], ref]
        seen[key] = (status, ref)

    # Native BundleDonation.Observed proves a single persistent ingredient slot
    # and paired cursor decrement. UiPressApplied/changed alone does not.
    deposits = {}
    for n, line in enumerate(native_text.splitlines(), 1):
        m = re.search(r'arti_command ui_press VERDICT request_id=(\S+) outcome=UiPressApplied '
                      r'effect=changed reason=bundle:donated(?:\s|$)', line)
        if m:
            if m[1] not in native_requests:
                deposits.setdefault(m[1], f'farmhand-SMAPI.txt:L{n}')

    # Generic UI and skill paths mirror the same economy event. Request IDs
    # deduplicate them, including mirrors of finish_verified.
    transactions = {}
    for ref, r, _, _ in rows:
        events = []
        if r.get('ev') == 'native_transaction' and r.get('state') == 'completed' and r.get('source') == 'generic_ui_native_readback':
            events = [r]
        if r.get('ev') == 'finish_verified' and (r.get('verdict') or {}).get('status') == 'achieved':
            events = ((r['verdict'].get('observed') or {}).get('events') or [])
        # ADR 142: close_shipping writes net native bin transfers in actual_shipped,
        # including partial objectives. The deposit is a receipt, never a gold estimate.
        verdict = r.get('verdict') or {}
        if (r.get('ev') == 'finish_verified' and verdict.get('status') in ('achieved', 'partial')
                and verdict.get('reason') in ('observed_ship', 'observed_ship_partial')):
            actual = (verdict.get('observed') or {}).get('actual_shipped')
            if isinstance(actual, list):
                events = list(events) + [dict(e, kind='ship', money_delta=0) for e in actual
                                        if isinstance(e, dict) and e.get('request_id')]
        for i, event in enumerate(events):
            if not isinstance(event, dict):
                continue
            kind, rid = event.get('kind'), event.get('request_id')
            qty, money = event.get('quantity'), event.get('money_delta', 0 if kind == 'ship' else None)
            if not rid and event is not r and event.get('container_token') and event.get('source_fact_id') and r.get('goal_id'):
                rid = f"{r['goal_id']}:{i}"
            if (kind in ('ship', 'sell') and isinstance(rid, str) and rid
                    and isinstance(event.get('item'), str) and type(qty) is int and qty > 0
                    and type(money) is int and (kind == 'ship' and money == 0 or kind == 'sell' and money > 0)):
                if event is r and (r.get('inventory_delta') != -qty or kind == 'ship' and r.get('shipping_delta') != qty):
                    continue
                transactions.setdefault((kind, str(Path(ref).parent), rid), (event, ref))
    sales = [(e, ref) for (k, _, _), (e, ref) in transactions.items() if k == 'sell']
    ships = [(e, ref) for (k, _, _), (e, ref) in transactions.items() if k == 'ship']
    # A zero flag-transition count alone remains a lower bound. For the combined
    # quest/letter gate, require observed quest flags, zero native reward-stat gain
    # AND complete letter-task instrumentation, including its terminal interval.
    quest_stat = delta('stats', 'quests_completed')
    quest_zero = bool(seen) and not completed and zero['letter_tasks_completed'] and quest_stat['value'] == 0
    if quest_zero:
        quest_refs = [r for _, r in seen.values()] + quest_stat['evidence'] + coverage_refs
    skill = measure(missing='first and last native progress_snapshot.skills for every skill')
    if len(snaps) >= 2:
        a, b = (p.get('skills') if isinstance(p, dict) else None for _, p in (snaps[0], snaps[-1]))
        if (isinstance(a, dict) and a and isinstance(b, dict) and a.keys() == b.keys()
                and all(type(a[k]) is int and type(b[k]) is int for k in a)):
            skill = measure(sum(max(0, b[k] - a[k]) for k in a), [snaps[0][0], snaps[-1][0]],
                            baseline=a, end=b, source='native snapshots')
        elif isinstance(a, dict) and a and all(type(v) is int and v >= 0 for v in a.values()):
            from scripts.free_play.snapshot_endpoint import fallback_endpoint
            valid = lambda v: (isinstance(v, dict) and v.keys() == a.keys()
                               and all(type(x) is int and x >= 0 for x in v.values()))
            endpoint = fallback_endpoint(evidence, journals, window, ('skills',), valid, native_text)
            if endpoint:
                b = endpoint['value']
                skill = measure(sum(max(0, b[k] - a[k]) for k in a), [snaps[0][0], endpoint['ref']],
                    baseline=a, end=b, source='native snapshots', endpoint_fallback=True, endpoint_snapshot=endpoint,
                    lower_bound=endpoint.get('lower_bound', False))
    return dict(
        version=VERSION,
        quests_native_completed=measure(len(completed) if seen else None, quest_refs,
            missing='native quest_record active -> completed flag transition', ids=sorted(completed),
            lower_bound=not quest_zero, coverage_complete=bool(quest_zero),
            definition='unique quest IDs changing active -> completed; excludes preexisting flags and disappearance; zero additionally requires zero quest-stat gain and complete native letter-task coverage'),
        quests_stat_gain=quest_stat,
        letter_tasks_completed=observed('letter_tasks_completed', letter_tasks - {
            r['task_id'] for _, r in receipts['letter_tasks_completed'].values() if r['task_kind'] == 'quest'}, letter_refs,
            missing='letter-linked task ID and native completion flag/event (quest_record facts.letter_id); reading mail is not completion',
            ),
        letters_read_gain=delta('mail', 'read'),
        cc_bundle_deposits=observed('cc_bundle_deposits', deposits, deposits.values(),
            missing='native ui_press VERDICT UiPressApplied effect=changed reason=bundle:donated (persistent ingredient flag + paired cursor decrement)',
            ),
        museum_donations=observed('museum_donations', missing='native paired museumPieces addition + actor inventory/cursor decrement, bound to request_id; MuseumMenu/UiPressApplied alone is insufficient'),
        own_money_earned=delta('stats', 'individual_money_earned'),
        team_money_earned=delta('money_earned'),
        own_sales_gold=measure(sum(e['money_delta'] for e, _ in sales), [ref for _, ref in sales], lower_bound=True,
            definition='positive native sale money deltas only; no baseline, gifts, team income, or shipping price estimates'),
        own_shipments=measure(len(ships), [ref for _, ref in ships], lower_bound=True,
            definition='native ship events; gold arrives next morning, not at deposit'),
        skill_level_gain=skill)


def semantic_counter(evidence, journals, review_path=None):
    """Count distinct contradicted reply turns, never an unbound aggregate.

    Structured reviews bind every reply by SHA256; each falsifiable claim must
    name native_refs in the indexed journals. Empty claims means explicitly
    reviewed/no falsifiable claims. Zero requires all nonempty reply turns.
    Legacy a23/a24 adapters credit only exact replies with native corroboration
    under the two frozen rubrics below; they can establish a positive minimum,
    never zero or exhaustive semantic coverage.
    """
    rows = indexed(evidence, journals)
    native_events = {'progress_snapshot', 'quest_record', 'letter_record', 'screen_text',
                     'tool_work_effect', 'extras_effect', 'interaction_effect', 'native_transaction'}
    native = {ref: r for ref, r, _, _ in rows if r.get('ev') in native_events
              or r.get('ev') in ('container_transfer', 'ui_press', 'ui_key_result') and r.get('verified') is True}
    transcript = evidence.rows(evidence.run / 'bridge/bridge-transcript.jsonl')
    turns = {r.get('n'): (n, r) for n, r in transcript
             if r.get('ev') == 'turn' and type(r.get('n')) is int and r.get('reply')}
    unknown = dict(value=None, evidence=[], missing_signal='machine-readable exact-reply semantic review with native counter-evidence',
                   review_version=VERSION, lower_bound=True, confirmed=[], reviewed_turns=0,
                   unreviewed_turns=len(turns), reply_turns=len(turns), complete=False)
    if review_path is None:
        paths = sorted({p for parent in (evidence.run, evidence.run / 'bridge') for p in parent.glob('semantic-review*.json')})
        if len(paths) != 1:
            return unknown
        review_path = paths[0]
    review_path = Path(review_path)
    evidence.track(review_path)
    try:
        from arti_runtime import ipc
        review = json.loads(ipc.read_text(review_path, encoding='utf-8-sig'))
    except (OSError, ValueError):
        evidence.warnings.append('Semantic review missing or malformed; no semantic credit')
        return unknown
    if not isinstance(review, dict):
        return unknown
    source = evidence.ref(review_path)
    from scripts.free_play.semantic_review_template import REVIEW_SCHEMA as V2, review_counter
    if review.get('schema') == V2:
        return review_counter(evidence, journals, review, source, VERSION)
    confirmed, reviewed = {}, set()

    def add(turn, code, refs):
        n, row = turns[turn]
        confirmed.setdefault(turn, dict(turn_n=turn, code=code,
            reply_sha256=hashlib.sha256(row['reply'].encode('utf-8')).hexdigest(),
            transcript_ref=f'bridge/bridge-transcript.jsonl:J{n}', counter_evidence=refs))

    if review.get('schema') == REVIEW_SCHEMA and review.get('run') == evidence.run.name:
        items = review.get('turns')
        if not isinstance(items, list):
            return unknown
        # Duplicate turn reviews are ambiguous, rather than last-one-wins.
        counts = {i: sum(isinstance(t, dict) and t.get('turn_n') == i for t in items) for i in turns}
        for item in items:
            if not isinstance(item, dict):
                continue
            turn = item.get('turn_n')
            if type(turn) is not int or turn not in turns or counts[turn] != 1:
                continue
            row = turns[turn][1]
            claims = item.get('claims')
            if item.get('reply_sha256') != hashlib.sha256(row['reply'].encode('utf-8')).hexdigest() or not isinstance(claims, list):
                continue
            valid = True
            for c in claims:
                refs = c.get('native_refs') if isinstance(c, dict) else None
                if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in native for ref in refs)
                        or c.get('verdict') not in ('invented', 'supported')
                        or not isinstance(c.get('claim'), str) or not c['claim'] or c['claim'] not in row['reply']):
                    valid = False
                    continue
                if c['verdict'] == 'invented':
                    add(turn, 'review_native_contradiction', refs)
            if valid:
                reviewed.add(turn)
    elif 'schema' not in review:
        # Frozen legacy semantics: vanilla Egg Festival Spring 13, only when
        # native spring_12 invitation and current native date corroborate the
        # review; refill claims require a same-date empty-can native signal and
        # no completed refill before the reply. Aggregate counts/intent ratings ignored.
        candidates = []
        def entries(key):
            value = review.get(key)
            return value if isinstance(value, list) else []

        exact = {r.get('turn', r.get('turn_n')): r.get('reply') for r in entries('probes')
                 if isinstance(r, dict) and type(r.get('turn', r.get('turn_n'))) is int}
        for c in entries('explicit_temporal_contradictions'):
            if isinstance(c, dict):
                candidates.append((c.get('turn'), 'past_egg_festival', c.get('reply')))
        candidates += [(t, 'past_egg_festival', exact.get(t)) for t in entries('definite_temporal_false_turns') if type(t) is int]
        candidates += [(t, 'premature_refill', exact.get(t)) for t in entries('definite_false_refill_turns') if type(t) is int]
        for turn, code, reply in candidates:
            if type(turn) is not int or turn not in turns or turns[turn][1]['reply'] != reply:
                continue
            row = turns[turn][1]
            date = ((row.get('now') or {}).get('game_time') or {}).get('date')
            dates = [ref for ref, r, _, _ in rows if r.get('ev') == 'progress_snapshot' and r.get('date') == date]
            refs = []
            if code == 'past_egg_festival':
                m = re.fullmatch(r'Y\d+ spring (\d+)', str(date))
                invites = [ref for ref, r, _, _ in rows if r.get('ev') == 'letter_record' and r.get('item_id') == 'spring_12']
                if m and int(m[1]) > 13 and dates and invites:
                    refs = [dates[0], invites[0]]
            else:
                from datetime import datetime
                try:
                    at = datetime.fromisoformat(row['utc'].replace('Z', '+00:00')).timestamp()
                except (ValueError, KeyError, TypeError):
                    continue
                empty = [(ref, ts) for ref, r, d, ts in rows if (r.get('ev') == 'tool_work_refill_needed'
                         or r.get('ev') == 'tool_work_pinned' and r.get('kind') == 'refill_can')
                         and d == date and ts is not None and ts <= at]
                refills = [(ref, ts) for ref, r, _, ts in rows if r.get('ev') == 'tool_work_effect'
                           and r.get('kind') == 'refill_can' and ts is not None]
                if empty and not any(empty[-1][1] <= ts <= at for _, ts in refills):
                    refs = [empty[-1][0], *[ref for ref, ts in refills if ts > at][:1]]
            if refs:
                add(turn, code, refs)
    complete = bool(turns) and len(reviewed) == len(turns)
    value = len(confirmed) if confirmed or complete else None
    return dict(value=value, evidence=[source, *[x['transcript_ref'] for x in confirmed.values()]],
                missing_signal=None if value is not None else unknown['missing_signal'],
                review_version=VERSION, review_schema=review.get('schema', 'legacy-a23-a24/1'),
                lower_bound=not complete, confirmed=sorted(confirmed.values(), key=lambda x: x['turn_n']),
                reviewed_turns=len(reviewed), unreviewed_turns=len(turns)-len(reviewed), reply_turns=len(turns), complete=complete,
                definition='distinct exact-reply turns with falsifiable claims contradicted by native evidence; partial reviews cannot establish zero')


def goal_gate(endurance, progress, semantic, *, metrics=None, metrics_config=None):
    def criterion(field, threshold=1):
        v = field['value']
        return dict(field, threshold=threshold,
                    status='UNKNOWN' if v is None
                    else 'MET' if v >= threshold else 'UNKNOWN' if field.get('lower_bound') else 'NOT_MET')

    def either(*fields):
        known = [f for f in fields if f['value'] is not None]
        # Overlapping quest/letter counts must not be summed; one completion suffices.
        value = max((f['value'] for f in known), default=None)
        return measure(value, [ref for f in fields for ref in f['evidence']],
                       lower_bound=not all(f['value'] is not None and not f.get('lower_bound') for f in fields),
                       missing=' OR '.join(f['missing_signal'] or '' for f in fields))

    own = progress['own_money_earned']
    own = dict(own, evidence=own['evidence'] + progress['own_shipments']['evidence'] + progress['own_sales_gold']['evidence'])
    if own['value'] is not None and own['value'] > 0 and not (progress['own_shipments']['value'] or progress['own_sales_gold']['value']):
        own = measure(missing='own native shipping/sale receipt to corroborate individualMoneyEarned gain')
    criteria = dict(
        active_minutes=criterion(measure(endurance['active_minutes'], ['window.json', 'timeline.jsonl'],
            missing='ARMED-to-stop span and semantic-progress timeline', definition=endurance['active_basis']), 180),
        skill_level_gain=criterion(progress['skill_level_gain']),
        quest_or_letter_completed=criterion(either(progress['quests_native_completed'], progress['quests_stat_gain'], progress['letter_tasks_completed'])),
        bundle_item_or_donation=criterion(either(progress['cc_bundle_deposits'], progress['museum_donations'])),
        own_money_earned=criterion(own),
        invented_facts=dict(semantic, **invented_verdict(semantic)))
    from scripts.free_play.session_metrics import configured_criteria
    measured = configured_criteria(metrics or {}, metrics_config)
    statuses = {c['status'] for c in criteria.values()}
    if measured['enabled']:
        statuses.update(c['status'] for c in measured['criteria'].values())
    return dict(version=VERSION, status='NOT_MET' if 'NOT_MET' in statuses else 'UNKNOWN' if 'UNKNOWN' in statuses else 'MET',
                criteria=criteria, measured_criteria=measured,
                definition='all six criteria required; any NOT_MET fails, otherwise missing evidence is UNKNOWN; no receipt/override input; tempo/social/variety disabled unless explicitly configured')
