"""Blind owner rating page (ADR 92). Synthetic transcripts only; no real session data."""
import hashlib
import json
import re

import pytest

from scripts.free_play import journey as J
from scripts.free_play import rating_page as R
from scripts.free_play import storage


def turn(n, source='owner_probe', reply=None, **extra):
    row = dict(ev='turn', n=n, source=source, trigger_type=source, input='lagi mau ngapain?',
               reply=reply if reply is not None else f'jawaban nomor {n}', silent=False, provider_source='codex')
    row.update(extra)
    return row


def write_run(tmp_path, rows, name='run-secret-7', raw=None):
    run = tmp_path / name
    path = run / 'bridge/bridge-transcript.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(raw if raw is not None else ''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return run


def sha(run):
    return storage.digest_file(run / 'bridge/bridge-transcript.jsonl')


def write_review(rundir, flags, where='bridge', **over):
    doc = dict(schema='arti.free_play.experience_review/1', run=rundir.name, transcript_sha256=sha(rundir),
               items=[dict(turn_n=n, reply_sha256='0' * 64, labels=dict(flag=True, note='judge secret note')) for n in flags])
    doc.update(over)
    path = rundir / where / 'experience-review.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding='utf-8')


def many(count=25):
    return [turn(i) for i in range(1, count + 1)]


def outside_meta(page):
    return re.sub(r'<script type="application/json" id="meta">.*?</script>', '', page, flags=re.S)


def embedded(page, tag):
    return json.loads(re.search(rf'<script type="application/json" id="{tag}">(.*?)</script>', page, re.S).group(1))


def test_ten_systematic_plus_flagged_capped_and_deduped(tmp_path):
    run = write_run(tmp_path, many(30))
    systematic = {s['turn_n'] for s in J.persona(run)['samples']}
    extras = [n for n in range(1, 31) if n not in systematic]
    write_review(run, sorted(systematic)[:2] + extras[:7] + [999])
    model = R.build_samples(run)
    assert len(model['samples']) == 10 and {s['turn_n'] for s in model['samples']} == systematic
    assert [s['turn_n'] for s in model['flagged']] == extras[:5]
    assert {s['set'] for s in model['flagged']} == {'judge_flagged'} and {s['set'] for s in model['samples']} == {'systematic'}
    page = R.render(model)
    assert page.count('class="card"') == 15 and page.count('judge-flagged') == 5
    assert 'judge secret note' not in page


def test_mismatched_or_ambiguous_review_ignored(tmp_path, capsys):
    run = write_run(tmp_path, many(30))
    extras = [n for n in range(1, 31) if n not in {s['turn_n'] for s in J.persona(run)['samples']}][:2]
    write_review(run, extras, transcript_sha256='f' * 64)
    model = R.build_samples(run)
    assert model['flagged'] == [] and any('ignored' in w for w in model['warnings'])
    write_review(run, extras, run='other-run')
    assert R.build_samples(run)['flagged'] == []
    write_review(run, extras)
    write_review(run, extras[:1], where='.')
    assert R.build_samples(run)['flagged'] == []
    out = tmp_path / 'p.html'
    assert R.main([str(run), '--out', str(out)]) == 0 and 'warning' in capsys.readouterr().err


def test_blind_page_hides_run_sha_and_prompt_fields(tmp_path):
    rows = [turn(i, context=dict(prompt_content='PROMPTBODY', niatku='NIATKU-TEXT', stardew_role='ROLE-TEXT')) for i in range(1, 12)]
    run = write_run(tmp_path, rows)
    page = R.render(R.build_samples(run))
    body = outside_meta(page)
    assert run.name not in body and sha(run) not in body
    assert run.name in page and sha(run) in page and embedded(page, 'meta') == dict(run=run.name, transcript_sha256=sha(run))
    for secret in ('PROMPTBODY', 'NIATKU-TEXT', 'ROLE-TEXT', 'prompt_content', 'niatku'):
        assert secret not in page
    assert 'http://' not in page and 'https://' not in page and '<link' not in page


def test_viewer_names_scrubbed_everywhere(tmp_path):
    rows = [turn(1, input='kata Budi Viewer: halo @fanboy99', reply='halo Budi Viewer dan @fanboy99'),
            dict(ev='turn', n=2, source='chat', author='Budi Viewer', input='x', reply='y'),
            turn(3, source='game', input='[Game] Budi Viewer kasih hadiah', reply='makasih Budi Viewer',
                 discoveries=[dict(situation='Budi Viewer datang')])]
    run = write_run(tmp_path, rows)
    write_review(run, [3])
    page = R.render(R.build_samples(run))
    assert 'Budi Viewer' not in page and '@fanboy99' not in page and '[viewer]' in page


def test_context_line_per_source(tmp_path):
    names = set()
    assert R.context_line(turn(1), names) == 'Owner asked: lagi mau ngapain?'
    d = turn(2, source='discovery', input='[Game] a | b', discoveries=[dict(situation='Rain began'), dict(situation='Crops watered'), dict(id='x')])
    assert R.context_line(d, names) == 'In the game: Rain began; Crops watered'
    assert R.context_line(turn(3, source='game', input='[Game] Pierre waved'), names) == 'In the game: Pierre waved'
    assert R.context_line(turn(4, source='status', input='energy low'), names) == 'Status: energy low'
    assert R.context_line(turn(5, source='moment', input='hm'), names) == 'Status: hm'
    assert R.context_line(turn(6, input=''), names) == 'No context recorded'
    assert R.context_line(turn(7, input=None), names) == 'No context recorded'
    assert len(R.context_line(turn(8, input='x' * 500), names)) == 240


def test_game_date_and_clock_shown(tmp_path):
    rows = [turn(i, probe=dict(date='Y1 spring 5', game_clock=900)) for i in range(1, 4)]
    page = R.render(R.build_samples(write_run(tmp_path, rows)))
    assert 'Y1 spring 5 9:00' in page


@pytest.mark.parametrize('raw', [None, ''])
def test_missing_or_empty_transcript(tmp_path, raw, capsys):
    run = tmp_path / 'r'
    if raw is not None:
        write_run(tmp_path, [], name='r', raw=raw)
    out = tmp_path / 'o.html'
    assert R.main([str(run), '--out', str(out)]) == 0
    assert 'No samples in this session' in out.read_text(encoding='utf-8') and 'warning' in capsys.readouterr().err
    assert 'class="card"' not in out.read_text(encoding='utf-8')


def test_malformed_lines_skipped(tmp_path):
    good = ''.join(json.dumps(turn(i)) + '\n' for i in range(1, 4))
    run = write_run(tmp_path, [], raw='{broken\n' + good + 'null\n[1,2]\n\n')
    assert [s['turn_n'] for s in R.build_samples(run)['samples']] == [1, 2, 3]


def test_embedded_samples_and_hashes_match_journey(tmp_path):
    run = write_run(tmp_path, many(25))
    write_review(run, [2])
    model = R.build_samples(run)
    page = R.render(model)
    slim = embedded(page, 'samples')
    assert len(slim) == len(model['samples']) + len(model['flagged'])
    assert all(set(s) == {'turn_n', 'reply_sha256', 'set'} for s in slim)
    assert [(s['turn_n'], s['reply_sha256']) for s in model['samples']] == [(s['turn_n'], s['reply_sha256']) for s in J.persona(run)['samples']]
    flagged = model['flagged'][0]
    assert flagged['reply_sha256'] == hashlib.sha256(flagged['reply'].encode()).hexdigest()
    assert 'persona-ratings:' in page and 'Export JSON' in page and 'new Date().toISOString()' in page
    assert 'arti.free_play.persona_ratings/1' in page and 'exported_utc' not in embedded(page, 'meta')


def test_deterministic_and_refuse_overwrite(tmp_path, capsys):
    run = write_run(tmp_path, many(15))
    assert R.render(R.build_samples(run)) == R.render(R.build_samples(run))
    out = tmp_path / 'o.html'
    assert R.main([str(run), '--out', str(out)]) == 0
    first = out.read_text(encoding='utf-8')
    out.write_text('keep', encoding='utf-8')
    assert R.main([str(run), '--out', str(out)]) == 2 and out.read_text(encoding='utf-8') == 'keep'
    assert R.main([str(run), '--out', str(out), '--force']) == 0 and out.read_text(encoding='utf-8') == first
