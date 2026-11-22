"""Hand-checkable synthetic measurement, calibration and W3 replay cases."""
import asyncio
import json
from pathlib import Path
import tempfile

import pytest

from scripts.livepath import replay_timeline as R
from scripts.livepath.replay_report import calibrate, table, values


@pytest.fixture
def repo_tmp_path():
    directory = R.ROOT / '.tmp'
    directory.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='replay-cli-', dir=directory) as name:
        yield Path(name)


def counts():
    return {k: R.Counter() for k in ('replaced', 'expired', 'cancelled')}


def test_five_metrics_with_disjoint_active_play_and_merged_chat():
    owner = R.Event(100, 'owner', 'Arti?', turn_id='o')
    other = R.Event(110, 'owner', 'question?', turn_id='p')
    missed = R.Event(115, 'owner', 'missed?', turn_id='m')
    chat = R.Event(120, 'enqueue', kind='yt_chat', data={'chat_ids': ['c1', 'c2']})
    audio = [(104, 108, 'mic_pasif', owner), (118, 120, 'mic_pasif', other),
             (125, 130, 'yt_chat', chat), (150, 155, 'own_play', None),
             (205, 210, 'game', None), (225, 230, 'own_play', None)]
    result = R.metrics([owner, other, missed], audio, counts(), [(100, 140), (200, 280)], [1, 2, 3])
    assert result['owner_questions'] == dict(total=3, answered=2, share_answered=2/3,
        audio_latency_median_sec=6, audio_latency_p90_sec=7.6)
    assert result['chat_messages'] == dict(incoming=3, answered=2, share_answered=2/3)
    assert result['active_play_sec'] == 120
    assert result['play_lines_per_2min'] == 2
    assert result['longest_silence_active_sec'] == 50


def test_latency_uses_first_audio_and_zero_length_audio_is_not_answered():
    owner = R.Event(100, 'owner', '?')
    missed = R.Event(100, 'owner', '?')
    result = R.metrics([owner, missed], [(102, 103, 'mic', owner), (108, 109, 'mic', owner),
        (104, 104, 'mic', missed)], counts(), (100, 220))
    assert result['owner_questions']['answered'] == 1
    assert result['owner_questions']['audio_latency_median_sec'] == 2


def synthetic_session(tmp_path):
    start = 1794754800.0
    events = [R.Event(start, 'owner', 'Arti?', 'mic_pasif', turn_id='o', named=True, queued_at=start),
        R.Event(start+20, 'chat', 'hello', 'yt_chat', viewer='viewer', line=2),
        R.Event(start+20, 'enqueue', 'hello', 'yt_chat', viewer='viewer', line=3,
                turn_id='c', queued_at=start+20, data={'chat_line': 2}),
        R.Event(start+50, 'enqueue', '[Live7 moment]', 'game', turn_id='g', queued_at=start+50),
        R.Event(start+90, 'enqueue', 'own play', 'own_play', turn_id='p', queued_at=start+90),
        R.Event(start+120, 'media_hold', data={'seconds': 20}),
        R.Event(start+121, 'enqueue', 'video', 'video', turn_id='v', queued_at=start+121)]
    turn = R.Turn('o', 'mic_pasif', start, source='codex', reply_at=start+1, chars=4, voiced_at=start+4,
                  audio_start=start+2, end=start+4.45, synth=1, play=2, reply_text='yes!')
    return R.Session(tmp_path / '2026-11-15_synthetic_bridge.log', start, start+180, events, [turn], {})


def test_labels_replace_fallback_and_do_not_export_categories(tmp_path):
    session = synthetic_session(tmp_path)
    unlabelled = R.Event(session.start+10, 'owner', 'no question mark', 'mic_pasif', turn_id='labelled')
    session.events.append(unlabelled)
    assert session.events[0].question
    assert not unlabelled.question
    path = tmp_path / 'labels.tsv'
    path.write_text('log\tturn_id\tcategory\tshould_answer\tshe_answered\tsensitive\n'
        f'{session.path.name}\tlabelled\tPRIVATE CATEGORY\tyes\tno\tyes\n', encoding='utf-8')
    assert R.read_labels(path, [session]) == 1
    assert not session.events[0].question
    assert unlabelled.question
    assert 'PRIVATE CATEGORY' not in json.dumps(R.observed_metrics([session], (session.start, session.end)))


def test_calibration_fields_use_before_arm_and_table_has_errors():
    baseline = R.metrics([], [], counts(), (100, 220))
    calibration = calibrate(baseline, baseline)
    assert calibration['status'] == 'INCONCLUSIVE'
    assert set(('owner_audio_median_sec', 'chat_voiced', 'mic_replaced', 'longest_silence_active_sec')) <= calibration['replay_minus_measured'].keys()
    assert calibration['replay_minus_measured']['longest_silence_active_sec'] == 0
    summary = table({'plain': {'metrics': baseline, 'calibration': calibration}})
    assert summary.count('cal ±') == 8
    assert 'INCONCLUSIVE' in summary


def test_calibration_ok_and_material_deviation():
    owner = R.Event(100, 'owner', '?')
    chat = R.Event(110, 'enqueue', data={'chat_ids': ['c']})
    measured = R.metrics([owner], [(102, 103, 'mic', owner), (112, 113, 'yt_chat', chat)],
                         counts(), (100, 220), [chat])
    assert calibrate(measured, measured)['status'] == 'OK'
    changed = {**measured, 'longest_silence_active_sec': 130}
    result = calibrate(changed, measured)
    assert result['replay_minus_measured']['longest_silence_active_sec'] == 23
    assert result['status'] == 'INCONCLUSIVE'


def test_plain_config_uses_real_w3_mark_and_unmarked_mic_keeps_chat(tmp_path):
    session = synthetic_session(tmp_path)
    session.events = [session.events[1], session.events[2],
        R.Event(session.start+21, 'owner', 'aduh panennya banyak banget hari ini', 'mic_pasif',
                queued_at=session.start+21, turn_id='unmarked')]
    cfg = R.build_config('plain', session)
    assert cfg['mic_priority_mark'] is True
    result = R.replay([session], R.DurationModel.fixed(4, 1, 2), 'plain',
        active=(session.start, session.end))
    assert result['chat_messages']['answered'] == 1
    assert result['items']['cancelled'] == 0
    assert result['items']['replaced'] == 0
    worker = R.Replay(R.DurationModel.fixed(4, 1, 2), (session.start, session.end))
    asyncio.run(worker.session(session, cfg, []))
    assert [a[2] for a in worker.audio] == ['yt_chat', 'mic_pasif']
    assert worker.b.speech_control.owner_revision == 0


def test_rotation_merges_messages_and_prioritizes_new_viewer(tmp_path):
    session = synthetic_session(tmp_path)
    start = session.start
    session.events = [R.Event(start, 'owner', 'Arti?', 'mic_pasif', turn_id='o', queued_at=start)]
    for i, viewer in enumerate(['heavy', 'heavy', 'heavy', 'new']):
        at = start+1+i
        session.events += [R.Event(at, 'chat', 'ordinary words', 'yt_chat', viewer=viewer, line=10+i),
            R.Event(at, 'enqueue', 'ordinary words', 'yt_chat', viewer=viewer, line=20+i,
                    queued_at=at, turn_id=f'c{i}', data={'chat_line': 10+i})]
    cfg = {**R.build_config('plain', session), 'yt_chat_queue_max': 12}
    worker = R.Replay(R.DurationModel.fixed(4, 1, 2), (start, session.end))
    asyncio.run(worker.session(session, cfg, []))
    assert [e.viewer for a, b, k, e in worker.audio if k == 'yt_chat'] == ['new', 'heavy']
    result = R.metrics(worker.owners, worker.audio, worker.counts, worker.active, worker.chats)
    assert result['chat_messages'] == dict(incoming=4, answered=4, share_answered=1)
    assert result['chat_replies_total'] == 2


def test_rotation_uses_raw_chat_instead_of_truncated_queue_display(tmp_path):
    session = synthetic_session(tmp_path)
    start = session.start
    session.events = [session.events[0]]
    for i in range(2):
        at = start+1+i
        session.events += [R.Event(at, 'chat', 'ordinary words', 'yt_chat', viewer='Arti', line=10+i),
            R.Event(at, 'enqueue', 'Viewer Arti: ordinary words', 'yt_chat', viewer='Arti',
                    turn_id=f'c{i}', queued_at=at, line=20+i,
                    data={'chat_line': 10+i, 'chat_text': 'ordinary words'})]
    result = R.replay([session], R.DurationModel.fixed(4, 1, 2), 'plain',
                      active=(start, session.end))
    assert result['chat_messages']['answered'] == 2
    assert result['chat_replies_total'] == 1


def test_media_hold_filters_chat_but_passes_marked_owner(tmp_path):
    session = synthetic_session(tmp_path)
    start = session.start
    session.events = [R.Event(start, 'media_hold', data={'seconds': 30}),
        R.Event(start+1, 'chat', 'ordinary words', 'yt_chat', viewer='v', line=1),
        R.Event(start+1, 'enqueue', 'ordinary words', 'yt_chat', viewer='v', line=2,
                queued_at=start+1, turn_id='c', data={'chat_line': 1}),
        R.Event(start+2, 'owner', 'Arti, jawab dong', 'mic_pasif', queued_at=start+2, turn_id='o')]
    worker = R.Replay(R.DurationModel.fixed(1, 1, 2), (start, session.end))
    asyncio.run(worker.session(session, R.build_config('plain', session), []))
    assert [a[2] for a in worker.audio] == ['mic_pasif', 'yt_chat']
    assert worker.audio[0][0] < start+30
    assert worker.audio[1][0] >= start+30


def test_own_play_gap_is_real_admission_gate(tmp_path):
    session = synthetic_session(tmp_path)
    start = session.start
    session.events = [R.Event(start+i, 'enqueue', 'play', 'own_play', queued_at=start+i,
                              turn_id=f'p{i}') for i in (0, 10, 30)]
    cfg = {**R.build_config('plain', session), 'live7_own_play_enabled': True, 'own_play_min_gap_sec': 25}
    result = R.replay([session], R.DurationModel.fixed(1, 1, 2), 'plain',
        config_factory=lambda *_: cfg, active=(start, session.end))
    assert result['game_comments_active'] == 2
    assert result['play_lines_per_2min'] == pytest.approx(4/3)


def test_desk_rechecks_bridge_expiry_before_starting_generation(tmp_path):
    session = synthetic_session(tmp_path)
    session.ready = session.start+40
    session.events = [R.Event(session.start, 'enqueue', '[Live7 moment]', 'game',
                              queued_at=session.start, turn_id='stale')]
    result = R.replay([session], R.DurationModel.fixed(1, 1, 2), 'desk',
                      active=(session.start, session.end))
    assert result['generations']['started'] == 0
    assert result['items']['expired_by_kind'] == {'game': 1}


def test_config_overlay_hook_does_not_require_harness_changes(tmp_path):
    session = synthetic_session(tmp_path)
    cfg = R.build_config('desk_v2', session, arms={'desk_v2': {'attention_desk_v2': True}})
    assert cfg['attention_desk_v2'] is True
    assert cfg['mic_priority_mark'] is True


def test_generated_reply_waits_for_media_and_audio_is_interrupted(tmp_path):
    session = synthetic_session(tmp_path)
    start = session.start
    session.events = [session.events[1], session.events[2],
        R.Event(start+21, 'media_hold', data={'seconds': 20})]
    worker = R.Replay(R.DurationModel.fixed(4, 1, 2), (start, session.end))
    asyncio.run(worker.session(session, R.build_config('plain', session), []))
    assert worker.audio[0][0] >= start+41
    session.events[-1] = R.Event(start+26, 'media_hold', data={'seconds': 20})
    worker = R.Replay(R.DurationModel.fixed(1, 1, 10), (start, session.end))
    asyncio.run(worker.session(session, R.build_config('plain', session), []))
    assert worker.audio[0][1] == start+26
    assert worker.counts['cancelled']['yt_chat'] == 1


def test_cli_writes_content_free_json_per_arm_and_one_table(repo_tmp_path, monkeypatch, capsys):
    tmp_path = repo_tmp_path
    session = synthetic_session(tmp_path)
    log = session.path
    log.write_text('synthetic input identity', encoding='utf-8')
    cfg = tmp_path / 'recorded.json'
    cfg.write_text(json.dumps({'voice_queue_enabled': True, 'mic_priority_mark': True}), encoding='utf-8')
    monkeypatch.setattr(R, 'read_session', lambda *_: session)
    out = tmp_path / 'output'
    args = ['--log', str(log), '--recorded-config', str(cfg), '--output-dir', str(out),
        '--active-start', R.datetime.fromtimestamp(session.start, R.WIB).isoformat(),
        '--active-end', R.datetime.fromtimestamp(session.end, R.WIB).isoformat()]
    assert R.main(args) == 0
    assert sorted(p.name for p in out.iterdir()) == ['before.json', 'desk.json', 'plain.json', 'summary.md']
    output = capsys.readouterr().out + ''.join(p.read_text(encoding='utf-8') for p in out.iterdir())
    for secret in ['Arti?', 'hello', 'own play', 'yes!', 'synthetic input identity', 'viewer']:
        assert secret not in output
    before = json.loads((out / 'before.json').read_text())
    plain = json.loads((out / 'plain.json').read_text())
    assert before['calibration'] == plain['calibration']
    assert before['calibration']['replay_minus_measured']['owner_audio_median_sec'] is not None
    assert values(plain['metrics'])['play_lines_per_2min'] >= 0


def test_cli_discovers_synthetic_run_and_applies_labels(repo_tmp_path, capsys):
    run = repo_tmp_path / 'run'
    logs = run / 'session_logs'
    logs.mkdir(parents=True)
    log = logs / '2026-11-15_220000_bridge.log'
    log.write_text('\n'.join([
        '22:00:00.000 [History Recorded] [22:00:00] [Streamer] Arti, SYNTHETIC_SECRET?',
        '22:00:00.000 [Queue] Trigger (mic_pasif) depth=1: "Arti, SYNTHETIC_SECRET?"',
        '22:00:00.000 [Livepath] {"event":"queued","kind":"mic_pasif","turn_id":"o","name_addressed":true}',
        '22:00:00.000 [Livepath] {"event":"start","kind":"mic_pasif","turn_id":"o"}',
        '22:00:01.000 Arti menjawab: "SYNTHETIC_REPLY"',
        '22:00:04.000 [Livepath] {"event":"voiced","kind":"mic_pasif","turn_id":"o","source":"codex"}',
        '22:00:04.000 [Latency] tts_synth=1000ms tts_play=2000ms',
        '22:00:04.450 [Livepath] {"event":"end","kind":"mic_pasif","turn_id":"o"}',
        '22:00:20.000 💬 [YT Chat] @SYNTHETIC_VIEWER: SYNTHETIC_CHAT',
        '22:00:20.000 [Queue] Trigger (yt_chat) depth=1: "SYNTHETIC_CHAT"',
        '22:00:20.000 [Livepath] {"event":"queued","kind":"yt_chat","turn_id":"c"}',
        '22:00:50.000 [Queue] Trigger (game) depth=1: "[Live7 moment]"',
        '22:00:50.000 [Livepath] {"event":"queued","kind":"moment","turn_id":"g"}',
        '22:02:00.000 [Video] Playback hold 20s',
        '22:02:01.000 [Queue] Trigger (video) depth=1: "SYNTHETIC_VIDEO"',
        '22:02:01.000 [Livepath] {"event":"queued","kind":"video","turn_id":"v"}',
        '22:03:00.000 [end]',
    ]), encoding='utf-8')
    session = R.read_session(log)
    journal = run / 'runtime/live7/c1/journal/g1_journal.jsonl'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'at': session.start+50, 'ev': 'live7_moment_emitted', 'kind': 'bed'}), encoding='utf-8')
    labels = repo_tmp_path / 'labels.tsv'
    labels.write_text('log\tturn_id\tcategory\tshould_answer\tshe_answered\tsensitive\n'
        f'{log.name}\to\tSYNTHETIC_CATEGORY\tyes\tyes\tyes\n', encoding='utf-8')
    cfg = repo_tmp_path / 'recorded.json'
    cfg.write_text(json.dumps({'voice_queue_enabled': True, 'mic_priority_mark': True,
                              'live7_moments_enabled': True}), encoding='utf-8')
    out = repo_tmp_path / 'output'
    assert R.main(['--run', str(run), '--recorded-config', str(cfg), '--labels', str(labels),
        '--active-start', '2026-11-15T22:00:00+07:00', '--active-end', '2026-11-15T22:03:00+07:00',
        '--output-dir', str(out)]) == 0
    payloads = [json.loads((out / f'{name}.json').read_text()) for name in ('before', 'plain', 'desk')]
    assert all(p['labels_used'] for p in payloads)
    assert payloads[1]['metrics']['chat_messages']['incoming'] == 1
    assert payloads[1]['metrics']['chat_messages']['answered'] == 1
    assert payloads[1]['metrics']['owner_questions']['answered'] == 1
    assert payloads[1]['metrics']['game_comments_active'] == 1
    output = capsys.readouterr().out + ''.join(p.read_text() for p in out.iterdir())
    assert 'SYNTHETIC_' not in output


def test_labels_match_only_owner_events_when_an_enqueue_shares_the_turn_id(tmp_path):
    """A real stream log can carry an enqueue event with the same turn id as a labelled owner line."""
    session = synthetic_session(tmp_path)
    owner = R.Event(session.start+10, 'owner', 'tolong cek', 'mic_pasif', turn_id='t-0001')
    twin = R.Event(session.start+10, 'enqueue', 'tolong cek', 'mic_pasif', turn_id='t-0001')
    session.events += [owner, twin]
    path = tmp_path / 'labels.tsv'
    path.write_text('log\tturn_id\tcategory\tshould_answer\tshe_answered\tsensitive\n'
        f'{session.path.name}\tt-0001\towner_to_her\tyes\tyes\tno\n', encoding='utf-8')
    assert R.read_labels(path, [session]) == 1
    assert owner.data['should_answer'] is True and twin.data['should_answer'] is False
