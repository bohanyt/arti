"""Evaluator 1.4.1: blind judge review (experience_review/1), owner page export and calibration (ADR 92).

Synthetic transcripts only; the provider is a fake. No model is called and no real session data exists here.
"""
import hashlib
import json

import pytest

from scripts.free_play import experience_review_template as R
from scripts.free_play.evaluate import EVALUATOR_VERSION, Evidence, evaluate, markdown, write_evaluation
from scripts.free_play.experience_judge import bind_labels, main as judge_main, prepare, run_judge
from scripts.free_play.goal_gate import goal_gate, native_progress, semantic_counter
from scripts.free_play.journey import persona
from tests.test_free_play_evaluate import fixture_run, journal, put
from tests.test_free_play_experience import VARIED, ev, turn

PRIVATE = 'SYSTEM PROMPT soul text must never leak'


def good(**over):
    labels = dict(mode='react', relevance='answers_intent', personality=['feeling'], why_match='n/a', role_confusion=False,
                  sensitive_topic='none', sensitive_breach=False, flag=False, note='')
    labels.update(over)
    return labels


def session(tmp_path, n_spont=12, n_probe=12, why=True):
    items = [ev('test_start', 0)]
    k = 0
    for i in range(max(n_spont, n_probe)):
        if i < n_spont:
            k += 1
            items.append(turn(k, 100 * k, source='game', reply=VARIED[i % len(VARIED)],
                              discoveries=[dict(id=f'e{k}', importance='high', count=1, situation=f'ikan ke-{k} nyangkut di kail @fanboy99')],
                              input=f'[Game] ikan ke-{k} nyangkut', context=dict(prompt_content=PRIVATE, niatku=PRIVATE, current=dict(
                                  basis='latest_observation', current_place='Farm', current_time=dict(date='Y1 spring 5', time=900)))))
        if i < n_probe:
            k += 1
            items.append(turn(k, 100 * k, source='owner_probe', input='kenapa ke sana?', reply=f'Bohan, aku mau ke kota nomor {k}',
                              probe=dict(slot=900, date='Y1 spring 5', game_clock=900 + k),
                              now=dict(present=True, fresh=True, why='mau beli benih di Pierre' if why else None, goal_ref='g1',
                                       game_time=dict(date='Y1 spring 5', time=900 + k))))
    items.append(ev('test_end', 100 * (k + 2)))
    (tmp_path / 'bridge').mkdir(exist_ok=True)
    (tmp_path / 'bridge/bridge-transcript.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in items), encoding='utf-8')
    return items


def fake_provider(batch):
    """Labels every item: probes aligned, spontaneous answers_intent; flags the reply that mentions 'hujan'."""
    out = {}
    for item in batch['items']:
        if item['kind'] == 'probe':
            out[item['id']] = good(relevance='n/a', why_match='aligned' if item.get('why') else 'n/a', personality=[])
        else:
            out[item['id']] = good(flag='hujan' in item['reply'], mode='narrate' if 'hujan' in item['reply'] else 'react')
    return out


# ----------------------------------------------------------------------------- batch

def test_batch_is_blind_and_reads_only_game_side_fields(tmp_path):
    session(tmp_path)
    js = [journal(tmp_path, [dict(ev='progress_snapshot', at=1_700_000_000 + 50, date='Y1 spring 5', progress=dict(skills=dict(farming=0))),
                              dict(ev='quest_record', at=1_700_000_000 + 60, item_id='q1', status='active', title='Getting Started', text='...', needs=['parsnip'])])]
    batch, key = R.build_batch(Evidence(tmp_path), js)
    text = json.dumps(batch, ensure_ascii=False)
    assert PRIVATE not in text and '@fanboy99' not in text and '[viewer]' in text
    assert tmp_path.name not in text and 'turn_n' not in text and 'utc' not in text and 'codex' not in text
    assert batch['schema'] == R.BATCH_SCHEMA and len(batch['items']) == 24 and batch['rubric_sha256'] == hashlib.sha256(R.RUBRIC.encode()).hexdigest()
    ids = [i['id'] for i in batch['items']]
    assert ids != sorted(ids) and len(set(ids)) == 24 and all(len(i) == 12 for i in ids)  # shuffled, unique, salted
    probe = next(i for i in batch['items'] if i['kind'] == 'probe')
    assert probe['why'] == 'mau beli benih di Pierre' and probe['goal_ref'] == 'g1' and probe['input'] == 'kenapa ke sana?'
    spont = next(i for i in batch['items'] if i['kind'] == 'spontaneous')
    assert spont['input'].startswith('ikan ke-') and spont['shown_events'] and 'why' not in spont
    assert spont['native_facts']['date'] == 'Y1 spring 5' and spont['native_facts']['quests'][0]['title'] == 'Getting Started'
    assert key['transcript_sha256'] == hashlib.sha256((tmp_path / 'bridge/bridge-transcript.jsonl').read_bytes()).hexdigest() and key['run'] == tmp_path.name
    assert set(key['items']) == set(ids) and key['items'][probe['id']]['reply_sha256'] == R.reply_hash(next(
        r['reply'] for r in (json.loads(l) for l in (tmp_path / 'bridge/bridge-transcript.jsonl').read_text().splitlines())
        if r.get('n') == key['items'][probe['id']]['turn_n']))
    again, _ = R.build_batch(Evidence(tmp_path), js)
    assert again == batch  # deterministic


def close_session(tmp_path):
    """Plan probe (turn 1, g1), close-step probes (turns 2 g1, 3 g2, 4 no why) with goal_today on the plan turns."""
    items = [ev('test_start', 0)]
    rows = [(1, 'mau beli benih di Pierre', 'g1', 'beli benih lalu tanam parsnip', 'Aku beli benih parsnip dulu'),
            (2, 'tutup menu dulu', 'g1', 'beli benih lalu tanam parsnip', 'Lanjut beli benih terus tanam parsnip'),
            (3, 'tutup menu dulu', 'g2', None, 'Mau mancing')]
    for n, why, goal, today, reply in rows:
        items.append(turn(n, 100 * n, source='owner_probe', input='lagi apa?', reply=reply, probe=dict(slot=900),
                          now=dict(present=True, why=why, goal_ref=goal, goal_today=today, game_time=dict(date='Y1 spring 5', time=900 + n))))
    items.append(turn(4, 400, source='owner_probe', input='lagi apa?', reply='ngobrol aja', probe=dict(slot=900), now=dict(present=True, why=None)))
    (tmp_path / 'bridge').mkdir(exist_ok=True)
    (tmp_path / 'bridge/bridge-transcript.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in items), encoding='utf-8')


OLD_WHY_ROWS = [
    ('tutup menu dulu', 'menu_close'), ('Menutup halaman toko', 'menu_close'), ('close the shop window', 'menu_close'),
    ('keluar dari menu', 'menu_close'), ('press ESC', 'menu_close'), ('batal', 'menu_close'), ('cancel dulu ya', 'menu_close'),
    ('lalu tutup', 'menu_close'),                                     # <= 5 words with a close verb, no UI noun
    ('mau beli benih di Pierre', 'plan'), ('mau keluar rumah terus beli benih parsnip di Pierre', 'plan'),   # 9 words: not short
    ('keluar rumah dulu', 'plan'), ('rumah back', 'plan'),           # leaving a place is a plan step, not a menu close
    ('back to menu', 'menu_close'), ('backup farm', 'plan'), ('', None), (None, None), ('   ', None), (3, None)]

# ADR 103 additions (FIX66D): Indonesian and English close steps and plan steps that must never qualify.
# menu_close, Indonesian: affixed stems, an ambiguous verb plus a UI noun, and the close-button phrase.
NEW_MENU_CLOSE_ID = ['tutup menunya', 'nutup menu crafting dulu', 'udah ditutup halamannya', 'tutupin layar kerajinan',
                     'keluar dari menu crafting', 'keluar dulu dari halaman toko', 'menutup halaman resep', 'batalin dialognya',
                     'pencet esc buat nutup', 'tekan esc', 'klik X', 'tutup tas', 'keluar dari inventori',
                     # frozen rule: a close step followed by a plan is still menu_close (verb and UI noun anywhere in the why)
                     'tutup kotak dialog terus lanjut siram']
NEW_MENU_CLOSE_EN = ['close the crafting screen', 'closed the craft menu', 'closing the page', 'exit the crafting screen',
                     'exited the shop page', 'escaped the dialog', 'back out of the menu', 'leave the settings screen',
                     'dismiss the popup', 'cancel and close the recipe tab', 'press the X']
# plan, never qualify: ambiguous verbs alone, leaving a place, no close verb (opening is not closing), near-miss words.
NEW_PLAN = ['keluar', 'back', 'leave', 'keluar dulu', 'keluar rumah dulu', 'keluar rumah terus siram', 'back to farm',
            'leave the house', 'leaving the farm to fish', 'mau beli benih di Pierre',
            'tanam parsnip di petak kosong',      # petak is not peta
            'lanjut nyiram tanaman', 'mau mancing di sungai sampai sore',
            'buka menu crafting buat bikin peti',  # UI noun but no close verb
            'cek tas dulu',                        # UI noun but no close verb
            'kembali ke toko',                     # kembali is not a close verb
            'backup farm', 'mau keluar rumah terus beli benih parsnip di Pierre']
NEW_WHY_ROWS = ([(w, 'menu_close') for w in NEW_MENU_CLOSE_ID + NEW_MENU_CLOSE_EN] + [(w, 'plan') for w in NEW_PLAN])


@pytest.mark.parametrize('why,kind', OLD_WHY_ROWS + NEW_WHY_ROWS)
def test_why_kind_detector_table(why, kind):
    assert R.why_kind(why) == kind


def test_old_table_labels_unchanged_and_ambiguous_verbs_never_short_close():
    expected = {'tutup menu dulu': 'menu_close', 'Menutup halaman toko': 'menu_close', 'close the shop window': 'menu_close',
                'keluar dari menu': 'menu_close', 'press ESC': 'menu_close', 'batal': 'menu_close', 'cancel dulu ya': 'menu_close',
                'lalu tutup': 'menu_close', 'mau beli benih di Pierre': 'plan',
                'mau keluar rumah terus beli benih parsnip di Pierre': 'plan', 'keluar rumah dulu': 'plan', 'rumah back': 'plan',
                'back to menu': 'menu_close', 'backup farm': 'plan', '': None, None: None, '   ': None, 3: None}
    assert dict(OLD_WHY_ROWS) == expected and len(OLD_WHY_ROWS) == len(expected)
    assert all(R.why_kind(w) == k for w, k in OLD_WHY_ROWS)
    assert not {'keluar', 'back', 'leave'} & set(R.SHORT_CLOSE_VERBS)
    assert {'keluar', 'back', 'leave'} <= set(R.AMBIGUOUS_CLOSE_VERBS)


def test_long_why_with_unambiguous_verb_but_no_ui_noun_is_plan():
    long_why = 'udah selesai semua jadi tutup aja sekarang ya'      # 8 words > SHORT_WHY_WORDS, verb tutup, no UI noun
    assert len(long_why.split()) == 8 > R.SHORT_WHY_WORDS and R.SHORT_CLOSE_VERB_RE.search(long_why)
    assert not R.UI_NOUN_RE.search(long_why) and R.why_kind(long_why) == 'plan'
    assert R.why_kind('tutup aja ya') == 'menu_close'                # the same verb in <= 5 words is a close step
    assert R.why_kind('keluar dari menu ' + 'x ' * 8) == 'menu_close'  # ambiguous verb + UI noun counts at any length


def test_menu_close_probe_carries_enclosing_plan_and_plan_probe_does_not(tmp_path):
    close_session(tmp_path)
    t = 1_700_000_000
    js = [journal(tmp_path, [
        dict(ev='skill_model_decision', at=t + 150, goal_ref='g1', choice='open_shop', why='mau beli benih di Pierre @fanboy99'),
        dict(ev='skill_model_decision', at=t + 190, goal_ref='g1', choice='x', why='tutup menu dulu'),       # close step: never a plan
        dict(ev='skill_model_decision', at=t + 250, goal_ref='g1', choice='x', why='nanti tanam parsnip di kebun'),  # after the turn
        dict(ev='skill_model_decision', at=t + 150, goal_ref='g2', choice='x', why='mau mancing di sungai'),
        dict(ev='skill_model_decision', goal_ref='g1', choice='x', why='tanpa waktu')])]
    e = Evidence(tmp_path)
    batch, key = R.build_batch(e, js)
    by_n = {key['items'][i['id']]['turn_n']: i for i in batch['items']}
    assert by_n[1]['why_kind'] == 'plan' and 'enclosing_plan' not in by_n[1]
    close = by_n[2]
    assert close['why_kind'] == 'menu_close' and close['why'] == 'tutup menu dulu'
    assert close['enclosing_plan'] == dict(goal_today='beli benih lalu tanam parsnip', goal_ref='g1',
                                           previous_why='mau beli benih di Pierre', journal_why='mau beli benih di Pierre [viewer]')
    other = by_n[3]['enclosing_plan']        # different goal_ref: nothing from g1; journal g2 row is at t+150 <= t+300
    assert other == dict(goal_today=None, goal_ref='g2', previous_why=None, journal_why='mau mancing di sungai')
    assert 'why_kind' not in by_n[4] and 'enclosing_plan' not in by_n[4]
    text = json.dumps(batch, ensure_ascii=False)
    assert '@fanboy99' not in text and tmp_path.name not in text and 'turn_n' not in text and 'utc' not in text and PRIVATE not in text
    assert R.build_batch(e, js)[0] == batch                                   # deterministic


def test_enclosing_plan_is_empty_but_present_without_journals_or_earlier_plan(tmp_path):
    close_session(tmp_path)
    batch, key = R.build_batch(Evidence(tmp_path))                            # no journals: skipped gracefully
    by_n = {key['items'][i['id']]['turn_n']: i for i in batch['items']}
    assert by_n[2]['enclosing_plan'] == dict(goal_today='beli benih lalu tanam parsnip', goal_ref='g1', previous_why='mau beli benih di Pierre', journal_why=None)
    assert by_n[3]['enclosing_plan'] == dict(goal_today=None, goal_ref='g2', previous_why=None, journal_why=None)
    # journal rows with no resolvable time are never used
    undated = [journal(tmp_path, [dict(ev='skill_model_decision', goal_ref='g2', choice='x', why='mau mancing di sungai')])]
    assert R.build_batch(Evidence(tmp_path), undated)[0]['items'] == batch['items']


def test_rubric_teaches_enclosing_plan_and_lx7_shares_keep_their_meaning(tmp_path):
    assert 'enclosing_plan' in R.RUBRIC and 'menu_close' in R.RUBRIC and 'previous_why' in R.RUBRIC
    close_session(tmp_path)
    e = Evidence(tmp_path)
    batch, key = R.build_batch(e)
    labelled = {i['id']: good(relevance='n/a', personality=[], why_match='aligned' if i.get('why') else 'n/a') for i in batch['items']}
    j = R.judge_counter(e, R.make_review(key, labelled, dict(provider='fake')))
    # three probes have a why (one plan, two close steps); the close step labelled aligned counts as aligned; no-why probe is excluded
    assert (j['aligned_share']['count'], j['aligned_share']['total']) == (3, 3)
    assert (j['mismatch_share']['count'], j['mismatch_share']['total']) == (0, 3)
    close_id = next(i for i, b in key['items'].items() if b['turn_n'] == 2)
    labelled[close_id] = good(relevance='n/a', personality=[], why_match='mismatch')
    j = R.judge_counter(e, R.make_review(key, labelled, dict(provider='fake')))
    assert (j['aligned_share']['count'], j['mismatch_share']['count'], j['mismatch_share']['total']) == (2, 1, 3)


def test_batch_skips_silent_and_unavailable_turns_and_refuses_bad_identities(tmp_path):
    rows = [turn(1, 10), turn(2, 20, reply='', silent=True), turn(3, 30, reply='(gangguan)', provider_source='unavailable:deadline')]
    (tmp_path / 'bridge').mkdir()
    (tmp_path / 'bridge/bridge-transcript.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    batch, key = R.build_batch(Evidence(tmp_path))
    assert len(batch['items']) == 1 and list(key['items'].values())[0]['turn_n'] == 1 and batch['items'][0]['native_facts'] == {}
    (tmp_path / 'bridge/bridge-transcript.jsonl').write_text(json.dumps(turn(1, 10)) + '\n' + json.dumps(turn(1, 20)) + '\n', encoding='utf-8')
    with pytest.raises(ValueError):
        R.build_batch(Evidence(tmp_path))
    with pytest.raises(ValueError):
        R.build_batch(Evidence(tmp_path / 'nowhere'))


# ----------------------------------------------------------------------------- labels, review, counter

def test_label_validation_rejects_unknown_values_and_make_review_lists_them(tmp_path):
    session(tmp_path, 2, 1)
    batch, key = R.build_batch(Evidence(tmp_path))
    ids = [i['id'] for i in batch['items']]
    labelled = {ids[0]: good(), ids[1]: good(mode='describe'), ids[2]: good(personality=['feeling', 'feeling']), 'deadbeef0000': good()}
    review = R.make_review(key, labelled, dict(provider='fake'))
    assert review['schema'] == R.REVIEW_SCHEMA and review['run'] == tmp_path.name and review['transcript_sha256'] == key['transcript_sha256']
    assert len(review['items']) == 1 and review['items'][0]['labels']['note'] == ''
    assert sorted(r['id'] for r in review['rejected']) == sorted([ids[1], ids[2], 'deadbeef0000'])
    assert any('unknown id' in r['errors'] for r in review['rejected']) and review['unlabelled'] == []
    assert R.label_errors(None) == ['labels not an object'] and R.label_errors(good(flag='yes')) == ['flag not a bool']
    assert R.make_review(key, [dict(id=ids[0], labels=good())], None)['judge']['provider'] == 'unknown'


def test_counter_shares_need_every_applicable_reply_but_counts_stand_alone(tmp_path):
    session(tmp_path)
    e = Evidence(tmp_path)
    review = run_judge(tmp_path, fake_provider, judge=dict(provider='fake'))
    j = R.judge_counter(e, review)
    assert j['bound'] and j['complete'] and j['reviewed_turns'] == j['reply_turns'] == 24
    assert j['narrate_share']['value'] == round(1 / 24, 3) and j['role_confusion']['value'] == 0
    assert j['answers_intent_share'] == dict(value=1.0, count=12, total=12, evidence=j['answers_intent_share']['evidence'], missing_signal=None)
    assert j['aligned_share']['value'] == 1.0 and j['mismatch_share']['value'] == 0.0 and j['personality_share']['value'] == 1.0
    assert j['sensitive_breaches']['value'] is None and j['sensitive_breaches']['missing_signal'] == 'sensitive_probes_not_exercised'
    assert len(j['flagged']) == 1 and j['narrate_share']['evidence'] == [f"bridge/bridge-transcript.jsonl:J{j['flagged'][0] + 1}"]
    # Drop one spontaneous item: shares over spontaneous replies become unknown, probe shares stay, a positive count stands.
    spont_n = next(i['turn_n'] for i in review['items'] if i['labels']['relevance'] == 'answers_intent')
    partial = dict(review, items=[i for i in review['items'] if i['turn_n'] != spont_n])
    partial['items'][0]['labels'].update(role_confusion=True, sensitive_topic='politics', sensitive_breach=True)
    p = R.judge_counter(e, partial)
    assert not p['complete'] and p['answers_intent_share']['value'] is None and 'not every applicable' in p['answers_intent_share']['missing_signal']
    assert p['narrate_share']['value'] is None and p['aligned_share']['value'] == 1.0
    assert p['role_confusion']['value'] == 1 and p['sensitive_breaches']['value'] == 1
    # Fewer than 10 applicable replies: unknown by sample size.
    small = session(tmp_path, 3, 3)
    sr = run_judge(tmp_path, fake_provider, force=True)
    s = R.judge_counter(Evidence(tmp_path), sr)
    assert s['complete'] and s['narrate_share']['value'] is None and 'fewer than 10' in s['narrate_share']['missing_signal']


def test_review_must_bind_to_this_run_and_transcript(tmp_path):
    session(tmp_path)
    e = Evidence(tmp_path)
    review = run_judge(tmp_path, fake_provider)
    assert R.judge_counter(e, None)['missing_signal'] == 'judge_not_run'
    for bad in (dict(review, run='other'), dict(review, transcript_sha256='0' * 64), dict(review, schema='x'), dict(review, items=None)):
        assert R.judge_counter(e, bad)['bound'] is False
    # a stale reply hash or a duplicated turn is dropped, never counted
    tampered = json.loads(json.dumps(review))
    tampered['items'][0]['reply_sha256'] = 'f' * 64
    tampered['items'].append(dict(tampered['items'][1]))
    j = R.judge_counter(e, tampered)
    assert j['bound'] and j['reviewed_turns'] == 22 and not j['complete']
    # transcript edited after labelling: nothing binds
    (tmp_path / 'bridge/bridge-transcript.jsonl').write_text((tmp_path / 'bridge/bridge-transcript.jsonl').read_text() + json.dumps(turn(99, 99999)) + '\n')
    assert R.judge_counter(Evidence(tmp_path), review)['bound'] is False


# ----------------------------------------------------------------------------- owner export and calibration

def export(tmp_path, per, ratings, extra=()):
    samples = per['samples']
    rows = [dict(turn_n=s['turn_n'], reply_sha256=s['reply_sha256'], rating=r, comment='', set='systematic') for s, r in zip(samples, ratings)]
    rows += [dict(turn_n=n, reply_sha256='ab' * 32, rating=r, comment='x', set='judge_flagged') for n, r in extra]
    sha = Evidence(tmp_path).files.get('bridge/bridge-transcript.jsonl') or R.transcript_sha(Evidence(tmp_path))
    data = dict(schema=R.RATINGS_SCHEMA, run=tmp_path.name, transcript_sha256=sha, exported_utc='2026-11-04T00:00:00Z', ratings=rows)
    put(tmp_path / 'bridge/persona-ratings.json', data)
    return data


def test_owner_ratings_bind_count_and_keep_legacy_lists_readable(tmp_path):
    session(tmp_path)
    e, per = Evidence(tmp_path), persona(tmp_path)
    assert len(per['samples']) == 10
    data = export(tmp_path, per, ['up'] * 7 + ['edit', 'edit', 'down'], extra=[(3, 'down'), (5, 'up')])
    o = R.owner_ratings(e, data, per)
    assert o['bound'] and o['binding'] == 'hash_bound' and (o['up'], o['edit'], o['down']) == (7, 2, 1) and o['rated_systematic'] == 10
    assert o['value'] == dict(up=7, down=1) and (o['flagged_rated'], o['flagged_down'], o['comments']) == (2, 1, 2)
    assert o['by_turn'][3] == 'down' and len(o['by_turn']) == 12
    assert persona(tmp_path)['rated'] == 10 and persona(tmp_path)['edit'] == 2   # journey reads the object export too
    partial = R.owner_ratings(e, dict(data, ratings=data['ratings'][:4]), per)
    assert partial['value'] is None and partial['missing_signal'] == 'owner_rating_missing (4/10 rated)'
    assert R.owner_ratings(e, dict(data, transcript_sha256='0' * 64), per)['missing_signal'] == 'owner ratings not bound to this transcript'
    legacy = R.owner_ratings(e, [dict(reply_sha256=per['samples'][0]['reply_sha256'], rating='up')], per)
    assert legacy['bound'] and legacy['binding'] == 'legacy_unbound' and legacy['up'] == 1 and legacy['by_turn'] == {per['samples'][0]['turn_n']: 'up'}
    assert R.owner_ratings(e, None, per)['missing_signal'] == 'owner_rating_missing'
    assert R.owner_ratings(e, data, dict(samples=per['samples'][:5]))['missing_signal'] == 'fewer than 10 systematic samples in this session'


def test_agreement_compares_up_down_only_and_counts_from_80_percent():
    judge = {n: good() for n in range(1, 13)}
    judge[12] = good(flag=True)
    judge[11] = good(mode='narrate')
    owner = {n: 'up' for n in range(1, 11)}
    owner.update({11: 'down', 12: 'edit', 13: 'down'})          # 13 has no judge label; 12 is an edit
    a = R.agreement(judge, owner)
    assert (a['compared'], a['agree'], a['rate'], a['counted']) == (11, 11, 1.0, True)
    owner.update({1: 'down', 2: 'down', 3: 'down'})
    a = R.agreement(judge, owner)
    assert (a['compared'], a['agree'], a['rate'], a['counted']) == (11, 8, 0.727, False)
    assert R.agreement(judge, {n: 'up' for n in range(1, 10)})['counted'] is False   # 9 pairs at 100% is not enough
    assert R.agreement({}, {})['rate'] is None
    assert R.judge_good(good(why_match='mismatch')) is False and R.judge_good(good(sensitive_breach=True)) is False


# ----------------------------------------------------------------------------- CLI

def test_cli_prepare_bind_and_provider_paths(tmp_path, capsys):
    session(tmp_path, 4, 4)
    assert judge_main([str(tmp_path)]) == 0
    assert 'never send the key' in capsys.readouterr().out
    batch = json.loads((tmp_path / 'bridge/experience-batch.json').read_text(encoding='utf-8'))
    key = json.loads((tmp_path / 'bridge/experience-batch.key.json').read_text(encoding='utf-8'))
    assert tmp_path.name not in json.dumps(batch) and key['run'] == tmp_path.name
    with pytest.raises(SystemExit):
        judge_main([str(tmp_path)])   # refuses to overwrite without --force
    labels = tmp_path / 'labels.json'
    labels.write_text(json.dumps({i['id']: good(relevance='n/a' if i['kind'] == 'probe' else 'answers_intent') for i in batch['items'][:5]}), encoding='utf-8')
    assert judge_main([str(tmp_path), '--labels', str(labels), '--judge-name', 'codex-offline', '--model', 'm1']) == 0
    review = json.loads((tmp_path / 'bridge/experience-review.json').read_text(encoding='utf-8'))
    assert review['judge'] == dict(provider='codex-offline', model='m1', rubric_sha256=batch['rubric_sha256'])
    assert len(review['items']) == 5 and len(review['unlabelled']) == 3 and '5 labelled, 0 rejected, 3 unlabelled' in capsys.readouterr().out
    assert judge_main([str(tmp_path), '--provider', 'tests.test_free_play_experience_judge:fake_provider', '--force']) == 0
    review = json.loads((tmp_path / 'bridge/experience-review.json').read_text(encoding='utf-8'))
    assert len(review['items']) == 8 and review['judge']['provider'] == 'fake_provider'
    with pytest.raises(SystemExit):
        judge_main([str(tmp_path), '--provider', 'json:loads', '--labels', str(labels)])
    with pytest.raises(SystemExit):
        judge_main([str(tmp_path), '--provider', 'nonsense'])
    out = tmp_path / 'elsewhere/review.json'
    bind_labels(tmp_path, labels, out=out)
    assert out.is_file()
    prepare(tmp_path, batch_out=tmp_path / 'b.json', key_out=tmp_path / 'k.json')


# ----------------------------------------------------------------------------- evaluator integration

def test_evaluate_turns_judge_and_owner_criteria_into_values_only_when_calibrated(tmp_path):
    run = fixture_run(tmp_path)
    session(run)
    base = evaluate(run)
    assert base['evaluator_version'] == EVALUATOR_VERSION == '1.7.16'
    g = base['experience_gate']
    assert g['judge']['missing_signal'] == 'judge_not_run' and g['owner']['missing_signal'] == 'owner_rating_missing' and 'by_turn' not in g['owner']
    assert g['criteria']['LX-3']['missing_signal'] == 'judge_not_run' and g['criteria']['LX-14']['status'] == 'UNKNOWN'
    assert g['judge_calibration']['counted'] is False
    # judge review alone: values computed, status stays UNKNOWN (uncalibrated)
    run_judge(run, fake_provider, judge=dict(provider='fake'))
    g = evaluate(run)['experience_gate']
    lx3 = g['criteria']['LX-3']
    assert lx3['status'] == 'UNKNOWN' and lx3['uncalibrated_status'] == 'MET' and lx3['value'] == round(1 / 24, 3)
    assert lx3['missing_signal'].startswith('judge_uncalibrated') and g['criteria']['LX-13']['missing_signal'] == 'sensitive_probes_not_exercised'
    assert g['criteria']['LX-2']['uncalibrated_status'] == 'MET' and g['criteria']['LX-7']['value'] == dict(aligned_share=1.0, mismatch_share=0.0)
    assert g['criteria']['LX-7']['evidence'][0] == 'bridge/experience-review.json'
    # owner export that agrees with the judge on 10 systematic samples: judge counts, LX-14 MET
    per = persona(run)
    flagged = g['judge']['flagged']
    ratings = ['down' if s['turn_n'] in flagged else 'up' for s in per['samples']]
    export(run, per, ratings)
    result = evaluate(run)
    g = result['experience_gate']
    assert g['judge_calibration']['counted'] is True and g['judge_calibration']['compared'] == 10
    assert g['criteria']['LX-3']['status'] == 'MET' and g['criteria']['LX-2']['status'] == 'MET' and g['criteria']['LX-4']['status'] == 'MET'
    assert g['criteria']['LX-7']['status'] == 'MET' and g['criteria']['LX-8']['status'] == 'MET' and g['criteria']['LX-13']['status'] == 'UNKNOWN'
    assert g['criteria']['LX-14']['status'] == 'MET' and g['criteria']['LX-14']['value'] == dict(up=ratings.count('up'), down=ratings.count('down'))
    assert g['counts']['UNKNOWN'] >= 2 and g['automatic_status'] == 'NOT_MET'   # LX-13 not exercised, LX-6 needs v2; one mood fails LX-9 in this fixture
    # the owner disagrees: judge criteria drop back to UNKNOWN, LX-14 fails on its own
    export(run, per, ['down'] * 10)
    g = evaluate(run)['experience_gate']
    assert g['judge_calibration']['counted'] is False and g['criteria']['LX-3']['status'] == 'UNKNOWN' and g['criteria']['LX-3']['uncalibrated_status'] == 'MET'
    assert g['criteria']['LX-14']['status'] == 'NOT_MET' and g['status'] == 'NOT_MET'
    # goal_gate is untouched by all of this
    evidence = Evidence(run)
    journals = [(p, evidence.rows(p)) for p in sorted(run.glob('runtime/live7/*/day-*/journal/g1_journal.jsonl'))]
    expected = goal_gate(result['endurance'], native_progress(evidence, journals, ''), semantic_counter(evidence, journals))
    assert json.dumps(result['goal_gate'], sort_keys=True) == json.dumps(expected, sort_keys=True)
    write_evaluation(run, result)
    text = (run / 'EVALUATION.md').read_text(encoding='utf-8')
    assert 'Judge review: bound; labelled 24/24 replies; flagged 1.' in text and 'judge counts: True' in text
    assert len([x for x in markdown(result).splitlines() if x.startswith('## ')]) == 13
    assert PRIVATE not in json.dumps(result)


def test_evaluate_ignores_ambiguous_or_unreadable_review_files(tmp_path):
    run = fixture_run(tmp_path)
    session(run)
    run_judge(run, fake_provider)
    (run / 'experience-review-copy.json').write_text('{}', encoding='utf-8')
    assert evaluate(run)['experience_gate']['judge']['bound'] is False   # two files: none chosen
    (run / 'experience-review-copy.json').unlink()
    (run / 'bridge/experience-review.json').write_text('not json', encoding='utf-8')
    g = evaluate(run)['experience_gate']
    assert g['judge']['bound'] is False and g['judge']['missing_signal'] == 'judge_not_run'
    (run / 'bridge/persona-ratings.json').write_text('[not json', encoding='utf-8')
    assert evaluate(run)['experience_gate']['owner']['bound'] is False
