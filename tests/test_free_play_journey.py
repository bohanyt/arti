"""Evaluator 1.2.0 journey score: game-state yardsticks, census, persona scrub. Synthetic data only."""
import hashlib
import json
from pathlib import Path

from scripts.free_play import journey as J
from scripts.free_play.evaluate import evaluate, markdown, rank, write_evaluation
from scripts.free_play.scoreboard import write_persona_samples, write_scoreboard

XSI = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding='utf-8')


def journal(run, rows, day=1):
    path = run / f'runtime/live7/c1/day-{day:04}-attempt-000/journal/g1_journal.jsonl'
    put(path, ''.join(json.dumps(r) + '\n' for r in rows))
    return path, list(enumerate(rows, 1))


def farmer(name, *, earned=0, farming=0, tools=(('Axe', 0),), friends=(), values=None, visited=(), mail=(),
           achievements=(), events=(), house=0, quests=0):
    items = ''.join(f'<Item xsi:type="{c}"><upgradeLevel>{lvl}</upgradeLevel></Item>' for c, lvl in tools)
    fr = ''.join(f'<item><key><string>{n}</string></key><value><Friendship><Points>{p}</Points><Status>{s}</Status>'
                 f'</Friendship></value></item>' for n, p, s in friends)
    vals = '' if values is None else '<stats><Values>' + ''.join(
        f'<item><key><string>{k}</string></key><value><unsignedInt>{v}</unsignedInt></value></item>'
        for k, v in values.items()) + '</Values></stats>'
    strings = lambda xs: ''.join(f'<string>{x}</string>' for x in xs)  # noqa: E731
    return (f'<Farmer><name>{name}</name><questLog>' + '<Quest><completed>false</completed></Quest>' * quests +
            f'</questLog><items>{items}</items><achievements>' + ''.join(f'<int>{a}</int>' for a in achievements) +
            f'</achievements><mailReceived>{strings(mail)}</mailReceived><mailbox><string>x</string></mailbox>'
            f'<locationsVisited>{strings(visited)}</locationsVisited><eventsSeen>{strings(events)}</eventsSeen>{vals}'
            f'<farmingLevel>{farming}</farmingLevel><fishingLevel>0</fishingLevel><foragingLevel>1</foragingLevel>'
            f'<miningLevel>0</miningLevel><combatLevel>0</combatLevel><luckLevel>0</luckLevel>'
            f'<houseUpgradeLevel>{house}</houseUpgradeLevel><friendshipData>{fr}</friendshipData>'
            f'<totalMoneyEarned>{earned}</totalMoneyEarned><money>5</money></Farmer>')


def save(host_mail=(), done_slots=(), rooms=('false',) * 6, **arti):
    bundles = ('<item><key><int>0</int></key><value><ArrayOfBoolean>' +
               ''.join(f'<boolean>{"true" if i in done_slots else "false"}</boolean>' for i in range(12)) +
               '</ArrayOfBoolean></value></item><item><key><int>1</int></key><value><ArrayOfBoolean>'
               '<boolean>false</boolean></ArrayOfBoolean></value></item>')
    data = ('<item><key><string>Pantry/0</string></key><value><string>Sample Bundle A/O 100 1/101 1 0 102 1 0 103 1 0 104 1 0/0/2//Sample Bundle A</string></value></item>'
            '<item><key><string>Vault/1</string></key><value><string>999g/O 100 1/-1 999 999 0/4///999g</string></value></item>')
    return (f'<SaveGame {XSI}><player><name>host</name><mailReceived>' + ''.join(f'<string>{m}</string>' for m in host_mail) +
            '</mailReceived></player><farmhands>' + farmer('Arti', **arti) + farmer('Other', earned=999999) +
            '</farmhands><locations><GameLocation xsi:type="CommunityCenter"><areasComplete>' +
            ''.join(f'<boolean>{r}</boolean>' for r in rooms) + f'</areasComplete><bundles>{bundles}</bundles>'
            f'</GameLocation></locations><bundleData>{data}</bundleData></SaveGame>')


def staged_run(tmp_path, start_xml, end_xml, *, tamper=False):
    template = tmp_path / 'templates/W_1'
    put(template / 'W_1', start_xml)
    digest = hashlib.sha256((template / 'W_1').read_bytes()).hexdigest()
    run = tmp_path / 'run'
    put(run / 'STAGING.json', dict(template=str(template), save_folder='W_1', farmhand_name='Arti',
                                   template_hashes={'W_1': '0' * 64 if tamper else digest}))
    put(run / 'preserved-saves/W_1/W_1', end_xml)
    return run


# --------------------------------------------------------------------------- game rules

def test_grandpa_matches_the_installed_game_rule():
    base = dict(money_earned=0, achievements=set(), host_mail=set(), self_mail=set(), events_seen=set(), married=False,
                house_upgrade=0, friend_points={}, skills={s: 0 for s in J.SKILLS})
    assert J.grandpa(base)['points'] == 0 and J.grandpa(base)['candles'] == 1
    rich = dict(base, money_earned=1_000_000)
    assert J.grandpa(rich)['points'] == 7  # 5 thresholds + 2 for one million
    full = dict(base, money_earned=1_000_000, achievements={5, 26, 34}, host_mail={'HasSkullKey', 'HasRustyKey'},
                self_mail=set(J.CC_ROOM_MAIL) | {'petLoveMessage'}, events_seen={'191393'}, married=True, house_upgrade=2,
                friend_points={str(i): 2000 for i in range(10)}, skills={s: 10 for s in J.SKILLS[:5]} | {'luck': 0})
    assert J.grandpa(full)['points'] == J.GRANDPA_MAX == 21 and J.grandpa(full)['candles'] == 4
    # CC complete without the ceremony: +1 only; the ceremony alone also grants the +1 (accessible OR complete).
    assert J.grandpa(dict(base, self_mail=set(J.CC_ROOM_MAIL)))['points'] == 1
    assert J.grandpa(dict(base, events_seen={'191393'}))['points'] == 3
    # 1975 points is the 8-heart floor; skill Level is the half-sum floor.
    assert J.grandpa(dict(base, friend_points={str(i): 1975 for i in range(5)}))['points'] == 1
    assert J.grandpa(dict(base, skills={s: 5 for s in J.SKILLS}))['points'] == 1  # 30 // 2 = 15
    assert [J.candles(p) for p in (0, 3, 4, 7, 8, 11, 12)] == [1, 1, 2, 2, 3, 3, 4]
    unknown = J.grandpa(dict(base, achievements=None, skills=None))
    assert {'master_angler', 'skill_level_15'} <= set(unknown['unknown'])


def test_bundle_done_uses_the_required_count_not_every_slot():
    status = J.bundle_status({'Pantry/0': 'A/x/101 1 0 102 1 0 103 1 0/0/2', 'Vault/1': 'B/x/-1 999 999 0/4'},
                             {0: [True, True, False], 1: [False]})
    assert status == [('Pantry', 0, True), ('Vault', 1, False)]
    assert J.bundle_status({'Pantry/2': 'C/x/101 1 0'}, {}) == [('Pantry', 2, None)]  # completion unread = unknown


# --------------------------------------------------------------------------- save diff

def test_save_diff_reads_the_named_farmhand_and_marks_sources(tmp_path):
    start = save(earned=30, values=dict(individualMoneyEarned=30), visited=('Farm',), friends=(('Pierre', 240, 'Friendly'),))
    end = save(earned=930, farming=1, tools=(('Axe', 1), ('Hoe', 0)), visited=('Farm', 'Town', 'Beach'),
               values=dict(individualMoneyEarned=830, questsCompleted=1, cropsShipped=4),
               friends=(('Pierre', 520, 'Friendly'), ('Leah', 260, 'Friendly')), done_slots=(0, 1))
    run = staged_run(tmp_path, start, end)
    journal(run, [dict(ev='part_live_row', skill='panen', verified=True,
                       evidence=dict(observed=dict(effects=[dict(kind='harvest_crop', delta=3)])))])
    j = J.journey(run, [(None, list(enumerate([dict(ev='part_live_row', skill='panen', verified=True,
                       evidence=dict(observed=dict(effects=[dict(kind='harvest_crop', delta=3)])))], 1)))])
    p = j['progress']
    assert p['money_earned'] == dict(value=900, source='save_diff')
    assert p['money_earned_individual']['value'] == 800
    assert p['skill_levels']['value']['farming'] == 1 and p['tool_upgrades']['value'] == 1
    assert p['new_places']['value'] == 2 and p['quests_completed']['value'] == 1  # absent Stats key = 0 (Stats.Get)
    assert p['crops_shipped']['value'] == 4
    assert p['friendship']['value'] == dict(points=540, hearts=3, npcs_new=1)
    assert p['bundles_completed'] == dict(value=1, source='save_diff')
    assert p['crops_harvested'] == dict(value=3, source='journal')  # no harvest counter in the save
    assert 'last SAVED night' in p['basis']
    f = j['finishing']
    assert (f['bundles_done'], f['bundles_total'], f['rooms_total'], f['grandpa_points']) == (1, 2, 6, 0)
    assert f['distance'] == 1 + 21
    assert j['saves']['start']['verified'] is True


def test_tampered_template_makes_deltas_unknown_never_zero(tmp_path):
    run = staged_run(tmp_path, save(earned=30), save(earned=900), tamper=True)
    j = J.journey(run, [])
    assert j['saves']['start']['verified'] is False
    assert j['progress']['money_earned'] == dict(value=None, source='unknown')
    assert j['progress']['crops_harvested'] == dict(value=None, source='unknown')
    assert j['finishing']['source'] == 'save_diff'  # the end yardstick is still read
    assert j['partial'] is True and 'money_per_500' in j['partial_missing']


def test_snapshot_rows_win_over_the_save_diff(tmp_path):
    run = staged_run(tmp_path, save(earned=30), save(earned=900))
    snap = lambda earned, harvested, bundles: dict(ev='progress_snapshot', game='stardew', day=1, phase='morning',  # noqa: E731
        progress=dict(money_earned=earned, crops=dict(harvested=harvested), skills={s: 0 for s in J.SKILLS},
                      cc=dict(bundles_done=bundles, bundles_total=30, rooms_done=0, rooms_total=6),
                      grandpa=dict(points=2, max=21, candles=1, criteria=[])))
    rows = list(enumerate([snap(30, 0, 0), snap(530, 7, 1)], 1))
    j = J.journey(run, [(None, rows)])
    assert j['snapshots'] == 2
    assert j['progress']['money_earned'] == dict(value=500, source='snapshot')
    assert j['progress']['crops_harvested'] == dict(value=7, source='snapshot')
    assert j['finishing']['source'] == 'snapshot' and j['finishing']['grandpa_points'] == 2
    assert j['finishing']['distance'] == 29 + 19


# --------------------------------------------------------------------------- understanding

def _decision(*choice):
    return dict(ev='skill_model_decision', choice=list(choice))


def test_census_definitions_and_repeat_failures():
    rows = [_decision('cek_pagi', 'tv'), _decision('pergi_ke', 'home'), _decision('pergi_ke', 'farm'),
            _decision('tanam', 'Parsnip Seeds'), _decision('siram', None), dict(ev='skill_model_decision', choice='invalid'),
            _decision('ui_action', 'press:1:A'),
            dict(ev='live7_ui_choice', result_v1=dict(changed='closed')), dict(ev='live7_ui_choice', result_v1=dict(changed='replaced')),
            dict(ev='part_live_row', skill='jual', arg='Stone', outcome_class='CLEAN_FAIL', verified=False, reason='shop_closed_by_choice'),
            dict(ev='part_live_row', skill='pergi_ke', arg='@Tunnel', outcome_class='CLEAN_FAIL', verified=False, reason='exit_not_offered'),
            dict(ev='part_live_row', skill='pergi_ke', arg='@Tunnel', outcome_class='CLEAN_FAIL', verified=False, reason='exit_not_offered'),
            dict(ev='part_live_row', skill='pergi_ke', arg='home', outcome_class='CLEAN_FAIL', verified=False, reason='route_paused_new_map'),
            dict(ev='part_live_row', skill='pergi_ke', arg='seed shop', outcome_class='INTERRUPTED', verified=False, reason='route_paused_people_here'),
            dict(ev='part_live_row', skill='cek_pagi', arg='tv', outcome_class='UNCERTAIN', verified=False, reason='UNCERTAIN(MENU_7)'),
            dict(ev='mind_reflection', new_goals=['t2-1', 't2-2'], kept=[], done=[]),
            dict(ev='mind_reflection', new_goals=['t3-1'], kept=['t2-2'],
                 done=[dict(id='t2-1', ref='r3', evidence='uang total 230g -> 412g'), dict(id='t2-2', ref='r4', evidence='')])]
    u = J.understanding([(None, list(enumerate(rows, 1)))])
    assert u['decisions'] == 6 and u['loop_parts'] == dict(cek_pagi=1, home=1, ui_closed=1)
    assert u['loop_proxy'] == 3 and u['loop_proxy_pct'] == 50.0
    assert u['progress_choices'] == 1 and u['care_choices'] == 1
    assert u['failures'] == 3 and u['repeated_failures'] == 1 and u['repeat_failure_pct'] == 33.3  # by_choice excluded
    assert u['top_repeated_failures'] == [['pergi_ke', '@Tunnel', 'exit_not_offered', 2]]
    assert (u['goals_set'], u['goals_done'], u['goals_done_with_evidence']) == (3, 2, 1)  # empty evidence is no proof
    legacy = J.understanding([(None, [(1, dict(ev='mind_reflection', new_goals=['a'], done=[dict(id='a', ref='r1')]))])])
    assert legacy['goals_done_with_evidence'] == 1  # historical rows carry the verified ref only


def test_census_reproduces_the_audit_shape_on_reasons_with_numbers():
    assert J._reason_class('UNCERTAIN(MENU_7)') == J._reason_class('UNCERTAIN(MENU_12)')


# --------------------------------------------------------------------------- persona

def test_persona_samples_scrub_viewers_and_spread(tmp_path):
    rows = [dict(ev='test_start')]
    rows += [dict(ev='turn', n=i, source='owner_probe', input='lagi mau ngapain?', reply=f'jawaban {i} buat @fanboy99',
                  probe=dict(date='Y1 spring 5', game_clock=900 + i)) for i in range(1, 25)]
    rows += [dict(ev='turn', n=99, source='chat', author='Budi Viewer', input='halo', reply='halo Budi Viewer'),
             dict(ev='turn', n=100, source='game', input='[Stardew] Budi Viewer gift', reply='makasih Budi Viewer!'),
             dict(ev='turn', n=101, source='owner_probe', input='x', reply='', silent=False)]
    put(tmp_path / 'bridge/bridge-transcript.jsonl', ''.join(json.dumps(r) + '\n' for r in rows))
    per = J.persona(tmp_path)
    assert len(per['samples']) == 10 and per['candidates'] == 25
    text = json.dumps(per, ensure_ascii=False)
    assert '@fanboy99' not in text and 'Budi Viewer' not in text and '[viewer]' in text
    assert all(s['source'] != 'chat' for s in per['samples'])
    assert per['samples'][0]['turn_n'] == 1 and per['samples'][-1]['turn_n'] == 100  # spread end to end
    sample = per['samples'][0]
    assert sample['reply_sha256'] == hashlib.sha256(sample['reply'].encode()).hexdigest()
    put(tmp_path / 'bridge/persona-ratings.json', [dict(reply_sha256=sample['reply_sha256'], rating='up'),
                                                    dict(reply_sha256='f' * 64, rating='down')])
    assert (J.persona(tmp_path)['up'], J.persona(tmp_path)['down']) == (1, 0)


def test_persona_markdown_keeps_owner_ratings_across_regeneration(tmp_path):
    sample = dict(turn_n=1, source='owner_probe', date='Y1 spring 5', clock=900, context='q | x', reply='a|b',
                  reply_sha256='ab' * 32)
    evaluation = dict(run='r1', journey=dict(persona=dict(samples=[sample])))
    out = tmp_path / 'PERSONA_SAMPLES.md'
    write_persona_samples([evaluation], out)
    out.write_text(out.read_text(encoding='utf-8').replace('|  | abababababab |', '| 👍 | abababababab |'), encoding='utf-8')
    write_persona_samples([evaluation], out)
    assert '| 👍 | abababababab |' in out.read_text(encoding='utf-8')
    assert 'a\\|b' in out.read_text(encoding='utf-8')


# --------------------------------------------------------------------------- composite + integration

def test_journey_points_composite(tmp_path):
    start = save(earned=0, values=dict(individualMoneyEarned=0), visited=('Farm',))
    end = save(earned=1200, farming=2, tools=(('Axe', 1),), visited=('Farm', 'Town'),
               values=dict(individualMoneyEarned=1100, questsCompleted=1), friends=(('Leah', 500, 'Friendly'),), done_slots=(0, 1))
    run = staged_run(tmp_path, start, end)
    rows = [dict(ev='part_live_row', skill='panen', verified=True, evidence=dict(observed=dict(effects=[dict(kind='harvest_crop', delta=10)]))),
            dict(ev='mind_reflection', new_goals=['t1'], done=[dict(id='t1', ref='r1', evidence='bundle Sample Bundle A done')])]
    j = J.journey(run, [(None, list(enumerate(rows, 1)))])
    assert j['journey_parts'] == dict(skill_levels=2, tool_upgrades=1, new_places=1, bundles_x2=2, quests_x2=2,
                                      goals_evidenced_x2=2, money_per_500=2, harvested_per_5=2, hearts=2)
    assert j['journey_points'] == 16 and j['partial'] is False


def test_evaluation_markdown_and_scoreboard_carry_the_journey(tmp_path):
    run = staged_run(tmp_path, save(earned=0), save(earned=600))
    put(run / 'request.json', dict(code_sha='s', dll_sha256='d', helper_sha256='h', no_operator_rescue_policy=True))
    put(run / 'window.json', dict(start_at=100, end_at=200))
    journal(run, [dict(ev='trial_started', at=110), _decision('tanam', 'x')])
    put(run / 'runtime/live7/c1/cycle_state.json', {'days': [{'index': 1}]})
    result = evaluate(run)
    assert result['evaluator_version'] == '1.7.16'
    assert result['journey']['progress']['money_earned']['value'] == 600
    assert result['endurance']['game_days'] == 1  # endurance stays its own section
    rank([result])
    write_evaluation(run, result)
    text = (run / 'EVALUATION.md').read_text(encoding='utf-8')
    assert '## Journey (game\'s own yardsticks)' in text and 'Grandpa 0/21' in text
    board = tmp_path / 'SCOREBOARD_AUTO.md'
    write_scoreboard([result], board)
    table = board.read_text(encoding='utf-8')
    assert '| Run | Δearned g |' in table and '| run | 600 |' in table
    assert 'never a ranking input' in table


# --------------------------------------------------------------------------- tool upgrades (ADR 103)

def _tools(**kw):
    return dict(tools=kw)


def test_new_training_rod_is_an_acquisition_not_an_upgrade():  # ADR 103, attempt 47
    start = _tools(**{'(T)Axe': 0, '(T)Hoe': 0, '(T)BambooPole': 0})
    end = _tools(**{'(T)Axe': 0, '(T)Hoe': 0, '(T)BambooPole': 0, '(T)TrainingRod': 1})
    assert J.delta(start, end)['tool_upgrades'] == 0


def test_held_rod_rising_a_level_counts_once_across_rod_names():  # ADR 103
    assert J.delta(_tools(**{'(T)TrainingRod': 1}), _tools(**{'(T)FiberglassRod': 2}))['tool_upgrades'] == 1


def test_axe_upgrade_counts_but_a_new_rod_beside_it_does_not():  # ADR 103
    start = _tools(**{'(T)Axe': 0, '(T)Hoe': 0})
    end = _tools(**{'(T)CopperAxe': 1, '(T)Hoe': 0, '(T)TrainingRod': 1})
    assert J.delta(start, end)['tool_upgrades'] == 1


def test_lost_tool_or_lowered_level_never_goes_negative():  # ADR 103
    assert J.delta(_tools(**{'(T)Axe': 0, '(T)Hoe': 1}), _tools(**{'(T)Axe': 0}))['tool_upgrades'] == 0
    assert J.delta(_tools(**{'(T)CopperAxe': 1}), _tools(**{'(T)Axe': 0}))['tool_upgrades'] == 0


def test_tools_missing_on_either_side_are_unknown():  # ADR 103
    assert J.delta(dict(money_earned=1), _tools(**{'(T)Axe': 0}))['tool_upgrades'] is None
    assert J.delta(_tools(**{'(T)Axe': 0}), dict(money_earned=1))['tool_upgrades'] is None


def test_save_diff_extra_end_class_adds_no_upgrade_but_a_held_one_does(tmp_path):  # ADR 103
    run = staged_run(tmp_path, save(tools=(('Axe', 0),)), save(tools=(('Axe', 0), ('Pan', 0))))
    assert J.journey(run, [])['progress']['tool_upgrades']['value'] == 0
    run2 = staged_run(tmp_path / 'b', save(tools=(('Axe', 0),)), save(tools=(('Axe', 1), ('Pan', 0))))
    assert J.journey(run2, [])['progress']['tool_upgrades']['value'] == 1
