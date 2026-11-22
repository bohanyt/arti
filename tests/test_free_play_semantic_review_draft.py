"""Offline tests for the semantic review draft (ADR 92): a starting point that can never count by itself."""
import json

from scripts.free_play.evaluate import Evidence
from scripts.free_play.goal_gate import semantic_counter
from scripts.free_play.semantic_review_draft import build_draft, extract_claims, main
from tests.test_free_play_evaluate import journal, put
from tests.test_free_play_goal_gate import transcript

REPLY = 'Aku di Farm sekarang. Punya 120g di saku; lalu siram parsnip\nsantai saja!'


def fixture(tmp_path, replies=(REPLY, None, 'santai saja'), with_journal=True):
    js = []
    if with_journal:
        js = [journal(tmp_path, [
            dict(ev='progress_snapshot', at=110, date='Y1 spring 5', progress=dict(skills=dict(farming=0), friends={'private': 99})),
            dict(ev='quest_record', at=111, item_id='q1', status='active', title='Parsnip Order', text='bring parsnip'),
            dict(ev='tool_work_effect', at=115, kind='refill_can'),
            dict(ev='location_changed', at=116, to='Farm', **{'from': 'FarmHouse'}),
            # ADR 100: the newest bag/wallet/crop read is citable for count claims.
            dict(ev='live7_baseline', at=117, skill='siram', arg=None,
                 before=dict(inventory={'(O)24': 3}, money=120, location='Farm', energy=200, time_of_day=900, weather='sunny',
                             crops={'tf:Farm:1,1:HoeDirt': dict(qualified_item_id='(O)24', phase=1, watered=False,
                                                                ready_for_harvest=False, dead=False)}, tile=[1, 1], pixel=[64, 64],
                             targets=['tf:Farm:1,1:HoeDirt']))])]
    transcript(tmp_path, [dict(ev='turn', n=i + 1, utc=f'1970-01-01T00:02:{i:02}Z', reply=r) for i, r in enumerate(replies)])
    return js


def test_candidates_are_exact_unique_and_refs_stay_within_turn_facts(tmp_path):
    js = fixture(tmp_path)
    draft = build_draft(Evidence(tmp_path), js)
    assert draft['draft'] == dict(schema='arti.free_play.semantic_review_draft/1', generator='semantic_review_draft',
                                  status='DRAFT_PENDING_REVIEW', factual_coverage_min=0.9)
    assert draft['schema'] == 'arti.free_play.semantic_review/2'
    first = draft['turns'][0]
    quotes = [c['claim'] for c in first['claims']]
    assert quotes and len(set(quotes)) == len(quotes) and all(q in REPLY for q in quotes)
    assert any(q.startswith('Punya 120g') and c['candidate_reason'] == 'money' for q, c in zip(quotes, first['claims']))
    refs = {f['ref'] for f in first['native_facts']}
    assert first['claims_exhaustive'] is False
    for c in first['claims']:
        assert c['verdict'] is None and c['severity'] is None and c['native_refs'] == [] and c['status'] == 'PENDING'
        assert set(c['candidate_native_refs']) <= refs
    parsnip = next(c for c in first['claims'] if 'parsnip' in c['claim'])
    assert any(r.endswith(':J2:quest') or r.endswith(':J2') for r in parsnip['candidate_native_refs'])


def test_draft_carries_empty_reviewer_block_and_bag_candidate_for_counts(tmp_path):
    js = fixture(tmp_path)
    draft = build_draft(Evidence(tmp_path), js)
    assert draft['reviewer']['role'] is None and draft['reviewer']['identity'] is None
    assert draft['reviewer']['roles'] == ['operator', 'independent'] and draft['draft']['factual_coverage_min'] == 0.9
    first = draft['turns'][0]
    baseline = next(f for f in first['native_facts'] if f['ev'] == 'live7_baseline')
    assert baseline['facts']['before']['money'] == 120 and 'tile' not in baseline['facts']['before']
    money = next(c for c in first['claims'] if c['candidate_reason'] == 'money')
    assert baseline['ref'] in money['candidate_native_refs']
    # A sentence that shares no token with the bag still names only the newest baseline, never older rows.
    assert money['candidate_native_refs'] == [baseline['ref']]


def test_no_claim_null_reply_and_duplicates(tmp_path):
    js = fixture(tmp_path)
    draft = build_draft(Evidence(tmp_path), js)
    assert draft['turns'][1]['claims'] == [] and draft['turns'][1]['draft_note'] == 'no reply text'
    assert draft['turns'][2]['claims'] == [] and 'draft_note' not in draft['turns'][2]
    assert [c['claim'] for c in extract_claims('Ada 5 apel. Ada 5 apel.', [])] == ['Ada 5 apel']
    assert extract_claims('', []) == [] and extract_claims(None, []) == []


def test_missing_journals_still_extract_numbers_and_money_without_refs(tmp_path):
    fixture(tmp_path, replies=('Dapat 50g hari ini', 'ada 3 telur'), with_journal=False)
    draft = build_draft(Evidence(tmp_path), [])
    reasons = [[c['candidate_reason'] for c in t['claims']] for t in draft['turns']]
    assert reasons == [['money'], ['number']]
    assert all(c['candidate_native_refs'] == [] for t in draft['turns'] for c in t['claims'])


def test_deterministic_and_leaks_nothing_private(tmp_path):
    js = fixture(tmp_path)
    assert build_draft(Evidence(tmp_path), js) == build_draft(Evidence(tmp_path), js)
    assert 'private' not in json.dumps(build_draft(Evidence(tmp_path), js))


def finish(draft, role='independent'):
    draft['reviewer'] = dict(role=role, identity='test-reviewer')
    for t in draft['turns']:
        t['claims_exhaustive'] = True
        for c in t['claims']:
            refs = c['candidate_native_refs']
            c.update(verdict='supported', native_refs=refs) if refs else c.update(
                verdict='unverifiable', reason='no native fact covers this claim')
            for k in ('candidate_native_refs', 'candidate_reason', 'status'):
                c.pop(k)
        t.pop('draft_note', None)


def test_draft_never_counts_but_is_a_valid_start_for_review(tmp_path):
    js = fixture(tmp_path)
    draft = build_draft(Evidence(tmp_path), js)
    put(tmp_path / 'semantic-review.json', draft)
    s = semantic_counter(Evidence(tmp_path), js)
    assert s['value'] is None and s['complete'] is False
    # Reviewer finishes one turn only: still incomplete.
    one = json.loads(json.dumps(draft))
    finish(one)
    one['turns'][1:] = [dict(t) for t in draft['turns'][1:]]
    put(tmp_path / 'semantic-review.json', one)
    assert semantic_counter(Evidence(tmp_path), js)['value'] is None
    # Reviewer finishes every turn.
    finish(draft)
    put(tmp_path / 'semantic-review.json', draft)
    s = semantic_counter(Evidence(tmp_path), js)
    assert s['value'] == 0 and s['complete'] is True and s['zero_admissible'] is True
    # The same finished review signed by the session operator never earns the zero (ADR 100).
    operator = json.loads(json.dumps(draft))
    operator['reviewer'] = dict(role='operator', identity='the-operator')
    put(tmp_path / 'semantic-review.json', operator)
    s = semantic_counter(Evidence(tmp_path), js)
    assert s['value'] is None and s['complete'] is True and s['zero_blockers'] == ['reviewer_is_operator']


def test_cli_writes_summary_and_refuses_existing_output(tmp_path, capsys):
    fixture(tmp_path)
    out = tmp_path / 'out' / 'semantic-review.json'
    main([str(tmp_path), '--output', str(out)])
    printed = capsys.readouterr().out
    assert 'arti.free_play.semantic_review/2: 3 turns,' in printed and 'reviewer block required' in printed
    assert json.loads(out.read_text(encoding='utf-8'))['draft']['status'] == 'DRAFT_PENDING_REVIEW'
    try:
        main([str(tmp_path), '--output', str(out)])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError('existing output must be refused')


# ---- FIX66D / ADR 103: bag claims ----

def bag_fixture(tmp_path, reply):
    offer = lambda at, used: dict(ev='live7_harvest_offer', at=at, value=dict(offered=True, free_slots=12 - used, bag_used=used,
                                                                              bag_capacity=12, note='private'))
    base = lambda at, money: dict(ev='live7_baseline', at=at, skill='siram', arg=None,
                                  before=dict(inventory={'(O)24': 3}, money=money, free_slots=0, bag_used=12, bag_capacity=12))
    js = [journal(tmp_path, [base(100, 5), base(101, 6), offer(102, 10), offer(103, 12)])]
    transcript(tmp_path, [dict(ev='turn', n=1, utc='1970-01-01T00:02:00Z', reply=reply)])
    draft = build_draft(Evidence(tmp_path), js)
    facts = draft['turns'][0]['native_facts']
    newest = {ev: max((f for f in facts if f['ev'] == ev), key=lambda f: f['at'])['ref']
              for ev in ('live7_baseline', 'live7_harvest_offer')}
    return draft['turns'][0]['claims'], newest


def test_bag_digit_sentence_is_number_with_both_count_refs(tmp_path):
    claims, newest = bag_fixture(tmp_path, 'Tas aku penuh, 12 dari 12 slot kepakai.')
    assert len(claims) == 1 and claims[0]['candidate_reason'] == 'number'
    assert set(newest.values()) <= set(claims[0]['candidate_native_refs'])


def test_bag_word_sentence_is_bag_with_both_count_refs(tmp_path):
    claims, newest = bag_fixture(tmp_path, 'Tasnya udah penuh banget.')
    assert len(claims) == 1 and claims[0]['candidate_reason'] == 'bag'
    assert claims[0]['candidate_native_refs'] == sorted(newest.values())


def test_no_bag_word_no_number_gets_no_bag_candidate(tmp_path):
    claims, _ = bag_fixture(tmp_path, 'santai saja ya.')
    assert claims == []
