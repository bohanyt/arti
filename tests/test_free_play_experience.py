"""Evaluator 1.4.0: transcript-only experience metrics and the report-only experience gate (ADR 75).

Synthetic transcripts only, shaped like `arti_bridge_testmode.Transcript` rows. No real session data.
"""
import json

from scripts.free_play import experience as X
from scripts.free_play.evaluate import EVALUATOR_VERSION, Evidence, evaluate, markdown, rank, write_evaluation
from scripts.free_play.goal_gate import VERSION as GOAL_GATE_VERSION, goal_gate, native_progress, semantic_counter
from scripts.free_play.scoreboard import write_scoreboard
from tests.test_free_play_evaluate import fixture_run

T0 = 1_700_000_000.0
MOODS = ['senang', 'excited', 'bingung', 'sedih', 'neutral']
# Distinct sentences so repetition metrics measure the metric, not the fixture template.
VARIED = ['wah ikan gede banget tadi hampir lepas', 'hujan lagi, tanaman pasti seneng', 'aku penasaran isi gua lantai lima',
          'kayaknya Pierre naikin harga benih deh', 'surat dari Willy, dia bilang dermaga udah dibenerin', 'uang tinggal dikit, jual parsnip dulu kali ya',
          'ada burung aneh di deket rumah Marnie', 'besok festival telur, aku mau ikut lomba', 'capek banget, tapi kebun udah rapi',
          'eh kok ada batu meteor di padang rumput', 'Linus baik banget ngasih resep', 'lupa nyiram, semoga ga layu',
          'panen stroberi pertama, rasanya manis sekali', 'aku mau coba mancing di laut malam ini', 'Lewis nitip pesan soal pusat komunitas',
          'kucingku ngikutin terus dari tadi', 'bunga musim semi bisa dijual ke Pierre', 'sepatuku kena lumpur, ya sudahlah',
          'ketemu geode, nanti dibuka ke Clint', 'museum masih kosong, sedih liatnya', 'pohon ceri di sebelah peternakan mulai berbunga',
          'tambang lantai sepuluh ada tangga baru', 'aku pengen beli ayam kalau duitnya cukup', 'hari ini mendung tapi ga hujan',
          'Haley nanya kenapa bajuku kotor', 'rumput liar numbuh lagi di jalur ke kota', 'ada kepiting di pantai, lucu jalannya',
          'kapak baru bikin nebang jadi cepat', 'air sumur dingin banget pagi ini', 'tadi nyasar di hutan rahasia sebentar']


def turn(n, t, source='game', reply='wah ikan!', emotion='senang', latency_ms=1200, queued_wait_s=0.5, **extra):
    row = dict(ev='turn', n=n, t=T0 + t, utc='2026-11-03T00:00:00Z', source=source, trigger_type=source, input='x',
               reply=reply, silent=False, provider_source='codex', emotion=emotion, latency_ms=latency_ms,
               queued_wait_s=queued_wait_s, shown_discovery_ids=[], discoveries=[])
    row.update(extra)
    return row


def ev(name, t, **fields):
    return dict(ev=name, t=T0 + t, utc='2026-11-03T00:00:00Z', **fields)


def rows(items):
    return list(enumerate(items, 1))


def metrics(items):
    return X.experience_metrics(rows(items))


def gate(items, semantic=None, persona=None):
    return X.experience_criteria(metrics(items), semantic, persona)


def session(n_replies, every_s=120, start=0, end=None, **kw):
    """n spontaneous replies every `every_s` seconds inside explicit session bounds."""
    end = end if end is not None else start + every_s * (n_replies + 1)
    items = [ev('test_start', start)]
    items += [turn(i + 1, start + every_s * (i + 1), **{'reply': VARIED[i % len(VARIED)], 'emotion': MOODS[i % len(MOODS)], **kw}) for i in range(n_replies)]
    items.append(ev('test_end', end))
    return items


# ----------------------------------------------------------------------------- edge cases

def test_empty_transcript_is_unknown_everywhere():
    m = metrics([])
    assert m['population']['turns'] == 0
    for key in ('silence_gaps', 'spontaneous_rate', 'repeated_opener', 'near_duplicates', 'mood_range', 'dropped_events', 'interrupts', 'reply_latency'):
        assert m[key]['value'] is None and m[key]['missing_signal'], key
    g = gate([])
    assert {c['status'] for c in g.values()} == {'UNKNOWN'}
    assert g['LX-5']['missing_signal'] == 'empty transcript'
    assert X.summarize(g)['status'] == 'UNKNOWN' and X.summarize(g)['automatic_status'] == 'UNKNOWN'


def test_single_reply_without_bounds_is_unknown_not_a_verdict():
    m = metrics([turn(1, 10)])
    assert m['silence_gaps']['value'] is None and 'under 600' in m['silence_gaps']['missing_signal']
    assert 'fewer than two' in metrics([turn(1, 10), turn(2, 2000, source='owner_probe')])['silence_gaps']['missing_signal']
    assert m['spontaneous_rate']['value'] is None  # zero span
    assert m['repeated_opener']['value'] is None and m['near_duplicates']['value'] is None  # below MIN_REPLIES
    assert m['mood_range']['value'] is None and 'fewer than' in m['mood_range']['missing_signal']
    assert m['interrupts']['value'] == 0 and m['reply_latency']['p50_ms'] == 1200
    g = gate([turn(1, 10)])
    assert g['LX-9']['status'] == g['LX-10']['status'] == g['LX-11']['status'] == 'UNKNOWN'
    assert g['LX-12']['status'] == 'MET' and g['LX-5']['status'] == 'MET'


def test_missing_fields_are_unknown_never_zero():
    items = [turn(i + 1, 60 * (i + 1)) for i in range(12)]
    for row in items:
        row.pop('emotion'); row.pop('latency_ms'); row.pop('queued_wait_s'); row.pop('t'); row['utc'] = 'not a time'
    m = metrics(items)
    assert m['mood_range']['missing_signal'] == 'emotion field absent from every reply turn'
    assert m['interrupts']['overlaps'] is None and m['interrupts']['queued_wait_p95_s'] is None
    assert m['reply_latency']['value'] is None
    assert m['silence_gaps']['value'] is None and m['spontaneous_rate']['value'] is None
    g = gate(items)
    assert g['LX-12']['status'] == 'UNKNOWN' and g['LX-11']['status'] == 'UNKNOWN' and g['LX-9']['status'] == 'UNKNOWN'
    assert g['LX-10']['status'] == 'NOT_MET'  # 12 identical replies: text-only metric still known


# ----------------------------------------------------------------------------- each metric

def test_silence_gaps_median_max_and_long_count_include_session_bounds():
    items = [ev('test_start', 0), turn(1, 600), turn(2, 900, source='owner_probe'), turn(3, 1200), turn(4, 2400),
             turn(5, 2460, source='game', reply='', silent=True), ev('test_end', 3600)]
    g = metrics(items)['silence_gaps']
    # gaps: 600 (start->1), 600 (1->3), 1200 (3->4), 1200 (4->end); the probe and the silent turn do not break a gap
    assert (g['median_s'], g['max_s'], g['count_over_15min'], g['gaps']) == (900.0, 1200.0, 2, 4)
    assert g['evidence'] == ['bridge/bridge-transcript.jsonl:J4', 'bridge/bridge-transcript.jsonl:J5']


def test_silence_without_any_reply_is_one_long_gap():
    g = metrics([ev('test_start', 0), turn(1, 100, source='owner_probe'), ev('test_end', 1800)])['silence_gaps']
    assert (g['max_s'], g['count_over_15min'], g['gaps']) == (1800.0, 1, 1)


def test_short_transcript_span_is_unknown_for_rate_and_gaps():
    m = metrics([ev('test_start', 0), turn(1, 100), turn(2, 200), ev('test_end', 300)])
    assert m['spontaneous_rate']['value'] is None and 'under 600' in m['spontaneous_rate']['missing_signal']
    assert m['silence_gaps']['value'] is None


def test_spontaneous_rate_excludes_probes_silent_and_unavailable_replies():
    items = [ev('test_start', 0), turn(1, 100), turn(2, 200, source='owner_probe'), turn(3, 300, reply='', silent=True),
             turn(4, 400, reply='(maaf, lagi gangguan)', provider_source='unavailable:deadline'), turn(5, 500, source='discovery'),
             turn(6, 600, source='chat', reply='halo'), ev('test_end', 1800)]
    r = metrics(items)['spontaneous_rate']
    assert r['count'] == 2 and r['per_hour'] == 4.0
    assert metrics(items)['population']['probe_replies'] == 1


def test_repeated_opener_share_and_min_sample():
    items = [turn(i + 1, 60 * i, reply=('Bohan, aku lagi sibuk' if i < 7 else f'eh ada ikan {i}')) for i in range(10)]
    r = metrics(items)['repeated_opener']
    assert r['top_opener'] == 'bohan aku' and r['top_share'] == 0.7 and r['top3_share'] == 0.7
    assert len(r['evidence']) == 3
    assert metrics(items[:9])['repeated_opener']['value'] is None
    assert gate(items)['LX-10']['components']['top_opener_share']['pass'] is False


def test_near_duplicates_use_window_and_jaccard():
    base = 'aku senang banget hari ini panen stroberi di kebun'
    items = [turn(i + 1, 60 * i, reply=VARIED[i]) for i in range(10)]
    items.append(turn(11, 700, reply=base))
    items.append(turn(12, 760, reply=base + '!'))          # near-duplicate of 11
    items.append(turn(13, 820, reply='cerita yang sama sekali berbeda tentang gua'))
    d = metrics(items)['near_duplicates']
    assert d['duplicates'] == 1 and d['rate'] == round(1 / 13, 3)
    assert d['evidence'] == ['bridge/bridge-transcript.jsonl:J12']
    # the same text 25 replies later is outside the 20-reply window
    far = items + [turn(14 + i, 900 + 60 * i, reply=VARIED[10 + i] if i < 20 else 'cuaca cerah, aku mau jalan ke pantai sebentar') for i in range(21)]
    far.append(turn(40, 3000, reply=base))
    assert metrics(far)['near_duplicates']['duplicates'] == 1
    assert X.jaccard(set(), set()) == 0.0


def test_mood_range_counts_distinct_top_and_null_share():
    moods = ['senang'] * 6 + ['sedih', 'excited', None, '']
    items = [turn(i + 1, 60 * i, emotion=m) for i, m in enumerate(moods)]
    r = metrics(items)['mood_range']
    assert (r['distinct'], r['top_share'], r['null_share']) == (3, 0.6, 0.2)
    assert r['counts'] == dict(excited=1, sedih=1, senang=6)
    c = gate(items)['LX-9']['components']
    assert (c['distinct']['pass'], c['top_share']['pass'], c['null_share']['pass']) == (False, True, False)


def ctx(n, t, identity, tier='high'):
    return ev('discovery_context', t, event=dict(id=identity, importance=tier, count=1), disposition='pending_reaction')


def test_dropped_events_accounting():
    items = [ctx(1, 1, 'e-reacted'), ctx(2, 2, 'e-silent'), ctx(3, 3, 'e-shown-later'), ctx(4, 4, 'e-lost'),
             ctx(5, 5, 'e-low', 'low'), ctx(6, 6, 'e-reacted'),  # duplicate context row of a delivered event
             turn(1, 10, source='game', trigger_type='discovery', shown_discovery_ids=['e-reacted']),
             ev('discovery_delivery', 10, turn_n=1, event_ids=['e-reacted'], status='reacted'),
             turn(2, 20, reply='', silent=True), ev('discovery_delivery', 20, turn_n=2, event_ids=['e-silent'], status='persona_silent'),
             ev('discovery_delivery', 25, turn_n=3, event_ids=['e-retry'], status='retry_pending'),
             turn(3, 30, source='owner_probe', shown_discovery_ids=['e-shown-later']),
             ev('game_trigger_dropped', 40, reason='expired_or_not_current'), ev('turn_skipped', 41, reason='max_turns'),
             ev('turn_unanswered_at_end', 50, source='owner_probe'), ev('discoveries_pending_at_end', 50, event_ids=['e-lost'])]
    d = metrics(items)['dropped_events']
    assert d['undelivered_ids'] == ['e-lost'] and d['high_events'] == 4
    assert (d['high_undelivered'], d['game_triggers_dropped'], d['turns_skipped'], d['unanswered_at_end'], d['value']) == (1, 1, 1, 1, 4)
    assert 'bridge/bridge-transcript.jsonl:J4' in d['evidence']
    assert gate(items)['LX-5']['status'] == 'NOT_MET'
    assert metrics([turn(1, 1)])['dropped_events']['value'] == 0


def test_overlap_is_a_turn_started_before_the_previous_one_was_written():
    items = [turn(1, 10, latency_ms=3000), turn(2, 20, latency_ms=5000), turn(3, 32, latency_ms=11000, queued_wait_s=30.0), turn(4, 40, latency_ms=8000)]
    i = metrics(items)['interrupts']
    # turn 3 started at 21 (< 20? no); turn 4 started at 32.0 == turn 3 end: not an overlap. Make one explicitly:
    assert i['overlaps'] == 0
    items.append(turn(5, 41, latency_ms=4000))  # started at 37 < 40
    i = metrics(items)['interrupts']
    assert i['overlaps'] == 1 and i['evidence'] == ['bridge/bridge-transcript.jsonl:J5']
    assert i['queued_wait_p95_s'] == 30.0 and i['queued_wait_max_s'] == 30.0
    c = gate(items)['LX-12']['components']
    assert c['overlaps']['pass'] is False and c['queued_wait_p95_s']['pass'] is False


def test_reply_latency_percentiles_are_nearest_rank():
    items = [turn(i + 1, 60 * i, latency_ms=100 * (i + 1)) for i in range(20)]
    r = metrics(items)['reply_latency']
    assert (r['p50_ms'], r['p95_ms'], r['max_ms'], r['replies']) == (1000, 1900, 2000, 20)
    assert X.percentile([], 0.5) is None and X.percentile([7], 0.95) == 7


# ----------------------------------------------------------------------------- the gate

def test_judge_and_owner_criteria_are_unknown_with_named_missing_signals():
    g = gate(session(12))
    for lx in ('LX-2', 'LX-3', 'LX-4', 'LX-7', 'LX-8', 'LX-13'):
        assert g[lx]['status'] == 'UNKNOWN' and g[lx]['missing_signal'] == 'judge_not_run' and g[lx]['scorer'] == 'J'
    assert g['LX-14']['missing_signal'] == 'owner_rating_missing' and g['LX-14']['scorer'] == 'O'
    assert gate(session(12), persona=dict(rated=10))['LX-14']['missing_signal'] == 'owner_choice_1_pending'
    assert g['LX-11']['tier'] == 'S (H if owner choice 2a)'
    assert {c['id'] for c in g.values()} == {f'LX-{i}' for i in range(1, 15)}


def test_lx1_and_lx6_follow_the_semantic_review_counter():
    v2 = 'arti.free_play.semantic_review/2'
    g = gate(session(12), dict(value=None, missing_signal='no review', evidence=[]))
    assert g['LX-1']['status'] == 'UNKNOWN' and g['LX-1']['missing_signal'] == 'no review' and g['LX-6']['status'] == 'UNKNOWN'
    g = gate(session(12), dict(value=0, review_schema=v2, complete=False, evidence=['r.json'], supported_claims=5, unverifiable_claims=1))
    assert g['LX-1']['status'] == 'UNKNOWN' and g['LX-6']['status'] == 'UNKNOWN'  # incomplete v2 never establishes zero
    g = gate(session(12), dict(value=0, review_schema=v2, complete=True, zero_admissible=True, reply_turns=12, evidence=['r.json'], supported_claims=7, unverifiable_claims=3))
    assert g['LX-1']['status'] == 'MET' and g['LX-6']['status'] == 'MET' and g['LX-6']['value'] == 0.7
    g = gate(session(12), dict(value=2, review_schema=v2, complete=True, evidence=['r.json'], supported_claims=1, unverifiable_claims=3))
    assert g['LX-1']['status'] == 'NOT_MET' and g['LX-6']['status'] == 'NOT_MET'
    g = gate(session(12), dict(value=0, review_schema=v2, complete=True, evidence=['r.json'], supported_claims=0, unverifiable_claims=0))
    assert g['LX-6']['missing_signal'] == 'no_factual_claims'
    # ADR 117: a legacy review names no independent reviewer, so its zero cannot earn MET; its positive count still fails.
    assert gate(session(12), dict(value=0, review_schema='legacy', complete=False, evidence=['r.json']))['LX-1']['status'] == 'UNKNOWN'
    assert gate(session(12), dict(value=1, review_schema='legacy', complete=False, evidence=['r.json']))['LX-1']['status'] == 'NOT_MET'


def test_a_lively_session_meets_the_automatic_criteria():
    items = [ev('test_start', 0)]
    items += [turn(i + 1, 300 * (i + 1), emotion=MOODS[i % 5], reply=VARIED[i]) for i in range(23)]
    items.append(ev('test_end', 7200))
    g = gate(items, dict(value=0, review_schema='legacy', complete=True, zero_admissible=True, reply_turns=23, evidence=['r.json']))
    s = X.summarize(g)
    assert g['LX-11']['value'] == dict(spontaneous_per_hour=11.5, median_gap_min=5.0, longest_gap_min=5.0)
    assert s['automatic_status'] == 'UNKNOWN' and s['automatic_counts'] == dict(MET=6, NOT_MET=0, UNKNOWN=1)  # LX-6 needs v2
    assert s['status'] == 'UNKNOWN' and s['counts']['UNKNOWN'] == 8


def test_a_statue_session_fails_lx11_and_lx10_but_keeps_overall_unknown():
    items = [ev('test_start', 0)] + [turn(i + 1, 1500 * (i + 1), reply='Bohan, aku masih di sini ya') for i in range(10)] + [ev('test_end', 18000)]
    g = gate(items)
    assert g['LX-11']['status'] == 'NOT_MET' and g['LX-10']['status'] == 'NOT_MET'
    assert X.summarize(g)['status'] == 'NOT_MET' and X.summarize(g)['automatic_status'] == 'NOT_MET'


# ----------------------------------------------------------------------------- evaluator integration

def test_evaluate_adds_the_section_and_leaves_goal_gate_untouched(tmp_path):
    run = fixture_run(tmp_path)
    (run / 'bridge').mkdir()
    (run / 'bridge/bridge-transcript.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in session(12)), encoding='utf-8')
    result = evaluate(run)
    assert result['evaluator_version'] == EVALUATOR_VERSION == '1.7.16' and GOAL_GATE_VERSION == '1.7.0'
    # The goal gate is still the 1.3.8 measurement of the same inputs; the experience gate adds beside it, never into it.
    evidence = Evidence(run)
    journals = [(p, evidence.rows(p)) for p in sorted(run.glob('runtime/live7/*/day-*/journal/g1_journal.jsonl'))]
    expected = goal_gate(result['endurance'], native_progress(evidence, journals, ''), semantic_counter(evidence, journals))
    assert json.dumps(result['goal_gate'], sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert set(result['goal_gate']) == {'version', 'status', 'criteria', 'definition', 'measured_criteria'} and 'LX-5' not in json.dumps(result['goal_gate'])
    assert result['goal_gate']['measured_criteria']['enabled'] is False
    g = result['experience_gate']
    assert g['report_only'] is True and g['provisional'] is True and g['version'] == EVALUATOR_VERSION
    assert g['criteria']['LX-5']['status'] == 'MET' and g['status'] == 'UNKNOWN'
    assert g['metrics']['population']['spontaneous_replies'] == 12
    assert 'bridge/bridge-transcript.jsonl' in result['evidence_files']
    rank([result])
    assert result['verdict']['ranking_basis'].startswith('maximum ARMED-to-stop minutes')
    write_evaluation(run, result)
    text = (run / 'EVALUATION.md').read_text(encoding='utf-8')
    assert '## Experience gate (provisional, report-only)' in text and '| LX-11 | Not a statue |' in text
    assert text.index('## Goal gate') < text.index('## Experience gate') < text.index('## Verdict')
    assert len([x for x in markdown(result).splitlines() if x.startswith('## ')]) == 13


def test_scoreboard_gets_a_separate_section_and_keeps_existing_columns(tmp_path):
    result = evaluate(fixture_run(tmp_path))
    rank([result])
    board = tmp_path / 'SCOREBOARD_AUTO.md'
    write_scoreboard([result], board)
    text = board.read_text(encoding='utf-8')
    main_header = next(l for l in text.splitlines() if l.startswith('| Run | Path / persona |'))
    assert main_header.endswith('| Stop class | Best |') and 'LX' not in main_header
    goal_header = next(l for l in text.splitlines() if l.startswith('| Run | Gate |'))
    assert goal_header == '| Run | Gate | Active >=180m | Skill +1 | Quest/letter +1 | Bundle item/donation +1 | Own money | Invented: 0 major, <3% turns (before 1.7.0: =0) |'
    assert '## Experience gate (provisional, report-only)' in text
    assert text.index('## Goal gate') < text.index('## Experience gate') < text.index('## Explicit inputs')
    assert '| Run | Provisional | Automatic | LX-1 invented |' in text
    row = next(l for l in text.splitlines() if l.startswith(f"| {result['run']} | UNKNOWN | UNKNOWN |"))
    assert 'LX' not in row and row.count('|') == 11
