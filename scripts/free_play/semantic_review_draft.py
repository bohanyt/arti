"""Candidate-claim draft for the per-turn semantic review v2 (ADR 92).

Purpose: give the human reviewer a head start. It starts from
semantic_review_template.build_template (bridge transcript replies plus native
journal facts only) and pre-extracts candidate factual claims per reply, each
with the native refs that share a token with it. It never reads the prompt,
soul, prefs, mood or viewer data, and never copies any private transcript field.

A draft can never count: every turn keeps claims_exhaustive=false, every
claim verdict=null and the required `reviewer` block (ADR 100) is left empty,
so goal_gate.semantic_counter reports value None until a reviewer names
themselves (role operator|independent, identity), sets verdicts/native_refs,
gives every contradicted claim a severity (major|minor, SEVERITY_RULE, ADR 117;
none counts as major) and flips claims_exhaustive per turn. Only an independent
reviewer with measured factual coverage >= FACTUAL_COVERAGE_MIN can earn MET.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play.invented_rule import BAR_TEXT, SEVERITIES, SEVERITY_RULE
from scripts.free_play.semantic_review_template import FACTUAL_COVERAGE_MIN, REVIEWER_ROLES, build_template

DRAFT = dict(schema='arti.free_play.semantic_review_draft/1', generator='semantic_review_draft',
             status='DRAFT_PENDING_REVIEW')
SENTENCE = re.compile(r'[^.!?;\n]+')
WORD = re.compile(r'[^\W_]+')
MONEY = re.compile(r'\d+ ?g\b')
NUMBER = re.compile(r'\d')
CAPITAL = re.compile(r'\b[A-Z][a-z]+')
REASON_BY_EVENT = {'current_place': 'place', 'location_changed': 'place', 'quest_status': 'quest', 'quest_record': 'quest',
                   'letter_record': 'letter', 'progress_snapshot': 'date', 'native_transaction': 'money',
                   # ADR 100: bag, wallet, crop and weather state ride the newest live7_baseline; her own
                   # activity outcomes ride part_live_row.
                   'live7_baseline': 'bag_crop_weather', 'part_live_row': 'activity', 'live7_harvest_offer': 'bag'}
# A money, number or bag sentence that shares no token with any fact still gets the newest bag/wallet/crop read and the
# newest bag slot counts as its candidates: counts of items, gold, crops and room in the bag are checked there
# (ADR 100 finding 2; ADR 103: slot counts ride live7_harvest_offer, the merged baseline inventory is not a slot count).
COUNT_EVENTS = ('live7_baseline', 'live7_harvest_offer')
BAG = re.compile(r'\b(?:tas|bag|ransel|backpack|inventori|inventory|inventaris|slot)\w*', re.IGNORECASE)
REVIEWER_PLACEHOLDER = dict(role=None, identity=None, roles=list(REVIEWER_ROLES),
                            note='required before the review can count; an operator review never earns MET')


def tokens(text):
    return {w.casefold() for w in WORD.findall(text) if len(w) >= 4}


def fact_tokens(value):
    if isinstance(value, str):
        return tokens(value)
    if isinstance(value, dict):
        return set().union(*(fact_tokens(v) for v in value.values()), set())
    if isinstance(value, (list, tuple)):
        return set().union(*(fact_tokens(v) for v in value), set())
    return set()


def extract_claims(reply, native_facts):
    """Candidate claims for one reply: exact, unique substrings, never invented text."""
    if not isinstance(reply, str) or not reply.strip():
        return []
    facts = [(f.get('ref'), f.get('ev'), fact_tokens(f.get('facts'))) for f in native_facts or []
             if isinstance(f, dict) and isinstance(f.get('ref'), str)]
    counts = {ev: ref for ref, ev, _ in facts if ev in COUNT_EVENTS}   # the newest ref per count event
    seen, claims = set(), []
    for match in SENTENCE.finditer(reply):
        sentence = match.group().strip()
        if not sentence or sentence in seen:
            continue
        words = tokens(sentence)
        shared = [(ref, ev) for ref, ev, vals in facts if vals & words]
        # A capitalised word that is not sentence-initial (position 0 after strip).
        later = any(m.start() > 0 for m in CAPITAL.finditer(sentence))
        if MONEY.search(sentence):
            reason = 'money'
        elif NUMBER.search(sentence):
            reason = 'number'
        elif BAG.search(sentence):
            reason = 'bag'
        elif shared:
            reason = REASON_BY_EVENT.get(shared[0][1], 'named_entity')
        elif later:
            reason = 'named_entity'
        else:
            continue
        seen.add(sentence)
        candidates = {ref for ref, _ in shared}
        if reason in ('money', 'number', 'bag') and counts:
            candidates.update(counts.values())
        claims.append(dict(claim=sentence, verdict=None, severity=None, native_refs=[],
                           candidate_native_refs=sorted(candidates),
                           candidate_reason=reason, status='PENDING'))
    return claims


def build_draft(evidence, journals):
    draft = build_template(evidence, journals)
    draft['draft'] = dict(DRAFT, factual_coverage_min=FACTUAL_COVERAGE_MIN)
    draft['reviewer'] = dict(REVIEWER_PLACEHOLDER)
    for turn in draft['turns']:
        turn['claims_exhaustive'] = False
        turn['claims'] = extract_claims(turn.get('reply'), turn.get('native_facts'))
        if not isinstance(turn.get('reply'), str) or not turn['reply'].strip():
            turn['draft_note'] = 'no reply text'
    return draft


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    from scripts.free_play.evaluate import Evidence
    evidence = Evidence(args.run)
    journals = [(p, evidence.rows(p)) for p in sorted(args.run.glob('runtime/live7/*/day-*/journal/g1_journal.jsonl'))]
    if args.output.exists():
        parser.error('output already exists; choose a fresh draft path')
    draft = build_draft(evidence, journals)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    count = sum(len(t['claims']) for t in draft['turns'])
    print(f"{draft['schema']}: {len(draft['turns'])} turns, {count} candidate claims; output={args.output}")
    print(f"reviewer block required (role {'|'.join(REVIEWER_ROLES)}, identity); only an independent review with "
          f"measured factual coverage >= {FACTUAL_COVERAGE_MIN} can earn invented_facts MET")
    print(f"every contradicted claim needs severity {'|'.join(SEVERITIES)}: see severity_rule in the draft; when in doubt, "
          f"{SEVERITY_RULE['when_in_doubt']}. MET needs {BAR_TEXT}")


if __name__ == '__main__':
    main()
