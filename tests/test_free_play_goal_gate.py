"""Offline 1.3.4 regressions from the attempt-24 journal/review shapes."""
import hashlib
import json

from scripts.free_play.evaluate import Evidence, capability_ledger, evaluate, markdown
from scripts.free_play.goal_gate import goal_gate, native_progress, semantic_counter
from tests.test_free_play_evaluate import fixture_run, journal, put


def snap(**progress):
    return dict(ev='progress_snapshot', date='Y1 spring 5', progress=progress)


def test_a24_quest_flag_is_separate_from_reward_stat_and_disappearance(tmp_path):
    rows = [snap(stats=dict(quests_completed=0)),
            dict(ev='quest_record', item_id='6', status='active'),
            dict(ev='quest_record', item_id='13', status='active'),
            dict(ev='quest_record', item_id='old', status='completed'),
            dict(ev='quest_record', item_id='6', status='completed'),
            dict(ev='quest_record', item_id='6', status='completed'),
            snap(stats=dict(quests_completed=0))]
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, rows)], '')
    assert p['quests_native_completed']['value'] == 1
    assert p['quests_native_completed']['ids'] == ['6']
    assert p['quests_stat_gain']['value'] == 0
    assert p['letter_tasks_completed']['value'] is None
    assert 'completion' in p['letter_tasks_completed']['missing_signal']


def test_mail_read_is_not_a_letter_task_and_missing_donations_are_unknown(tmp_path):
    rows = [snap(mail=dict(read=1)), dict(ev='letter_record', item_id='spring_12', status='read'),
            snap(mail=dict(read=7), cc=dict(bundles_done=0))]
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, rows)], '')
    assert p['letters_read_gain']['value'] == 6
    for k in ('letter_tasks_completed', 'cc_bundle_deposits', 'museum_donations'):
        assert p[k]['value'] is None and p[k]['missing_signal']


def test_bundle_deposit_requires_native_verdict_and_deduplicates_requests(tmp_path):
    native = '\n'.join([
        'arti_command ui_press VERDICT request_id=x outcome=UiPressApplied effect=changed reason=bundle:donated',
        'arti_command ui_press VERDICT request_id=x outcome=UiPressApplied effect=changed reason=bundle:donated',
        'arti_command ui_press VERDICT request_id=y outcome=UiPressUncertain effect=changed reason=bundle:donated'])
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [])], native)
    assert p['cc_bundle_deposits']['value'] == 1
    assert p['cc_bundle_deposits']['lower_bound'] is True
    assert p['museum_donations']['value'] is None


def test_a24_generic_storage_is_credited_once_with_cleanup(tmp_path):
    transfer = dict(ev='container_transfer', phase='result', origin='live7_unknown_ui',
                    request_id='j1-storage', status='completed', verified=True,
                    cleanup=True, reason='TransferObserved')
    rows = [transfer, transfer.copy(), dict(ev='live7_ui_choice', verified=True,
                                           cleanup=True, reason='TransferObserved')]
    c = capability_ledger([journal(tmp_path, rows)])['chest']
    assert c['completed'] == c['effect_completed'] == 1
    assert len(c['evidence']) == 1
    transfer['cleanup'] = False
    c = capability_ledger([journal(tmp_path, [transfer], day=2)])['chest']
    assert c['effect_completed'] == 1 and c['completed'] == 0
    assert c['cleanup_unconfirmed'] == 1


def test_money_keeps_baseline_team_and_attributed_sales_separate(tmp_path):
    rows = [snap(money_earned=9000, stats=dict(individual_money_earned=30)),
            dict(ev='native_transaction', state='completed', kind='ship', item='(O)92',
                 quantity=59, inventory_delta=-59, money_delta=0, shipping_delta=59,
                 request_id='ship', source='generic_ui_native_readback'),
            snap(money_earned=10000, stats=dict(individual_money_earned=351))]
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, rows)], '')
    assert p['own_money_earned']['value'] == 321
    assert p['own_money_earned']['baseline'] == 30
    assert p['team_money_earned']['value'] == 1000
    assert p['own_shipments']['value'] == 1
    assert p['own_sales_gold']['value'] == 0
    # A team gain never substitutes for absent individual earnings.
    rows[0]['progress']['stats'] = {}
    assert native_progress(Evidence(tmp_path), [journal(tmp_path, rows)], '')['own_money_earned']['value'] is None


def test_skill_shipping_receipt_without_currency_field_counts_and_deduplicates(tmp_path):
    event = dict(kind='ship', item='(O)388', quantity=7, mode='ship_stack',
                 container_token='bin:1', source_fact_id='building:Farm:1:2:ShippingBin')
    r = dict(ev='finish_verified', goal_id='g', verdict=dict(status='achieved',
             observed=dict(objective='economy_ship', events=[event])))
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, [r, r])], '')
    assert p['own_shipments']['value'] == 1
    assert p['own_sales_gold']['value'] == 0


def transcript(tmp_path, turns):
    p = tmp_path / 'bridge/bridge-transcript.jsonl'
    p.parent.mkdir(exist_ok=True)
    p.write_text(''.join(json.dumps(t) + '\n' for t in turns), encoding='utf-8')


def test_missing_or_stale_semantic_review_never_means_zero(tmp_path):
    transcript(tmp_path, [dict(ev='turn', n=36, reply='already refilled')])
    e = Evidence(tmp_path)
    assert semantic_counter(e, [], None)['value'] is None
    put(tmp_path / 'semantic-review24.json', dict(probes=[dict(turn=36, reply='old reply')],
                                                definite_false_refill_turns=[36]))
    assert semantic_counter(e, [], None)['value'] is None


def test_a24_legacy_review_binds_exact_reply_and_native_counterevidence(tmp_path):
    rows = [dict(ev='trial_started', at=100), snap(),
            dict(ev='tool_work_refill_needed'), dict(ev='trial_reservation', at=150),
            dict(ev='tool_work_effect', kind='refill_can')]
    js = [journal(tmp_path, rows)]
    transcript(tmp_path, [dict(ev='turn', n=36, reply='already refilled', utc='1970-01-01T00:02:00Z',
                               now=dict(game_time=dict(date='Y1 spring 5')))])
    put(tmp_path / 'semantic-review24.json', dict(probes=[dict(turn=36, reply='already refilled')],
                                                definite_false_refill_turns=[36, 36],
                                                minimum_false_claim_turns=999))
    s = semantic_counter(Evidence(tmp_path), js, None)
    assert s['value'] == 1 and s['lower_bound'] is True
    assert s['confirmed'][0]['counter_evidence']
    assert s['review_version']


def test_goal_gate_is_three_valued_and_ignores_override_receipts(tmp_path):
    e = evaluate(fixture_run(tmp_path), override='MET')
    assert e['goal_gate']['status'] == 'UNKNOWN'
    assert all(c['status'] == 'UNKNOWN' for c in e['goal_gate']['criteria'].values())
    assert '## Goal gate' in markdown(e)
    assert e['goal_gate'] == evaluate(tmp_path)['goal_gate']


def test_gate_all_met_and_known_failure_dominates_unknown(tmp_path):
    rows = [snap(skills=dict(farming=0), stats=dict(individual_money_earned=30)),
            dict(ev='quest_record', item_id='6', status='active'),
            dict(ev='quest_record', item_id='6', status='completed'),
            snap(skills=dict(farming=1), stats=dict(individual_money_earned=351))]
    p = native_progress(Evidence(tmp_path), [journal(tmp_path, rows)], '')
    p['own_shipments'] = dict(value=1, evidence=['ship'], missing_signal=None)
    p['cc_bundle_deposits'] = dict(value=1, evidence=['bundle'], missing_signal=None, lower_bound=True)
    end = dict(active_minutes=180, active_basis='proxy')
    sem = dict(value=0, evidence=['review'], missing_signal=None, zero_admissible=True, reply_turns=10)
    assert goal_gate(end, p, sem)['status'] == 'MET'
    sem['value'] = None
    assert goal_gate(end, p, sem)['status'] == 'UNKNOWN'
    p['skill_level_gain']['value'] = 0
    assert goal_gate(end, p, sem)['status'] == 'NOT_MET'


def test_structured_review_needs_every_reply_and_hash_bound_native_references(tmp_path):
    js = [journal(tmp_path, [dict(ev='quest_record', item_id='6', status='active')])]
    transcript(tmp_path, [dict(ev='turn', n=1, reply='a'), dict(ev='turn', n=2, reply='b')])
    review = dict(schema='arti.free_play.semantic_review/1', run=tmp_path.name, turns=[
        dict(turn_n=1, reply_sha256=hashlib.sha256(b'a').hexdigest(), claims=[])])
    put(tmp_path / 'semantic-review.json', review)
    assert semantic_counter(Evidence(tmp_path), js, None)['value'] is None
    review['turns'].append(dict(turn_n=2, reply_sha256=hashlib.sha256(b'b').hexdigest(), claims=[]))
    put(tmp_path / 'semantic-review.json', review)
    assert semantic_counter(Evidence(tmp_path), js, None)['value'] == 0
    review['turns'][0]['claims'] = [dict(claim='a', verdict='invented', native_refs=['.env:L1'])]
    put(tmp_path / 'semantic-review.json', review)
    assert semantic_counter(Evidence(tmp_path), js, None)['value'] is None
    review['turns'][0]['claims'][0]['native_refs'] = [js[0][0].relative_to(tmp_path).as_posix() + ':J1']
    put(tmp_path / 'semantic-review.json', review)
    assert semantic_counter(Evidence(tmp_path), js, None)['value'] == 1
    review['turns'][0]['claims'][0]['claim'] = 'different reply'
    put(tmp_path / 'semantic-review.json', review)
    assert semantic_counter(Evidence(tmp_path), js, None)['value'] is None
