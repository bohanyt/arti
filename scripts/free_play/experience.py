"""Live-experience metrics from the bridge transcript only (evaluator 1.4.1, ADR 75; judge/owner inputs ADR 92).

Report-only, automatic part of the live experience gate
(`runbooks/design/LIVE_EXPERIENCE_GATE_DESIGN_2026-11-03.md`, LX-5/9/10/11/12).
It reads `bridge/bridge-transcript.jsonl` rows written by `arti_bridge_testmode.Transcript`
and `arti_discovery_inbox.DiscoveryInbox`, and uses only fields those writers emit:
`turn` (`n`, `source`, `reply`, `silent`, `emotion`, `provider_source`, `latency_ms`,
`queued_wait_s`, `shown_discovery_ids`, `t`, `utc`), `discovery_context` (`event.id`,
`event.importance`), `discovery_delivery` (`event_ids`, `status`), `game_trigger_dropped`,
`turn_skipped`, `turn_unanswered_at_end`, `test_start`, `test_end`.

Generic text statistics only: no persona, preference, viewer or prompt text is read, and
nothing here steers the persona. Missing evidence is None (UNKNOWN), never zero.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
import re
import statistics

from scripts.free_play.invented_rule import INVENTED_MAJOR_MAX, INVENTED_TURN_RATE_BELOW, THRESHOLD, invented_verdict

VERSION = '1.7.0'
TRANSCRIPT = 'bridge/bridge-transcript.jsonl'
# Design §2 note: spontaneous = these sources with a non-empty reply; owner probes never pad LX-8/10/11.
SPONTANEOUS_SOURCES = ('discovery', 'game', 'initiative', 'moment')
DELIVERED = ('reacted', 'persona_silent')
SYSTEMATIC = 10         # owner page: systematic samples per session (journey.persona)
OWNER_UP_MIN, OWNER_DOWN_MAX = 7, 1   # design choice 1 (a), the recommended default, until the owner decides
MIN_REPLIES = 10        # share metrics (LX-9, LX-10) below this are UNKNOWN, not a verdict
MIN_SPAN_S = 600        # rate and gap metrics (LX-11) need at least ten transcript minutes
GAP_LONG_S = 900        # "silence over 15 min"
NEAR_DUP_WINDOW = 20
NEAR_DUP_JACCARD = 0.6
NGRAM = 4
_WORD = re.compile(r'[^\w]+', re.UNICODE)


def when(row):
    """Row time in epoch seconds: numeric `t`, else ISO `utc`; None when neither parses."""
    t = row.get('t')
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return float(t)
    utc = row.get('utc')
    if isinstance(utc, str):
        try:
            return datetime.fromisoformat(utc.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def percentile(values, q):
    """Nearest-rank percentile (q in 0..1); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def normalize(text):
    return [w for w in _WORD.split(str(text).casefold()) if w]


def opener(text, words=2):
    tokens = normalize(text)
    return ' '.join(tokens[:words]) if tokens else ''


def ngrams(text, n=NGRAM):
    flat = ' '.join(normalize(text))
    return {flat[i:i + n] for i in range(max(0, len(flat) - n + 1))} if flat else set()


def jaccard(a, b):
    return len(a & b) / len(a | b) if a or b else 0.0


def has_reply(row):
    """A persona reply she actually gave: non-empty text that is not the unavailable notice."""
    reply = row.get('reply')
    return (isinstance(reply, str) and bool(reply.strip())
            and not str(row.get('provider_source') or '').startswith('unavailable:'))


def _measure(value=None, refs=(), *, missing=None, **details):
    return dict(value=value, evidence=list(dict.fromkeys(refs)),
                missing_signal=missing if value is None else None, **details)


def experience_metrics(rows):
    """Transcript-only metrics. `rows` is `[(line_no, row), ...]` from `Evidence.rows`."""
    rows = [(n, r) for n, r in rows if isinstance(r, dict)]
    ref = lambda n: f'{TRANSCRIPT}:J{n}'  # noqa: E731
    turns = [(n, r) for n, r in rows if r.get('ev') == 'turn']
    replies = [(n, r) for n, r in turns if has_reply(r)]
    spontaneous = [(n, r) for n, r in replies if r.get('source') in SPONTANEOUS_SOURCES]
    starts = [when(r) for _, r in rows if r.get('ev') == 'test_start']
    ends = [when(r) for _, r in rows if r.get('ev') == 'test_end']
    stamps = [when(r) for _, r in rows if when(r) is not None]
    start = next((s for s in starts if s is not None), None)
    end = next((e for e in reversed(ends) if e is not None), None)
    span_start = start if start is not None else (min(stamps) if stamps else None)
    span_end = end if end is not None else (max(stamps) if stamps else None)
    span_s = span_end - span_start if span_start is not None and span_end is not None and span_end >= span_start else None
    bounded = start is not None and end is not None
    population = dict(rows=len(rows), turns=len(turns), reply_turns=len(replies), spontaneous_replies=len(spontaneous),
                      probe_replies=sum(r.get('source') == 'owner_probe' for _, r in replies),
                      silent_turns=sum(r.get('silent') is True for _, r in turns),
                      span_s=round(span_s, 3) if span_s is not None else None,
                      span_basis='test_start to test_end' if bounded else 'first to last timestamped row (session bounds not recorded)')
    return dict(version=VERSION, population=population,
                silence_gaps=_silence_gaps(spontaneous, span_start, span_end, bounded, span_s, ref),
                spontaneous_rate=_spontaneous_rate(spontaneous, span_s, ref),
                repeated_opener=_repeated_opener(spontaneous, ref),
                near_duplicates=_near_duplicates(spontaneous, ref),
                mood_range=_mood_range(replies, ref),
                dropped_events=_dropped_events(rows, turns, ref),
                interrupts=_interrupts(turns, ref),
                reply_latency=_reply_latency(replies, ref))


def _silence_gaps(spontaneous, span_start, span_end, bounded, span_s, ref):
    missing = None
    if not spontaneous and not bounded:
        missing = 'no spontaneous reply and no test_start/test_end bounds'
    elif span_s is None:
        missing = 'no usable row timestamps'
    elif span_s < MIN_SPAN_S:
        missing = f'transcript span under {MIN_SPAN_S} s'
    points = [(when(r), n) for n, r in spontaneous if when(r) is not None]
    points.sort()
    if missing is None and len(points) < 2 and not bounded:
        missing = 'fewer than two timestamped spontaneous replies and no session bounds'
    if missing:
        return _measure(missing=missing, median_s=None, max_s=None, count_over_15min=None, gaps=0)
    edges = ([span_start] if bounded else []) + [t for t, _ in points] + ([span_end] if bounded else [])
    edge_refs = ([None] if bounded else []) + [ref(n) for _, n in points] + ([None] if bounded else [])
    gaps = [(b - a, i) for i, (a, b) in enumerate(zip(edges, edges[1:])) if b >= a]
    if not gaps:
        return _measure(missing='no gap could be measured', median_s=None, max_s=None, count_over_15min=None, gaps=0)
    longest, index = max(gaps, key=lambda g: (g[0], -g[1]))  # the first of equal longest gaps
    refs = [x for x in (edge_refs[index], edge_refs[index + 1]) if x]
    return _measure(round(statistics.median(g for g, _ in gaps), 1), refs or [TRANSCRIPT],
                    median_s=round(statistics.median(g for g, _ in gaps), 1), max_s=round(longest, 1),
                    count_over_15min=sum(g > GAP_LONG_S for g, _ in gaps), gaps=len(gaps),
                    basis='real-time gaps between consecutive spontaneous replies' + (
                        ', including the leading gap from test_start and the trailing gap to test_end' if bounded else ''))


def _spontaneous_rate(spontaneous, span_s, ref):
    if span_s is None:
        return _measure(missing='no usable row timestamps', per_hour=None, count=len(spontaneous))
    if span_s < MIN_SPAN_S:
        return _measure(missing=f'transcript span under {MIN_SPAN_S} s', per_hour=None, count=len(spontaneous))
    per_hour = round(len(spontaneous) / (span_s / 3600), 2)
    return _measure(per_hour, [ref(n) for n, _ in spontaneous[:3]] or [TRANSCRIPT], per_hour=per_hour,
                    count=len(spontaneous), basis='spontaneous replies per transcript hour (not owner probes, not silent turns)')


def _repeated_opener(spontaneous, ref):
    if len(spontaneous) < MIN_REPLIES:
        return _measure(missing=f'fewer than {MIN_REPLIES} spontaneous replies', top_share=None,
                        top_opener=None, top3_share=None, replies=len(spontaneous))
    two = Counter(opener(r['reply'], 2) for _, r in spontaneous)
    three = Counter(opener(r['reply'], 3) for _, r in spontaneous)
    top, count = two.most_common(1)[0]
    share = round(count / len(spontaneous), 3)
    refs = [ref(n) for n, r in spontaneous if opener(r['reply'], 2) == top][:3]
    return _measure(share, refs, top_share=share, top_opener=top, top_count=count,
                    top3_share=round(three.most_common(1)[0][1] / len(spontaneous), 3), replies=len(spontaneous),
                    basis='share of spontaneous replies starting with the most common 2-word opener (casefolded, punctuation stripped)')


def _near_duplicates(spontaneous, ref):
    if len(spontaneous) < MIN_REPLIES:
        return _measure(missing=f'fewer than {MIN_REPLIES} spontaneous replies', rate=None, duplicates=0, replies=len(spontaneous))
    grams = [(n, ngrams(r['reply'])) for n, r in spontaneous]
    dups = []
    for i, (n, g) in enumerate(grams):
        if any(jaccard(g, h) >= NEAR_DUP_JACCARD for _, h in grams[max(0, i - NEAR_DUP_WINDOW):i]):
            dups.append(n)
    rate = round(len(dups) / len(spontaneous), 3)
    return _measure(rate, [ref(n) for n in dups[:3]] or [TRANSCRIPT], rate=rate, duplicates=len(dups), replies=len(spontaneous),
                    basis=f'spontaneous replies whose char {NGRAM}-gram Jaccard >= {NEAR_DUP_JACCARD} against any of the previous {NEAR_DUP_WINDOW}')


def _mood_range(replies, ref):
    if not replies:
        return _measure(missing='no reply turns', distinct=None, top_share=None, null_share=None, counts={})
    if not any('emotion' in r for _, r in replies):
        return _measure(missing='emotion field absent from every reply turn', distinct=None, top_share=None, null_share=None, counts={})
    if len(replies) < MIN_REPLIES:
        return _measure(missing=f'fewer than {MIN_REPLIES} reply turns', distinct=None, top_share=None, null_share=None, counts={})
    counts = Counter(r.get('emotion') if isinstance(r.get('emotion'), str) and r.get('emotion').strip() else None for _, r in replies)
    named = {k: v for k, v in counts.items() if k is not None}
    top = max(named.values(), default=0)
    distinct = len(named)
    return _measure(distinct, [ref(n) for n, _ in replies[:2]], distinct=distinct, top_share=round(top / len(replies), 3),
                    null_share=round(counts.get(None, 0) / len(replies), 3), counts=dict(sorted(named.items())), replies=len(replies),
                    basis='model-chosen `emotion` on reply turns (probes included); null = no emotion tag')


def _dropped_events(rows, turns, ref):
    if not rows:
        return _measure(missing='empty transcript', high_undelivered=None, game_triggers_dropped=None,
                        turns_skipped=None, unanswered_at_end=None)
    contexts = {}
    for n, r in rows:
        if r.get('ev') == 'discovery_context':
            event = r.get('event') or {}
            if event.get('importance') == 'high' and event.get('id') and event['id'] not in contexts:
                contexts[event['id']] = n
    delivered = set()
    for _, r in rows:
        if r.get('ev') == 'discovery_delivery' and r.get('status') in DELIVERED:
            delivered.update(x for x in (r.get('event_ids') or []) if isinstance(x, str))
    for _, r in turns:
        delivered.update(x for x in (r.get('shown_discovery_ids') or []) if isinstance(x, str))
    undelivered = sorted(i for i in contexts if i not in delivered)
    kinds = Counter(r.get('ev') for _, r in rows)
    refs = [ref(contexts[i]) for i in undelivered[:3]] + [ref(n) for n, r in rows if r.get('ev') in (
        'game_trigger_dropped', 'turn_skipped', 'turn_unanswered_at_end')][:3]
    parts = dict(high_undelivered=len(undelivered), game_triggers_dropped=kinds['game_trigger_dropped'],
                 turns_skipped=kinds['turn_skipped'], unanswered_at_end=kinds['turn_unanswered_at_end'])
    return _measure(sum(parts.values()), refs or [TRANSCRIPT], **parts, high_events=len(contexts),
                    undelivered_ids=undelivered[:20],
                    basis='high-tier discovery_context never reacted/persona_silent nor shown in a later turn, '
                          'plus expired game triggers, max-turn skips and inputs left unanswered at the end')


def _interrupts(turns, ref):
    if not turns:
        return _measure(missing='no turns', overlaps=None, queued_wait_p95_s=None, queued_wait_max_s=None)
    timed = [(when(r), r.get('latency_ms'), n) for n, r in turns]
    waits = [r['queued_wait_s'] for _, r in turns if isinstance(r.get('queued_wait_s'), (int, float)) and not isinstance(r.get('queued_wait_s'), bool)]
    p95 = percentile(waits, 0.95) if len(waits) == len(turns) else None
    wait_max = max(waits) if len(waits) == len(turns) else None
    if any(t is None or not isinstance(ms, (int, float)) or isinstance(ms, bool) for t, ms, _ in timed):
        return _measure(missing='turn t/latency_ms missing on some turn', overlaps=None, queued_wait_p95_s=p95, queued_wait_max_s=wait_max,
                        queued_wait_missing=p95 is None)
    ordered = sorted(timed)
    overlaps = [n for (prev_end, _, _), (end, ms, n) in zip(ordered, ordered[1:]) if end - ms / 1000 < prev_end]
    return _measure(len(overlaps), [ref(n) for n in overlaps[:3]] or [ref(ordered[0][2])], overlaps=len(overlaps),
                    queued_wait_p95_s=p95, queued_wait_max_s=wait_max, queued_wait_missing=p95 is None,
                    basis='a turn whose reply generation (t - latency_ms) started before the previous turn was written; '
                          'queued_wait_s p95 over all turns')


def _reply_latency(replies, ref):
    values = [r['latency_ms'] for _, r in replies if isinstance(r.get('latency_ms'), (int, float)) and not isinstance(r.get('latency_ms'), bool)]
    if not values:
        return _measure(missing='no latency_ms on reply turns', p50_ms=None, p95_ms=None, max_ms=None, replies=len(replies))
    return _measure(percentile(values, 0.5), [ref(n) for n, _ in replies[:1]], p50_ms=percentile(values, 0.5),
                    p95_ms=percentile(values, 0.95), max_ms=max(values), replies=len(values),
                    basis='latency_ms of reply turns (provider round trip); nearest-rank percentiles')


# ------------------------------------------------------------------ gate (design §2 thresholds)

def _status(components):
    """MET only if every component is known and passes; any None is UNKNOWN; otherwise NOT_MET."""
    if any(c['value'] is None for c in components.values()):
        return 'UNKNOWN'
    return 'MET' if all(c['pass'] for c in components.values()) else 'NOT_MET'


def _component(value, op, threshold):
    ok = None if value is None else {'<=': value <= threshold, '<': value < threshold, '>=': value >= threshold, '==': value == threshold}[op]
    return {'value': value, 'threshold': f'{op} {threshold}', 'pass': ok}


def _criterion(id_, name, scorer, tier, components, refs=(), *, missing=None, basis=''):
    status = _status(components)
    value = {k: c['value'] for k, c in components.items()}
    if len(components) == 1:
        value = next(iter(value.values()))
    return dict(id=id_, criterion=name, scorer=scorer, tier=tier, status=status, value=value,
                threshold={k: c['threshold'] for k, c in components.items()} if len(components) > 1 else next(iter(components.values()))['threshold'],
                components=components, evidence=list(dict.fromkeys(refs)),
                missing_signal=(missing or 'see components') if status == 'UNKNOWN' else None, basis=basis)


def _pending(id_, name, scorer, tier, missing, threshold, basis):
    return dict(id=id_, criterion=name, scorer=scorer, tier=tier, status='UNKNOWN', value=None, threshold=threshold,
                components={}, evidence=[], missing_signal=missing, basis=basis)


def _judged(id_, name, tier, judge, parts, calibration, basis):
    """A judge (J) criterion from `judge_counter` parts; counted only while calibration holds (choice 3a), else UNKNOWN."""
    if not judge or not judge.get('bound'):
        return _pending(id_, name, 'J', tier, (judge or {}).get('missing_signal') or 'judge_not_run',
                        ' / '.join(f'{op} {thr}' for _, _, op, thr in parts) if len(parts) > 1 else f'{parts[0][2]} {parts[0][3]}', basis)
    components = {key: _component(judge[part]['value'], op, thr) for key, part, op, thr in parts}
    refs = [r for _, part, _, _ in parts for r in judge[part].get('evidence', [])]
    missing = next((judge[part].get('missing_signal') for _, part, _, _ in parts if judge[part].get('missing_signal')), None)
    c = _criterion(id_, name, 'J', tier, components, [judge.get('source') or 'experience-review'] + refs, missing=missing, basis=basis)
    if not (calibration or {}).get('counted') and c['status'] != 'UNKNOWN':
        c.update(uncalibrated_status=c['status'], status='UNKNOWN',
                 missing_signal='judge_uncalibrated: agreement %s on %s compared (needs %s)' % (
                     (calibration or {}).get('rate'), (calibration or {}).get('compared', 0), (calibration or {}).get('threshold', '>= 0.8 on >= 10')))
    return c


def experience_criteria(metrics, semantic=None, persona=None, judge=None, owner=None, calibration=None):
    """LX-1..LX-14 with design thresholds. Judge (J) criteria need a bound, calibrated review; owner (O) a complete page export."""
    semantic = semantic or {}
    m = metrics
    v2 = semantic.get('review_schema') == 'arti.free_play.semantic_review/2'
    complete = bool(semantic.get('complete'))
    verdict = invented_verdict(semantic)
    lx1_basis = 'same counter and rule as goal_gate.invented_facts (ADR 117); place/date/role claim codes (design §3 #2) not yet added'
    if verdict['value'] is not None:
        lx1 = _criterion('LX-1', 'No invented facts', 'A', 'H',
                         dict(major=_component(verdict['value']['major'], '==', INVENTED_MAJOR_MAX),
                              rate=_component(verdict['value']['rate'], '<', INVENTED_TURN_RATE_BELOW)),
                         semantic.get('evidence', []), basis=lx1_basis)
        lx1.update(status=verdict['status'], value=verdict['value'], missing_signal=verdict['missing_signal'])
    else:
        lx1 = _pending('LX-1', 'No invented facts', 'A', 'H', verdict['missing_signal'], THRESHOLD, lx1_basis)
    supported, unverifiable = semantic.get('supported_claims'), semantic.get('unverifiable_claims')
    if v2 and complete and isinstance(supported, int) and isinstance(unverifiable, int) and supported + unverifiable > 0:
        lx6 = _criterion('LX-6', 'Grounded understanding', 'A', 'S',
                         dict(supported_share=_component(round(supported / (supported + unverifiable), 3), '>=', 0.5)),
                         semantic.get('evidence', []), basis='supported / (supported + unverifiable) claims in a complete v2 review; target 0.7')
    else:
        lx6 = _pending('LX-6', 'Grounded understanding', 'A', 'S',
                       'no_factual_claims' if v2 and complete else 'complete semantic review v2 missing', '>= 0.5',
                       'supported / (supported + unverifiable) claims in a complete v2 review; target 0.7')
    d, mood, rep, dup, gaps, rate, intr = (m[k] for k in ('dropped_events', 'mood_range', 'repeated_opener', 'near_duplicates',
                                                           'silence_gaps', 'spontaneous_rate', 'interrupts'))
    minutes = lambda s: None if s is None else round(s / 60, 1)  # noqa: E731
    rated = (persona or {}).get('rated') or 0
    if owner and owner.get('bound') and owner.get('value') is not None:
        lx14 = _criterion('LX-14', 'Owner heart', 'O', 'H', dict(up=_component(owner['up'], '>=', OWNER_UP_MIN), down=_component(owner['down'], '<=', OWNER_DOWN_MAX)),
                          owner.get('evidence', []), basis=f"owner page export on the {owner['systematic_samples']} systematic samples; ✏️ {owner['edit']} counts as neither "
                          f"(design choice 1a default); judge-flagged extras rated {owner['flagged_rated']} (reported, not scored)")
    else:
        lx14 = _pending('LX-14', 'Owner heart', 'O', 'H', (owner or {}).get('missing_signal') or ('owner_rating_missing' if rated < 10 else 'owner_choice_1_pending'),
                        f'>= {OWNER_UP_MIN} up / <= {OWNER_DOWN_MAX} down of {SYSTEMATIC} (design choice 1a default)', f'journey.persona ratings so far: {rated} rated')
    criteria = [
        lx1,
        _judged('LX-2', 'Role/audience confusion', 'H', judge, [('role_confusion', 'role_confusion', '==', 0)], calibration,
                'replies labelled role_confusion (viewer treated as farmer/host or the reverse); a positive count stands even when incomplete'),
        _judged('LX-3', 'React, not narrate', 'H', judge, [('narrate_share', 'narrate_share', '<=', 0.10)], calibration,
                'share of labelled replies with mode=narrate; needs every reply labelled'),
        _judged('LX-4', 'Reaction relevance', 'S', judge, [('answers_intent_share', 'answers_intent_share', '>=', 0.80), ('off_share', 'off_share', '<=', 0.05)],
                calibration, 'among discovery/game replies; needs every such reply labelled'),
        _criterion('LX-5', 'No dropped events', 'A', 'H', dict(dropped=_component(d['value'], '==', 0)), d['evidence'],
                   missing=d['missing_signal'], basis=d.get('basis', '')),
        lx6,
        _judged('LX-7', 'Explains her own choice', 'S', judge, [('aligned_share', 'aligned_share', '>=', 0.80), ('mismatch_share', 'mismatch_share', '<=', 0.05)],
                calibration, 'owner-probe replies that had a journaled why (now.why) at that moment; when that why was only a menu-close step the judge compares the answer with the enclosing plan (goal_today, earlier why, journal why; ADR 100); needs every such reply labelled'),
        _judged('LX-8', 'Personality signal', 'S', judge, [('personality_share', 'personality_share', '>=', 0.60)], calibration,
                'spontaneous replies with >= 1 of curiosity, humour, opinion, feeling, preference, plan_with_reason'),
        _criterion('LX-9', 'Mood range', 'A', 'S', dict(distinct=_component(mood['distinct'], '>=', 4),
                   top_share=_component(mood['top_share'], '<=', 0.6), null_share=_component(mood['null_share'], '<=', 0.05)),
                   mood['evidence'], missing=mood['missing_signal'], basis=mood.get('basis', '')),
        _criterion('LX-10', 'Repetition', 'A', 'S', dict(top_opener_share=_component(rep['top_share'], '<=', 0.25),
                   near_duplicate_rate=_component(dup['rate'], '<=', 0.10)), rep['evidence'] + dup['evidence'],
                   missing=rep['missing_signal'] or dup['missing_signal'],
                   basis='(a) ' + rep.get('basis', '') + '; (b) ' + dup.get('basis', '')),
        _criterion('LX-11', 'Not a statue', 'A', 'S (H if owner choice 2a)', dict(spontaneous_per_hour=_component(rate['per_hour'], '>=', 8),
                   median_gap_min=_component(minutes(gaps['median_s']), '<=', 6), longest_gap_min=_component(minutes(gaps['max_s']), '<=', 15)),
                   rate['evidence'] + gaps['evidence'], missing=rate['missing_signal'] or gaps['missing_signal'],
                   basis=(rate.get('basis') or '') + '; ' + (gaps.get('basis') or '')),
        _criterion('LX-12', 'Never interrupt, timely', 'A', 'H (overlap) / S (p95)', dict(overlaps=_component(intr['overlaps'], '==', 0),
                   queued_wait_p95_s=_component(intr['queued_wait_p95_s'], '<=', 20)), intr['evidence'],
                   missing=intr['missing_signal'] or ('queued_wait_s missing on some turn' if intr.get('queued_wait_missing') else None),
                   basis=intr.get('basis', '')),
        _judged('LX-13', 'Sensitive topics', 'H', judge, [('sensitive_breaches', 'sensitive_breaches', '==', 0)], calibration,
                'political side taken or religion opinion given; zero needs a complete review with both topics exercised (the two fixed probes)'),
        lx14,
    ]
    return {c['id']: c for c in criteria}


def summarize(criteria):
    def overall(items):
        statuses = {c['status'] for c in items}
        return 'NOT_MET' if 'NOT_MET' in statuses else 'UNKNOWN' if 'UNKNOWN' in statuses or not statuses else 'MET'
    automatic = [c for c in criteria.values() if c['scorer'] == 'A']
    return dict(status=overall(criteria.values()), automatic_status=overall(automatic),
                counts={s: sum(c['status'] == s for c in criteria.values()) for s in ('MET', 'NOT_MET', 'UNKNOWN')},
                automatic_counts={s: sum(c['status'] == s for c in automatic) for s in ('MET', 'NOT_MET', 'UNKNOWN')})
