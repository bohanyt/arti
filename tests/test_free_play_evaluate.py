import json
from pathlib import Path

import pytest

from scripts.free_play.evaluate import (EVALUATOR_VERSION, Evidence, action_outcome,
    aggregate, agreement, capability_ledger, classify_stop, endurance, evaluate,
    markdown, mind, primitives, rank, real_path, receipt_ratings, write_evaluation)
from scripts.free_play.scoreboard import load_evaluations, write_scoreboard


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


def journal(tmp_path, rows, day=1):
    path = tmp_path / f'runtime/live7/c1/day-{day:04}-attempt-000/journal/g1_journal.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return path, list(enumerate(rows, 1))


def fixture_run(tmp_path):
    put(tmp_path / 'request.json', dict(code_sha='source', dll_sha256='dll', helper_sha256='helper', no_operator_rescue_policy=True))
    put(tmp_path / 'window.json', dict(start_at=100, end_at=200))
    journal(tmp_path, [dict(ev='trial_started', at=110), dict(ev='trial_decision'),
                       dict(ev='skill_step', kind='act', operation='select_item'),
                       dict(ev='action_result', action=dict(lifecycle='completed', outcome='SelectionObserved', world_effect_status='observed_selection'))])
    put(tmp_path / 'runtime/live7/c1/cycle_state.json', {'days': [{'index': 1, 'status': 'active'}]})
    return tmp_path


def test_no_game_import_and_safe_artifacts(tmp_path):
    result = evaluate(fixture_run(tmp_path), override='owner annotation')
    rank([result])
    write_evaluation(tmp_path, result)
    assert result['evaluator_version'] == EVALUATOR_VERSION
    assert result['verdict']['best_so_far'] is True
    assert result['identity']['path'] == 'harness'
    assert result['real_path']['ratings']['match'] is None
    assert result['endurance']['active_minutes'] is None  # no progress evidence
    assert 'owner annotation' in (tmp_path / 'EVALUATION.md').read_text(encoding='utf-8')
    assert len([x for x in markdown(result).splitlines() if x.startswith('## ')]) == 13  # 1.7.7: + measured tempo/social/variety
    assert load_evaluations([tmp_path])[0] == result


def test_agreement_precedence_and_chosen_denominator(tmp_path):
    rows = [dict(ev='trial_decision'), dict(ev='skill_step', kind='act', operation='select_item'),
            dict(ev='play_decision_refused', reason='play_inspect_required'),
            dict(ev='action_result', action=dict(lifecycle='rejected')),
            dict(ev='trial_decision'), dict(ev='skill_choice_rejected', code='invalid'),
            dict(ev='action_result', action=dict(lifecycle='uncertain'))]
    result = agreement([journal(tmp_path, rows)], {})
    assert result['per_class']['slot']['c'] == 1
    assert result['overall']['a'] == 1
    assert result['overall']['disagreement_pct'] == 100
    assert aggregate({'move': dict(a=2)}, ['move'])['disagreement_pct'] is None


def test_tool_effect_rescues_inputcompleted_only_in_same_window(tmp_path):
    rows = [dict(ev='trial_decision'), dict(ev='skill_step', kind='act', operation='world_key.use_tool'),
            dict(ev='action_result', action=dict(lifecycle='completed', outcome='InputCompleted', world_effect_status='unchanged')),
            dict(ev='tool_work_effect'), dict(ev='trial_decision'),
            dict(ev='skill_step', kind='act', operation='world_key.use_tool'),
            dict(ev='action_result', action=dict(lifecycle='completed', outcome='InputCompleted', world_effect_status='unchanged'))]
    result = agreement([journal(tmp_path, rows)], {})
    assert result['per_class']['world']['e'] == result['per_class']['world']['d'] == 1


def test_terminal_readback_once_and_missing_result_uncertain(tmp_path):
    rows = [dict(ev='skill_step', kind='act', operation='approach_target'),
            dict(ev='action_result', action=dict(lifecycle='not_taken')),
            dict(ev='action_result', action=dict(lifecycle='completed', world_effect_status='observed_change')),
            dict(ev='skill_step', kind='act', operation='select_item'),
            dict(ev='play_decision_refused', reason='play_inspect_required'),
            dict(ev='skill_step', kind='act', operation='world_key.use_tool')]
    table = primitives([journal(tmp_path, rows)], [])['table']
    assert table['move'] == dict(attempts=1, verified=1, failed=0, uncertain=0)
    assert table['select']['failed'] == 1
    assert table['use_tool']['uncertain'] == 1
    assert all(v['attempts'] == v['verified'] + v['failed'] + v['uncertain'] for v in table.values())


def test_uncertainty_is_not_credited_for_observed_pose():
    assert action_outcome(dict(lifecycle='uncertain', world_effect_status='observed_change')) == 'uncertain'
    assert action_outcome(dict(lifecycle='completed', outcome='NOT_COMPLETED:NO_EFFECT', world_effect_status='observed_change')) == 'uncertain'


def test_kept_goals_count_but_stale_reflection_does_not_cross_passout(tmp_path):
    a = journal(tmp_path, [dict(ev='mind_reflection', new_goals=[], kept=['kept-id']), dict(ev='skill_session_finished', reason='live7_sleep_verified')], 1)
    b = journal(tmp_path, [dict(ev='skill_model_decision', choice=['wait'], goal_ref='kept-id'), dict(ev='skill_session_finished', reason='live7:passed_out')], 2)
    c = journal(tmp_path, [dict(ev='skill_model_decision', choice=['wait'], goal_ref=None)], 3)
    result = mind(Evidence(tmp_path), [a, b, c])
    assert result['kept_goal_count'] == 1
    assert len(result['goal_carry_over']) == 2
    assert result['goal_carry_over'][0]['hit'] is True
    assert result['goal_carry_over'][1]['hit'] is False
    assert result['goal_carry_over'][1]['previous_day_reflection'] is False
    assert result['goal_carry_over'][1]['kept_goals'] == []


def test_steering_lint_excludes_mind_native_text_and_counts_unparsed(tmp_path):
    j = journal(tmp_path, [])
    archive = j[0].parent / 'provider_archive'
    put(archive / 'one-request.json', {'content': {'prompt': 'facts\nSchema: {}\n\n' + json.dumps({'mind': 'must', 'unknown_ui': 'should', 'facts': 'sun'})}})
    put(archive / 'two-request.json', {'content': {'prompt': 'facts\nSchema: {}\n\n' + json.dumps({'facts': 'must'})}})
    put(archive / 'three-request.json', {'content': {'prompt': 'unparseable'}})
    result = mind(Evidence(tmp_path), [j])
    assert result['steering_hits'] == 1
    assert result['steering_prompts_checked'] == 2
    assert result['steering_prompts_unparsed'] == 1


def test_capability_declining_is_distinct_from_execution_failure(tmp_path):
    rows = [dict(ev='skill_offer', skills=['beli','jual','craft']),
            dict(ev='skill_model_decision', choice=['beli', 'seed']),
            dict(ev='part_live_row', skill='beli', reason='shop_closed_by_choice'),
            dict(ev='skill_model_decision', choice=['jual', 'stone']),
            dict(ev='part_live_row', skill='jual', reason='unconfirmed')]
    result = capability_ledger([journal(tmp_path, rows)])
    assert result['fish']['status'] == 'unavailable'
    assert result['craft']['status'] == 'offered not chosen'
    assert result['buy']['status'] == 'closed by choice'
    assert result['sell']['status'] == 'broken'


def test_armed_stop_and_terminal_gap_exclude_cleanup(tmp_path):
    j = journal(tmp_path, [dict(ev='trial_started', at=112), dict(ev='live7_no_progress', outcome_class='SAFETY_STOP')])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    path = tmp_path / 'timeline.jsonl'
    path.write_text('\n'.join(json.dumps(x) for x in [dict(at=120, semantic_progress=True), dict(at=200, semantic_progress=True),
                    dict(at=800, file=j[0].relative_to(tmp_path).as_posix(), line=2)]), encoding='utf-8')
    result = endurance(Evidence(tmp_path), [j], [])
    assert result['minutes_played'] == round(700 / 60, 3)
    assert result['active_minutes'] == round(100 / 60, 3)
    assert result['longest_no_progress_gap_s'] == 600


@pytest.mark.parametrize(('cause','expected'), [('JournalCapacityReached', 'primitive'), ('host_stop_or_lost_telemetry', 'harness'),
    ('live7:no_progress_600s', 'owner-hard-stop'), ('hp_critical', 'owner-hard-stop'), ('hours_complete','budget'),
    # ADR 109 (1.6.2): the machine's own disk stopping her is host_resource, before any other class.
    ('disk_projection', 'host_resource'), ('low_disk', 'host_resource'), ('low_disk,disk_projection', 'host_resource'),
    ('host_stop_or_lost_telemetry:process_gone,disk_projection', 'host_resource'),
    ('disk_projection:provider_budget', 'host_resource'), ('window_not_drawable:iconic', 'primitive')])
def test_stop_classes(cause, expected):
    assert classify_stop(cause) == expected


def test_recovered_pad_hiccup_is_counted_and_initiating_protection_names_stop(tmp_path):
    put(tmp_path / 'window.json', dict(start_at=100, end_at=400))
    j = journal(tmp_path, [dict(ev='FAULT', at=150, type='pad_hiccup_recovered', disposition='RECOVER',
        observed=dict(seconds_lost=.3), refs=dict(incident_id='one')),
        dict(ev='skill_session_finished', at=399, reason='stop:live7_stop_file', outcome_class='SAFETY_STOP')])
    cycle = tmp_path / 'runtime/live7/c1/cycle_events.jsonl'
    cycle.write_text(json.dumps(dict(ev='SAFETY_STOP', at=398, reason='pad_watch_violation',
                                    protection='pad_held_after_terminal')) + '\n')
    e = endurance(Evidence(tmp_path), [j], [])
    assert e['pad_hiccup_recoveries'] == 1 and e['pad_hiccup_seconds_lost'] == .3
    assert e['stop_class'] == 'protection' and e['stop_cause'] == 'pad_watch_violation:pad_held_after_terminal'
    cycle.unlink()
    j = journal(tmp_path, [j[1][0][1]])
    e = endurance(Evidence(tmp_path), [j], [])
    assert e['stop_cause'] != 'pad_held_after_terminal' and e['minutes_played'] == 5


def test_a79_native_beds_survive_not_ready_outer_row_and_passout_terminal(tmp_path):
    put(tmp_path / 'window.json', dict(start_at=100, end_at=400))
    js = []
    for day in range(1, 11):
        rows = [dict(ev='skill_session_finished', reason='live7:passed_out')]
        if day <= 8:
            rows.insert(0, dict(ev='part_live_row', skill='tidur', verified=False, outcome_class='NOT_READY',
                evidence=dict(observed=dict(objective='sleep', origin='routine_sleep_rule', start_date=[1,'fall',day],
                                            date=[1,'fall',day+1], answered=True, bed_time=1950))))
        js.append(journal(tmp_path, rows, day))
    e = endurance(Evidence(tmp_path), js, [])
    assert (e['nights_bed'], e['nights_pass_out']) == (8, 2)


def test_rank_separates_paths_and_ignores_override(tmp_path):
    a = evaluate(fixture_run(tmp_path / 'a'))
    b = evaluate(fixture_run(tmp_path / 'b'), override='best ever')
    c = evaluate(fixture_run(tmp_path / 'c'))
    a['endurance']['minutes_played'] = 100
    b['identity']['path'] = c['identity']['path'] = 'real'
    b['endurance']['minutes_played'] = 10
    c['endurance']['minutes_played'] = 20
    rank([a, b, c])
    assert [r['verdict']['best_so_far'] for r in (a,b,c)] == [True,False,True]
    output = tmp_path / 'SCOREBOARD_AUTO.md'
    write_scoreboard([a,b,c], output)
    assert output.read_text().count('| yes |') == 2
    with pytest.raises(ValueError):
        write_scoreboard([a], tmp_path / 'PM1_SCOREBOARD.md')


def test_malformed_evidence_warns_without_echoing_private_content(tmp_path):
    path = tmp_path / 'bad.jsonl'
    path.write_text('{PRIVATE_TEXT\n[]\n{}\n', encoding='utf-8')
    evidence = Evidence(tmp_path)
    assert evidence.rows(path) == [(3, {})]
    assert len(evidence.warnings) == 2
    assert 'PRIVATE_TEXT' not in json.dumps(evidence.warnings)


def test_explicit_receipt_ratings_validated_and_provenanced(tmp_path):
    path = tmp_path / 'receipt.md'
    path.write_text('Probe/why match: 27 MATCH, 1 PARTIAL, 0 invented', encoding='utf-8')
    with pytest.raises(ValueError):
        receipt_ratings(path, 2)
    result = receipt_ratings(path, 28)
    assert result['match'] == 27
    assert len(result['sha256']) == 64


def test_transfers_count_decisions_instead_of_mirrored_native_effects(tmp_path):
    rows = [dict(ev='trial_decision'),
            dict(ev='action_result', action=dict(request_id='one', lifecycle='completed', outcome='TransferObserved', world_effect_status='observed_paired_transfer')),
            dict(ev='container_transfer', phase='result', request_id='one', verified=True),
            dict(ev='live7_ui_choice', pick='transfer:0:put', verified=True)]
    table = primitives([journal(tmp_path, rows)], [])['table']
    assert table['transfer'] == dict(attempts=1, verified=1, failed=0, uncertain=0)


def test_final_bed_is_uncertain_until_checkpoint_and_passout_is_day_change(tmp_path):
    a = journal(tmp_path, [dict(ev='action_result', action=dict(world_effect_status='observed_sleep_question'))], 1)
    b = journal(tmp_path, [dict(ev='skill_session_finished', reason='live7:passed_out')], 2)
    cycles = [{'days': [dict(index=1, status='active'), dict(index=2, checkpoint={'save': True})]}]
    table = primitives([a, b], cycles)['table']
    assert table['sleep_save'] == dict(attempts=2, verified=1, failed=0, uncertain=1)
    assert table['next_morning']['verified'] == 1


def test_bridge_timeout_is_not_counted_again_when_thread_discard_logged(tmp_path):
    bridge = tmp_path / 'bridge'
    bridge.mkdir()
    (bridge / 'bridge.log').write_text('[Codex] timeout 8s - fallback\n[Codex] thread dibuang (timeout)\n[Codex] gpt-6-luna/low 42ms (turn 1) in=1\n')
    result = real_path(Evidence(tmp_path))
    assert result['llm_timeouts'] == 1
    assert result['llm_completed_calls'] == 1


def test_economy_effect_survives_uncertain_skill_summary(tmp_path):
    rows = [dict(ev='skill_model_decision', choice=['kirim', 'sap']),
            dict(ev='finish_verified', goal_id='g', verdict=dict(status='achieved', observed=dict(objective='economy_ship'))),
            dict(ev='finish_verified', goal_id='g', verdict=dict(status='achieved', observed=dict(objective='economy_ship'))),
            dict(ev='part_live_row', skill='kirim', outcome_class='UNCERTAIN', reason='generic_ui_readback_unconfirmed')]
    result = capability_ledger([journal(tmp_path, rows)])
    assert result['ship']['completed'] == 1


def test_native_craft_effect_is_credited_apart_from_cleanup(tmp_path):
    """1.3.2 (a22 d3:J130 then J142): native readback proves the Chest craft; the later skill row is downgraded by
    menu cleanup. Effect and cleanup are reported separately; nothing is credited without native craft events."""
    event = dict(kind='craft', item='(BC)130', quantity=1, money_delta=0, request_id='j1-x', source='generic_crafting_page')
    rows = [dict(ev='skill_model_decision', choice=['buat', 'peti']),
            dict(ev='finish_verified', goal_id='g', verdict=dict(status='achieved', reason='observed_craft',
                 observed=dict(objective='economy_craft', item_bound=True, done=1, events=[event]))),
            dict(ev='part_live_row', skill='buat', outcome_class='UNCERTAIN', verified=False,
                 reason='ui_cleanup_or_readback_unconfirmed',
                 evidence=dict(cleanup=dict(menu=None, free=True, input_released=True)))]
    result = capability_ledger([journal(tmp_path, rows)])
    assert result['craft']['completed'] == 1
    assert result['craft']['effect_completed'] == 1
    assert result['craft']['cleanup_unconfirmed'] == 1
    assert result['craft']['status'] == 'completed'
    # An achieved craft verdict without a native craft event is not effect evidence.
    bare = [dict(ev='skill_model_decision', choice=['buat', 'peti']),
            dict(ev='finish_verified', goal_id='h', verdict=dict(status='achieved', observed=dict(objective='economy_craft'))),
            rows[2]]
    result = capability_ledger([journal(tmp_path, bare, day=2)])
    assert result['craft']['completed'] == 0 and result['craft']['effect_completed'] == 0
    assert result['craft']['status'] == 'broken'


def test_completed_budget_uses_completion_timestamp_before_cleanup(tmp_path):
    j = journal(tmp_path, [dict(ev='skill_session_finished', reason='live7_sleep_verified')])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    put(tmp_path / 'completed.json', dict(ended_utc='1970-01-01T00:03:20+00:00'))
    result = endurance(Evidence(tmp_path), [j], [])
    assert result['stop_class'] == 'budget'
    assert result['minutes_played'] == round(100/60, 3)


def test_scoreboard_protects_manual_filename_case_insensitively(tmp_path):
    with pytest.raises(ValueError):
        write_scoreboard([], tmp_path / 'pm1_scoreboard.MD')


def test_first_stop_keeps_its_own_cause_when_later_stop_differs(tmp_path):
    j = journal(tmp_path, [dict(ev='live7_no_progress', outcome_class='SAFETY_STOP', at=200),
                          dict(ev='live7_journal_alert', outcome_class='SAFETY_STOP', at=300, reason='JournalCapacityReached')])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    result = endurance(Evidence(tmp_path), [j], [])
    assert result['stop_class'] == 'owner-hard-stop'
    assert result['stop_cause'] == 'live7_no_progress'


def test_failsafe_close_alone_does_not_grant_self_chosen_primitive_credit(tmp_path):
    result = primitives([journal(tmp_path, [dict(ev='store_close_rule', verified=True)])], [])
    assert result['table']['close']['verified'] == 1
    assert result['verified_primitives'] == 0


def two_day_run(tmp_path, day2_rows, *, last_receipt_at=300, end_at=1000):
    """1.3.1 (a19): day 1 ends in a saved night, day 2 holds the run's terminal event."""
    put(tmp_path / 'window.json', dict(start_at=100, end_at=end_at))
    d1 = journal(tmp_path, [dict(ev='trial_started', at=110),
                            dict(ev='skill_session_finished', reason='live7_sleep_verified')], day=1)
    d2 = journal(tmp_path, [dict(ev='trial_started', at=400), *day2_rows], day=2)
    (tmp_path / 'timeline.jsonl').write_text('\n'.join(json.dumps(x) for x in [
        dict(at=150, semantic_progress=True), dict(at=last_receipt_at, semantic_progress=True)]), encoding='utf-8')
    return endurance(Evidence(tmp_path), [d1, d2], [])


def test_operator_no_progress_stop_on_the_last_day_is_the_terminal_stop(tmp_path):
    # a19: J1573 live7_stop_file on day 2, 641.823 s after the last receipt; 1.3.0 reported day 1's sleep.
    result = two_day_run(tmp_path, [dict(ev='trial_decision', at=410), dict(ev='live7_stop_file', action='stop')])
    assert result['stop_cause'] == 'live7_stop_file;operator_no_progress_600s'
    assert result['stop_class'] == 'owner-hard-stop'
    assert result['longest_no_progress_gap_s'] == 700


def test_operator_stop_before_ten_minutes_keeps_the_stop_file_cause(tmp_path):
    result = two_day_run(tmp_path, [dict(ev='live7_stop_file', action='stop')], last_receipt_at=500)
    assert result['stop_cause'] == 'live7_stop_file'  # never day 1's live7_sleep_verified
    assert result['stop_class'] == 'primitive'


def test_automatic_no_progress_stop_on_the_last_day_wins_over_an_earlier_day(tmp_path):
    put(tmp_path / 'window.json', dict(start_at=100, end_at=1000))
    d1 = journal(tmp_path, [dict(ev='trial_started', at=110),
                            dict(ev='live7_journal_alert', outcome_class='SAFETY_STOP', at=200, reason='JournalCapacityReached'),
                            dict(ev='skill_session_finished', reason='live7_sleep_verified')], day=1)
    d2 = journal(tmp_path, [dict(ev='trial_started', at=400),
                            dict(ev='live7_no_progress', outcome_class='SAFETY_STOP', at=950),
                            dict(ev='skill_session_finished', reason='live7:no_progress_600s')], day=2)
    result = endurance(Evidence(tmp_path), [d1, d2], [])
    assert (result['stop_cause'], result['stop_class']) == ('live7_no_progress', 'owner-hard-stop')
    assert result['stop_utc'] == utc_of(950)


def test_a_last_day_without_a_terminal_row_is_unknown_not_the_previous_night(tmp_path):
    result = two_day_run(tmp_path, [dict(ev='trial_decision', at=410)])
    assert result['stop_cause'] == 'unknown'


def utc_of(seconds):
    from scripts.free_play.evaluate import utc
    return utc(seconds)


def _mixed_runs(tmp_path):
    new = evaluate(fixture_run(tmp_path / 'new'))
    write_evaluation(tmp_path / 'new', new)
    old = json.loads(json.dumps(new))
    old['run'] = 'old-run'
    old['evaluator_version'] = '1.4.0'
    put(tmp_path / 'old' / 'EVALUATION.json', old)
    return new, old


def test_load_evaluations_accepts_mixed_versions_sorted_by_run(tmp_path):
    new, old = _mixed_runs(tmp_path)
    loaded = load_evaluations([tmp_path])
    assert [e['run'] for e in loaded] == sorted([new['run'], 'old-run'])
    assert {e['evaluator_version'] for e in loaded} == {EVALUATOR_VERSION, '1.4.0'}


def test_load_evaluations_still_refuses_conflicts_foreign_schema_and_missing_version(tmp_path):
    new, old = _mixed_runs(tmp_path)
    dup = dict(old, run=new['run'])
    put(tmp_path / 'dup' / 'EVALUATION.json', dup)
    with pytest.raises(ValueError, match='Conflicting duplicate'):
        load_evaluations([tmp_path])
    (tmp_path / 'dup' / 'EVALUATION.json').unlink()
    put(tmp_path / 'foreign' / 'EVALUATION.json', dict(old, run='x', schema='other/1'))
    with pytest.raises(ValueError, match='Incompatible'):
        load_evaluations([tmp_path])
    put(tmp_path / 'foreign' / 'EVALUATION.json', {k: v for k, v in dict(old, run='x').items() if k != 'evaluator_version'})
    with pytest.raises(ValueError, match='Incompatible'):
        load_evaluations([tmp_path])


def test_scoreboard_names_old_evaluator_version_beside_goal_gate(tmp_path):
    new, old = _mixed_runs(tmp_path)
    header = '| Run | Gate | Active >=180m | Skill +1 | Quest/letter +1 | Bundle item/donation +1 | Own money | Invented: 0 major, <3% turns (before 1.7.0: =0) |'
    mixed, current = tmp_path / 'mixed.md', tmp_path / 'current.md'
    write_scoreboard(load_evaluations([tmp_path]), mixed)
    text = mixed.read_text(encoding='utf-8')
    assert text.count('not comparable with a 1.6.0 MET') == 1
    assert '- old-run: evaluated by 1.4.0;' in text
    assert text.count(header) == 1
    assert text.index(header) < text.index('evaluated by 1.4.0;') < text.index('## Experience gate')
    assert 'evaluator `1.4.0`.' in text and f'evaluator `{EVALUATOR_VERSION}`.' in text
    write_scoreboard([new], current)
    assert 'not comparable' not in current.read_text(encoding='utf-8')
    assert header in current.read_text(encoding='utf-8')


# --------------------------------------------------------------------------- old evaluations (ADR 103)

GONE = ('goal_gate', 'native_progress', 'experience_gate', 'decision_visibility', 'capability_ledger')
OLD_GATE_ROW = '| old-run | absent | absent | absent | absent | absent | absent | absent |'


def _old_1_2_0(tmp_path, *, drop=GONE):
    """A current evaluation rewritten as an evaluator 1.2.0 file: journey existed, the gate did not."""
    new = evaluate(fixture_run(tmp_path / 'new'))
    write_evaluation(tmp_path / 'new', new)
    old = json.loads(json.dumps(new))
    old['run'], old['evaluator_version'] = 'old-run', '1.2.0'
    for key in drop:
        old.pop(key, None)
    put(tmp_path / 'old' / 'EVALUATION.json', old)
    return new, old


def _board(tmp_path, evaluations, name='board.md'):
    out = tmp_path / name
    write_scoreboard(evaluations, out)
    return out.read_text(encoding='utf-8')


def test_old_evaluation_loads_and_scoreboard_renders_absent_gate(tmp_path):
    new, old = _old_1_2_0(tmp_path)
    loaded = load_evaluations([tmp_path])
    assert sorted(e['run'] for e in loaded) == sorted([new['run'], 'old-run'])
    assert OLD_GATE_ROW in _board(tmp_path, loaded).splitlines()


def test_old_evaluation_experience_and_visibility_rows(tmp_path):
    _old_1_2_0(tmp_path)
    text = _board(tmp_path, load_evaluations([tmp_path]))
    assert '| old-run | ' + ' | '.join(['absent'] * 9) + ' |' in text.splitlines()
    assert '- old-run: unknown (evaluation predates decision visibility).' in text


def test_old_evaluation_keeps_its_recorded_journey_numbers(tmp_path):
    new, old = _old_1_2_0(tmp_path)
    old['journey']['journey_points'] = 7
    old['journey']['partial'] = False
    put(tmp_path / 'old' / 'EVALUATION.json', old)
    row = next(r for r in _board(tmp_path, load_evaluations([tmp_path])).splitlines() if r.startswith('| old-run | ') and '|---' not in r
               and r.count('|') == 16)
    assert row.split(' | ')[0] == '| old-run' and 'absent' not in row and ' 7 |' in row


def test_scoreboard_never_regrades_or_rewrites_an_old_file(tmp_path):
    _old_1_2_0(tmp_path)
    path = tmp_path / 'old' / 'EVALUATION.json'
    before = path.read_bytes()
    loaded = load_evaluations([tmp_path])
    _board(tmp_path, loaded)
    assert path.read_bytes() == before
    assert next(e for e in loaded if e['run'] == 'old-run')['evaluator_version'] == '1.2.0'


def test_old_version_notes_appear_only_with_an_old_run(tmp_path):
    new, _ = _old_1_2_0(tmp_path)
    text = _board(tmp_path, load_evaluations([tmp_path]))
    assert '- old-run: evaluated by 1.2.0;' in text and text.count('not comparable with a 1.6.0 MET') == 1
    assert "- `absent`: that run's evaluator wrote no such section" in text
    current = _board(tmp_path, [json.loads(json.dumps(new))], 'current.md')
    assert 'evaluated by' not in current and 'not comparable' not in current and '- `absent`:' not in current


def test_file_without_journey_renders_fourteen_absent_cells(tmp_path):
    _old_1_2_0(tmp_path, drop=GONE + ('journey',))
    text = _board(tmp_path, load_evaluations([tmp_path]))
    assert '| old-run | ' + ' | '.join(['absent'] * 14) + ' |' in text.splitlines()


def test_rank_tolerates_a_run_missing_identity_and_endurance(tmp_path):
    new, old = _old_1_2_0(tmp_path, drop=GONE + ('identity', 'endurance'))
    current = json.loads(json.dumps(new))
    rank([old, current])
    assert old['verdict']['best_so_far'] is False
    assert current['verdict']['comparison_runs'] == [current['run']]


def _disk_log(tmp_path, rows):
    path = tmp_path / 'disk-projection.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')


def test_host_resource_stop_is_the_machine_not_her_primitives_or_her_budget(tmp_path):
    # a48: something else filled the host disk; the harness wrote stop-reasons disk_projection (monitor: near_floor).
    j = journal(tmp_path, [dict(ev='trial_started', at=110), dict(ev='trial_decision', at=120)])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    put(tmp_path / 'stop-reasons-0.json', dict(utc=utc_of(400), reasons=['disk_projection']))
    put(tmp_path / 'completed.json', dict(ended_utc=utc_of(500)))
    _disk_log(tmp_path, [dict(utc=utc_of(300), allowed=True, decision='headroom', reasons=[]),
                         dict(utc=utc_of(399.5), allowed=False, decision='near_floor', reasons=['near_floor']),
                         dict(utc=utc_of(450), allowed=False, decision='near_floor', reasons=['near_floor', 'projected_floor'])])
    (tmp_path / 'timeline.jsonl').write_text('\n'.join(json.dumps(x) for x in [
        dict(at=150, semantic_progress=True), dict(at=300, semantic_progress=True)]), encoding='utf-8')
    result = endurance(Evidence(tmp_path), [j], [])
    assert (result['stop_class'], result['stop_cause'], result['stop_detail']) == ('host_resource', 'disk_projection', 'near_floor')
    assert result['stop_utc'] == utc_of(400)  # the stop receipt, never completed.json's budget
    assert result['minutes_played'] == round(300 / 60, 3)
    assert result['active_minutes'] == result['minutes_played']  # no terminal stall subtraction on a host stop
    assert result['longest_no_progress_gap_s'] == 150


def test_host_stop_detail_is_absent_without_the_monitor_log_and_never_on_other_stops(tmp_path):
    j = journal(tmp_path, [dict(ev='trial_started', at=110)])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    put(tmp_path / 'stop-reasons-0.json', dict(utc=utc_of(400), reasons=['low_disk', 'disk_projection']))
    result = endurance(Evidence(tmp_path), [j], [])
    assert (result['stop_class'], result['stop_cause'], result['stop_detail']) == ('host_resource', 'low_disk,disk_projection', None)
    put(tmp_path / 'stop-reasons-0.json', dict(utc=utc_of(400), reasons=['host_stop_or_lost_telemetry:process_gone']))
    _disk_log(tmp_path, [dict(utc=utc_of(399), allowed=False, decision='near_floor', reasons=['near_floor'])])
    result = endurance(Evidence(tmp_path), [j], [])
    assert (result['stop_class'], result['stop_detail']) == ('harness', None)


def test_untimed_host_stop_receipt_is_never_budget_complete(tmp_path):
    j = journal(tmp_path, [dict(ev='trial_started', at=110)])
    put(tmp_path / 'window.json', dict(start_at=100, end_at=999))
    put(tmp_path / 'stop-reasons-0.json', dict(reasons=['disk_projection']))  # no utc
    put(tmp_path / 'completed.json', dict(ended_utc=utc_of(500)))
    evidence = Evidence(tmp_path)
    result = endurance(evidence, [j], [])
    assert (result['stop_class'], result['stop_cause']) == ('host_resource', 'disk_projection')
    assert result['stop_utc'] == utc_of(999) and result['minutes_played'] == round(899 / 60, 3)
    assert any('stop-reasons-0.json' in w and 'window.json' in w for w in evidence.warnings)
    # The end is the window's, so a sample from the 45 s after the stop request lies before it; the first refusal is
    # the one the harness stopped on, and a corrupt reasons field is no detail rather than a crash.
    _disk_log(tmp_path, [dict(utc=utc_of(399), allowed=False, decision='near_floor', reasons=['near_floor']),
                         dict(utc=utc_of(450), allowed=False, decision='near_floor', reasons=['near_floor', 'projected_floor'])])
    assert endurance(Evidence(tmp_path), [j], [])['stop_detail'] == 'near_floor'
    _disk_log(tmp_path, [dict(utc=utc_of(399), allowed=False, decision='near_floor', reasons=5)])
    assert endurance(Evidence(tmp_path), [j], [])['stop_detail'] is None


def test_evaluation_and_scoreboard_render_host_resource_and_keep_older_stop_classes(tmp_path):
    run = fixture_run(tmp_path / 'a48')
    put(run / 'stop-reasons-0.json', dict(utc=utc_of(180), reasons=['disk_projection']))
    _disk_log(run, [dict(utc=utc_of(179), allowed=False, decision='near_floor', reasons=['near_floor'])])
    new = evaluate(run)
    write_evaluation(run, new)
    assert new['endurance']['stop_class'] == 'host_resource'
    assert 'Stop **host_resource**: `disk_projection` (near_floor).' in (run / 'EVALUATION.md').read_text(encoding='utf-8')
    assert 'disk-projection.jsonl' in new['evidence_files']
    # The same run as 1.6.1 recorded it: stays primitive on the board, never regraded.
    old = json.loads(json.dumps(new))
    old.update(run='a48-as-recorded', evaluator_version='1.6.1')
    old['endurance'].update(stop_class='primitive')
    del old['endurance']['stop_detail']
    put(tmp_path / 'old' / 'EVALUATION.json', old)
    before = (tmp_path / 'old' / 'EVALUATION.json').read_bytes()
    text = _board(tmp_path, load_evaluations([tmp_path]))
    rows = {}  # first table only: the endurance row of each run
    endurance_table = text.split('| Run | Path / persona', 1)[1].split('##', 1)[0]
    for line in endurance_table.splitlines():
        if line.startswith('| a48'):
            rows.setdefault(line.split(' | ')[0].strip('| '), line)
    assert '| host_resource |' in rows['a48'] and '| primitive |' in rows['a48-as-recorded']
    assert '- a48-as-recorded: evaluated by 1.6.1;' in text
    assert 'host_resource' in text  # the endurance legend names the class
    assert (tmp_path / 'old' / 'EVALUATION.json').read_bytes() == before


def test_hard_disk_floor_detail_and_transient_count_render_without_changing_gate(tmp_path):
    run = fixture_run(tmp_path / 'hard-floor')
    put(run / 'stop-reasons-0.json', dict(utc=utc_of(180), reasons=['disk_projection']))
    before = evaluate(run)
    _disk_log(run, [dict(utc=utc_of(120), allowed=True, event='disk_transient_hold', transition='started'),
                    dict(utc=utc_of(160), allowed=True, event='disk_transient_hold', transition='recovered'),
                    dict(utc=utc_of(175), allowed=True, event='disk_transient_hold', transition='started'),
                    dict(utc=utc_of(179), allowed=False, event='disk_transient_hold', transition='stopped', reasons=['hard_floor']),
                    dict(utc=utc_of(181), allowed=False, reasons=['provider_budget'])])
    result = evaluate(run)
    assert EVALUATOR_VERSION == '1.7.16'
    assert (result['endurance']['stop_class'], result['endurance']['stop_detail']) == ('host_resource', 'hard_floor')
    assert result['endurance']['disk_transient_holds'] == 2
    assert result['goal_gate'] == before['goal_gate']
    write_evaluation(run, result)
    report = (run / 'EVALUATION.md').read_text(encoding='utf-8')
    assert 'Stop **host_resource**: `disk_projection` (hard_floor).' in report
    assert 'Disk transient holds recorded: 2.' in report


def test_transient_holds_count_only_starts_within_play_window_on_any_stop(tmp_path):
    run = fixture_run(tmp_path)
    put(run / 'completed.json', dict(ended_utc=utc_of(180)))
    _disk_log(run, [dict(utc=utc_of(t), event='disk_transient_hold', transition=transition)
                    for t, transition in [(99, 'started'), (100, 'started'), (120, 'recovered'),
                                          (150, 'started'), (180, 'recovered'), (181, 'started')]]
                   + [dict(event='disk_transient_hold', transition='started'), [], 'corrupt'])
    result = evaluate(run)['endurance']
    assert result['stop_class'] == 'budget' and result['stop_detail'] is None
    assert result['disk_transient_holds'] == 2


def test_legacy_run_has_zero_recorded_holds_without_a_monitor_log(tmp_path):
    result = evaluate(fixture_run(tmp_path))
    assert result['endurance']['disk_transient_holds'] == 0
