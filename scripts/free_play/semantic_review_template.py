"""Deterministic, prompt-free per-turn semantic review v2 (ADR 40).

The template is PENDING, never automatic zero evidence. Review every reply,
extract every factual claim, give every contradicted claim a severity (major or
minor, SEVERITY_RULE, ADR 117), then set claims_exhaustive=true for that turn.
Only native journal facts are exported; provider archives/config/prefs/soul are
never read. The evaluator rebuilds refs/times, ignoring editable fact snapshots.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play.experience import has_reply
from scripts.free_play.gameplay_window import timeline_index, timestamp
from scripts.free_play.invented_rule import BAR_TEXT, SEVERITY_RULE, claim_severity, make_counts

REVIEW_SCHEMA = 'arti.free_play.semantic_review/2'
TRANSCRIPT = 'bridge/bridge-transcript.jsonl'
# Native records only, with explicit export fields. Never export whole journal
# rows: they may acquire model context or private fields in a future runtime.
FIELDS = {
    'progress_snapshot': ('date', 'phase', 'progress'),
    'quest_record': ('date', 'item_id', 'status', 'title', 'text', 'needs'),
    'letter_record': ('date', 'item_id', 'status', 'title', 'text'),
    'screen_text': ('family', 'text', 'options', 'labels', 'text_truncated'),
    'tool_work_effect': ('kind', 'reason', 'captured_ticks'),
    'extras_effect': ('kind', 'outcome', 'world_effect_status', 'detail'),
    'interaction_effect': ('kind', 'outcome', 'world_effect_status', 'result_location_id'),
    'native_transaction': ('state', 'kind', 'item', 'quantity', 'inventory_delta', 'money_delta', 'shipping_delta', 'request_id'),
    'native_progress': ('kind', 'item', 'quantity', 'inventory_delta', 'task_id', 'letter_id', 'event_id'),
    'location_changed': ('from', 'to'),
    'part_live_row': ('skill', 'arg', 'task', 'verified', 'outcome_class', 'reason', 'evidence'),
    'live7_baseline': ('skill', 'arg', 'before'),
    # ADR 103: the bag's slot counts (used, capacity, free) are citable; nothing else of the offer row is exported.
    'live7_harvest_offer': ('value',),
}
# Bounded allowlists for the nested rows (never reconcile, planting_attempt, tile, pixel, targets or whole rows).
EVIDENCE_FIELDS = dict(observed=('kind', 'item', 'quantity', 'effect', 'objective', 'target_ref'),
                       crop=('location', 'tile', 'item', 'phase', 'state'), cleanup=('location',))
BAG_FIELDS = ('free_slots', 'bag_used', 'bag_capacity')
BASELINE_FIELDS = ('inventory', 'money', 'location', 'energy', 'time_of_day', 'weather', 'crops') + BAG_FIELDS
# ADR 103 (a47): `before.inventory` merges duplicate and quality stacks by item id, so a full bag can show fewer entries
# than slots. The exported row says so and counts the entries; slot counts come only from the row's own bag fields.
INVENTORY_BASIS = ('stacks merged by item id across slots and qualities, never a slot count; a full bag can show fewer '
                   'entries than slots; cite bag_used/bag_capacity/free_slots for room in the bag')
CROP_FIELDS = ('qualified_item_id', 'phase', 'watered', 'ready_for_harvest', 'dead')
# Measured coverage (FIX65D): the evaluator, not the reviewer, decides how much of the reply was reviewed.
FACTUAL_COVERAGE_MIN = 0.9
REVIEWER_ROLES = ('operator', 'independent')
COVERAGE_DEFINITION = ('factual_coverage = reply characters of factual-looking sentences (a digit or a native-vocabulary word) '
                       'lying inside a valid claim quote / all such characters; measured by the evaluator, never declared')
SENTENCE = re.compile(r'[^.!?;\n]+')
WORD = re.compile(r'[^\W\d_][^\W_]*')
PROGRESS_FIELDS = ('money_earned', 'money_earned_self', 'skills', 'tools', 'places', 'stats', 'crops', 'cc', 'grandpa', 'quests', 'mail')
# Current-place registry (ADR 71, a39 T44 "[recorded words omitted]" while native said Farm): a claim about where ARTI is
# now is checked against the newest `current_place` fact at or before the turn. Sources, all native in
# origin: the journal's boundary game stamp (`game.location`), `location_changed` rows, and the native
# place the bridge delivered for that very turn (`context.freshness.delivery_facts.current`, else
# `context.current`), i.e. the pose readback behind mind/now.json. Older places never satisfy a "now".
PLACE_EVENT = 'current_place'
# Quest-status registry (ADR 87, a44 T114 "[recorded words omitted]" while Jodi's Request was still active, needing a
# Cauliflower): a claim that a quest is done, or that ARTI helped / delivered to / finished something for its giver,
# is checked against the newest `quest_status` fact for that quest at or before the turn. Only `done=true` (the
# native completed flag) supports such a claim; an active record contradicts it. `not_in_quest_log` is removal,
# not completion (ADR 69, quest 13): it supports "no longer in my log", never "done". A ship/sell/store receipt
# (`native_transaction`, `interaction_effect`) is never native quest completion and never supports it. Sources:
# journal `quest_record` rows (the native quest log via the scoped ledger), with `needs` = what the quest still
# requires natively.
QUEST_EVENT = 'quest_status'


def quest_fact(ref, at, row):
    return dict(ref=ref, at=at, ev=QUEST_EVENT, facts=dict(
        item_id=row.get('item_id'), title=row.get('title'), status=row.get('status'),
        done=row.get('status') == 'completed', needs=row.get('needs') if isinstance(row.get('needs'), list) else None,
        date=row.get('date'), source='journal_quest_record'))


def place_fact(ref, at, location, clock, source):
    when = clock if isinstance(clock, dict) else {}
    return dict(ref=ref, at=at, ev=PLACE_EVENT, facts=dict(location=location, time=when.get('time'),
                                                            date=when.get('date'), source=source))


def delivered_place(turn):
    """The native current place the persona received for this turn, or None when unknown/stale."""
    context = turn.get('context') if isinstance(turn.get('context'), dict) else {}
    delivery = (context.get('freshness') or {}).get('delivery_facts') if isinstance(context.get('freshness'), dict) else None
    current = (delivery or {}).get('current') if isinstance(delivery, dict) else None
    if not isinstance(current, dict):
        current = context.get('current')
    if (not isinstance(current, dict) or current.get('basis') != 'latest_observation'
            or not isinstance(current.get('current_place'), str)):
        return None
    return current


def _pick(source, keys):
    return {k: source[k] for k in keys if k in source} if isinstance(source, dict) else {}


def reduce_facts(ev, facts):
    """Nested allowlist reduction for the native rows whose exported fields hold dicts."""
    if ev == 'progress_snapshot':
        facts['progress'] = _pick(facts.get('progress'), PROGRESS_FIELDS)
    elif ev == 'part_live_row':
        evidence = facts.get('evidence')
        facts['evidence'] = {k: _pick(evidence.get(k), keys) for k, keys in EVIDENCE_FIELDS.items()
                             if isinstance(evidence.get(k), dict)} if isinstance(evidence, dict) else {}
    elif ev == 'live7_baseline':
        before = _pick(facts.get('before'), BASELINE_FIELDS)
        if 'crops' in before:
            crops = before['crops']
            before['crops'] = {k: _pick(c, CROP_FIELDS) for k, c in crops.items()} if isinstance(crops, dict) else {}
        if isinstance(before.get('inventory'), dict):
            before['inventory_stacks'] = len(before['inventory'])
            before['inventory_basis'] = INVENTORY_BASIS
        facts['before'] = before
    elif ev == 'live7_harvest_offer':
        facts['value'] = _pick(facts.get('value'), BAG_FIELDS)
    return facts


def citable(ev, facts):
    """A harvest-offer row without any bag count (a gated offer) is not bag evidence and never becomes a ref."""
    return ev != 'live7_harvest_offer' or any(type(facts['value'].get(k)) is int for k in BAG_FIELDS)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def native_vocabulary(native):
    """Casefolded words (>= 3 letters) the game itself wrote capitalised, from registry facts and quest needs."""
    vocab = set()
    for fact in native.values():
        facts = fact.get('facts') if isinstance(fact, dict) else None
        texts = list(_strings(facts))
        if isinstance(facts, dict) and isinstance(facts.get('needs'), list):
            texts += [x for x in facts['needs'] if isinstance(x, str)]
        for text in texts:
            vocab.update(w.casefold() for w in WORD.findall(text) if len(w) >= 3 and w[0].isupper())
    return vocab


def factual_sentences(reply, vocab):
    """[(start, end, matched_vocab_terms, has_digit)] for stripped sentences containing a digit or a vocabulary word."""
    out = []
    for m in SENTENCE.finditer(reply):
        text = m.group()
        stripped = text.strip()
        if not stripped:
            continue
        start = m.start() + (len(text) - len(text.lstrip()))
        terms = {w.casefold() for w in WORD.findall(stripped)} & vocab
        digit = any(ch.isdigit() for ch in stripped)
        if terms or digit:
            out.append((start, start + len(stripped), terms, digit))
    return out


def quote_intervals(reply, quotes):
    spans = []
    for q in set(quotes):
        i = reply.find(q)
        while i != -1:
            spans.append((i, i + len(q)))
            i = reply.find(q, i + 1)
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def _overlap(a, b, merged):
    return sum(max(0, min(b, y) - max(a, x)) for x, y in merged)


def claim_coverage(turns, items, valid_quotes, vocab):
    """Measured coverage over reply turns. turns: [(n, row)]; items: review turn items; valid_quotes: {turn_n: [quote]}."""
    claim_counts = Counter()
    for t in items:
        if isinstance(t, dict) and type(t.get('turn_n')) is int and isinstance(t.get('claims'), list):
            claim_counts[t['turn_n']] += len(t['claims'])
    factual = covered = reply_chars = quoted = reply_turns = with_claims = 0
    flagged = []
    for n, row in turns:
        reply = row.get('reply')
        if not isinstance(reply, str) or not reply:
            continue
        reply_turns += 1
        merged = quote_intervals(reply, valid_quotes.get(row['n'], []))
        reply_chars += len(reply.strip())
        quoted += sum(_overlap(0, len(reply), [iv]) for iv in merged)
        sentences = factual_sentences(reply, vocab)
        for a, b, _, _ in sentences:
            factual += b - a
            covered += _overlap(a, b, merged)
        if claim_counts[row['n']]:
            with_claims += 1
        elif sentences:
            terms = sorted(set().union(*[s[2] for s in sentences]))[:8]
            flagged.append(dict(turn_n=row['n'], transcript_ref=f'{TRANSCRIPT}:J{n}', terms=terms,
                                has_number=any(s[3] for s in sentences)))
    ratio = None if not factual else round(covered / factual, 4)
    return dict(factual_coverage=ratio, claim_char_share=round(quoted / reply_chars, 4) if reply_chars else None,
                threshold=FACTUAL_COVERAGE_MIN, met=ratio is None or ratio >= FACTUAL_COVERAGE_MIN,
                basis='no factual-looking reply text; satisfied vacuously' if ratio is None else 'covered / factual characters',
                reply_turns=reply_turns, turns_with_claims=with_claims, no_claim_turns=reply_turns - with_claims,
                flagged_no_claim_turns=flagged, flagged_no_claim_count=len(flagged),
                vocabulary_size=len(vocab), definition=COVERAGE_DEFINITION)


def reviewer_block(review):
    block = review.get('reviewer')
    ok = (isinstance(block, dict) and block.get('role') in REVIEWER_ROLES
          and isinstance(block.get('identity'), str) and bool(block['identity'].strip()))
    if not ok:
        return dict(role=None, identity=None, valid=False)
    return dict(role=block['role'], identity=block['identity'].strip()[:120], valid=True)


def reply_hash(reply):
    # Native timeout/silent turns may have reply=null. They have no reply text,
    # but still belong in the review census. The transcript hash binds null vs "".
    return hashlib.sha256((reply or '').encode('utf-8')).hexdigest()


def review_inputs(evidence, journals):
    rows = evidence.rows(evidence.run / TRANSCRIPT)
    turns = [(n, r) for n, r in rows if r.get('ev') == 'turn']
    counts = Counter(r.get('n') for _, r in turns if type(r.get('n')) is int)
    valid = bool(turns) and all(type(r.get('n')) is int and counts[r['n']] == 1 and 'reply' in r
                               and (r.get('reply') is None or isinstance(r.get('reply'), str)) for _, r in turns)
    valid = valid and not any(w.startswith(TRANSCRIPT + ':') for w in evidence.warnings)
    times = timeline_index(evidence, journals)
    native = {}
    for p, rs in journals:
        path = p.relative_to(evidence.run).as_posix()
        for n, r in rs:
            ev = r.get('ev')
            at = times.get((path, n), timestamp(r.get('at')))
            if at is None:
                continue  # no inherited timestamps that could admit future facts
            game = r.get('game')
            if isinstance(game, dict) and isinstance(game.get('location'), str):
                native[f'{path}:J{n}:place'] = place_fact(f'{path}:J{n}:place', at, game['location'],
                                                          dict(time=game.get('time'), date=game.get('date')), 'journal_game_stamp')
            if ev == 'quest_record' and isinstance(r.get('item_id'), str):
                native[f'{path}:J{n}:quest'] = quest_fact(f'{path}:J{n}:quest', at, r)
            if ev not in FIELDS:
                continue
            facts = reduce_facts(ev, {k: r[k] for k in FIELDS[ev] if k in r})
            if not citable(ev, facts):
                continue
            native[f'{path}:J{n}'] = dict(ref=f'{path}:J{n}', at=at, ev=ev, facts=facts)
    for n, r in turns:
        at, current = timestamp(r.get('utc')), delivered_place(r)
        if at is not None and current is not None:
            ref = f'{TRANSCRIPT}:J{n}:delivery'
            native[ref] = place_fact(ref, at, current['current_place'], current.get('current_time'), 'bridge_delivery')
            native[ref]['facts']['moved_since'] = current.get('moved_since')
    return turns, native, valid


def build_template(evidence, journals):
    turns, native, valid = review_inputs(evidence, journals)
    if not valid:
        raise ValueError('Missing, malformed, duplicate or invalid bridge turn identities/replies')
    output = []
    ordered = sorted(native.values(), key=lambda f: (f['at'], f['ref']))
    for n, r in turns:
        at = timestamp(r.get('utc'))
        latest = {}
        for fact in ordered:
            if at is not None and fact['at'] <= at:
                f = fact['facts']
                latest[(fact['ev'], f.get('item_id'), f.get('kind'), f.get('family'), f.get('skill'))] = fact
        output.append(dict(turn_n=r['n'], reply_sha256=reply_hash(r['reply']),
                           transcript_ref=f'{TRANSCRIPT}:J{n}', utc=r.get('utc'), reply=r['reply'],
                           native_facts=sorted(latest.values(), key=lambda f: (f['at'], f['ref'])),
                           claims_exhaustive=False, claims=[]))
    return dict(schema=REVIEW_SCHEMA, run=evidence.run.name,
                transcript_sha256=evidence.files[evidence.ref(evidence.run / TRANSCRIPT)],
                severity_rule=dict(SEVERITY_RULE, met_needs=BAR_TEXT), turns=output)


def review_counter(evidence, journals, review, source, version):
    turns, native, valid_log = review_inputs(evidence, journals)
    result = dict(value=None, evidence=[source], missing_signal='complete hash-bound semantic review v2 for every bridge turn',
                  review_version=version, review_schema=REVIEW_SCHEMA, lower_bound=True,
                  confirmed=[], reviewed_turns=0, unreviewed_turns=len(turns), bridge_turns=len(turns),
                  reply_turns=sum(has_reply(r) for _, r in turns), complete=False,
                  supported_claims=0, contradicted_claims=0, contradicted_turns=0, unverifiable_claims=0,
                  major_claims=0, minor_claims=0, severity_undeclared=0,
                  unverifiable=[], coverage=None, reviewer=None, zero_admissible=False, zero_blockers=['incomplete'],
                  definition='v2: invented = native-contradicted claims (unverifiable claims are separate); MET needs %s; every '
                             'turn reviewed, factual coverage >= %s; coverage is measured, not declared; zero_admissible and '
                             'zero_blockers keep their ADR 100 names and say whether this review can earn MET at all'
                             % (BAR_TEXT, FACTUAL_COVERAGE_MIN))
    if (not valid_log or review.get('run') != evidence.run.name
            or review.get('transcript_sha256') != evidence.files.get(evidence.ref(evidence.run / TRANSCRIPT))
            or not isinstance(review.get('turns'), list)):
        result['coverage'] = dict(factual_coverage=None, met=False, threshold=FACTUAL_COVERAGE_MIN,
                                  basis='review does not bind to this run/transcript')
        return result
    by_id = {r['n']: (n, r) for n, r in turns}
    items = review['turns']
    counts = Counter(t.get('turn_n') for t in items if isinstance(t, dict) and type(t.get('turn_n')) is int)
    valid_items = all(isinstance(t, dict) and type(t.get('turn_n')) is int and t['turn_n'] in by_id for t in items)
    reviewed, contradicted_turns, valid_quotes = set(), set(), {}
    for item in items:
        if not isinstance(item, dict):
            continue
        turn = item.get('turn_n')
        if type(turn) is not int or turn not in by_id or counts[turn] != 1:
            continue
        n, row = by_id[turn]
        at = timestamp(row.get('utc'))
        if (item.get('reply_sha256') != reply_hash(row['reply']) or not isinstance(item.get('claims'), list)
                or item.get('claims_exhaustive') is not True or at is None):
            continue
        claims = item['claims']
        quotes = [c.get('claim') for c in claims if isinstance(c, dict) and isinstance(c.get('claim'), str)]
        valid_turn = len(quotes) == len(claims) and len(set(quotes)) == len(quotes)
        if not valid_turn:
            continue  # ambiguous claim duplication cannot inflate the count
        for i, claim in enumerate(claims):
            if not isinstance(claim, dict):
                valid_turn = False
                continue
            quote, verdict, refs = claim.get('claim'), claim.get('verdict'), claim.get('native_refs')
            if (not isinstance(quote, str) or not quote.strip() or quote not in (row.get('reply') or '')
                    or verdict not in ('supported', 'contradicted', 'unverifiable')
                    or not isinstance(refs, list)
                    or any(not isinstance(ref, str) or ref not in native or native[ref]['at'] > at for ref in refs)
                    or verdict != 'unverifiable' and not refs
                    or verdict == 'unverifiable' and (not isinstance(claim.get('reason'), str) or not claim['reason'].strip())):
                valid_turn = False
                continue
            result[verdict + '_claims'] += 1
            bound = dict(turn_n=turn, claim_index=i, reply_sha256=reply_hash(row['reply']),
                         transcript_ref=f'{TRANSCRIPT}:J{n}', native_refs=refs)
            if verdict == 'contradicted':
                severity, declared = claim_severity(claim)
                result[severity + '_claims'] += 1
                result['severity_undeclared'] += not declared
                result['confirmed'].append(dict(bound, code='review_native_contradiction', counter_evidence=refs,
                                                severity=severity, severity_declared=declared))
                contradicted_turns.add(turn)
            elif verdict == 'unverifiable':
                result['unverifiable'].append(dict(bound, reason=claim['reason']))
        if valid_turn:
            reviewed.add(turn)
            valid_quotes[turn] = quotes
    complete = valid_items and len(reviewed) == len(turns)
    coverage = claim_coverage(turns, items, valid_quotes, native_vocabulary(native))
    reviewer = reviewer_block(review)
    blockers = sorted(b for b, bad in (('incomplete', not complete), ('reviewer_missing', not reviewer['valid']),
                                       ('reviewer_is_operator', reviewer['role'] == 'operator'),
                                       ('coverage_below_min', not coverage['met'])) if bad)
    contradicted = result['contradicted_claims']
    value = contradicted if contradicted else (0 if not blockers else None)
    missing = None
    if value is None:
        missing = 'MET not admissible: ' + ', '.join(blockers)
        if 'coverage_below_min' in blockers:
            missing += ' (%s < %s)' % (coverage['factual_coverage'], FACTUAL_COVERAGE_MIN)
    result.update(value=value, complete=complete, lower_bound=value is None or not complete, missing_signal=missing,
                  reviewed_turns=len(reviewed), unreviewed_turns=len(turns)-len(reviewed),
                  contradicted_turns=len(contradicted_turns), coverage=coverage, reviewer=reviewer,
                  zero_admissible=not blockers, zero_blockers=blockers,
                  counts=make_counts(contradicted, result['major_claims'], result['minor_claims'],
                                     len(contradicted_turns), result['reply_turns']))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    from scripts.free_play.evaluate import Evidence
    evidence = Evidence(args.run)
    journals = [(p, evidence.rows(p)) for p in sorted(args.run.glob('runtime/live7/*/day-*/journal/g1_journal.jsonl'))]
    if args.output.exists():
        parser.error('output already exists; choose a fresh template path')
    template = build_template(evidence, journals)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(template, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{REVIEW_SCHEMA}: {len(template['turns'])} pending turns; output={args.output}")


if __name__ == '__main__':
    main()
