"""fix131A part 4: program-written rule sentences leave her page when the fact line is on.

The strings below are the producers' exact sentences as recorded on attempt
85/86 pages. Her own dated outcomes (a knock-out, a gift, a meal with the
gains the game showed) stay or appear as plain records, never as a rule. The
body guard and the bedtime return are mechanisms and are not touched here.
"""
import json
from types import SimpleNamespace as NS

import pytest

import arti_stardew_live7 as L
from arti_runtime import fact_line as F

RECORDED_GOAL = ('Live7: main bebas satu hari penuh, kegiatanmu pilihanmu sendiri. '
                 'Fakta: jam 02:00 karakter pingsan kalau belum tidur, cadangan waktu pulang tetap dijaga.')
MINES = ('Di Mines: HP 0 = pingsan, bangun di klinik, barang bisa hilang (game). Program: lawan_mine mulai HP 80, '
         'saat bertarung di bawah 80% makan/keluar; tambang_bijih keluar bila diserang dan HP <60%; HP <=40% keluar; '
         'dari lantai keluar 20:45 (pulang ~255m + cadangan bersihkan jalan 60m, pingsan 02:00); energi <=60 keluar '
         '(cadangan bersihkan jalan 40 di atas batas tool 20); jatah skill habis = keluar. Sebelum itu pilihanmu.')
GIFT = ('ACTION dengan Object di tangan langsung memberi hadiah tanpa konfirmasi. Untuk ngobrol, pilih sendiri slot '
        'kosong atau alat dahulu. TalkedToToday adalah fakta native.')
FOOD = 'makanan di tas (dimakan memulihkan energi/HP): Common Mushroom x1, Wild Plum x1'
KO = ('Pingsan terakhir Y1 fall 23 (13 hari lalu) UndergroundMine2 jam 13:00; bangun Mine jam 14:40; 0g; '
      'barang (O)388 x5; waktu lewat 100 menit.')
ON = {F.SETTING: True}


def families(page):
    """The attempt-85/86 discovery-audit detectors for these four families (a rig-side audit script)."""
    day = page.get('day') or {}
    return dict(
        clock_collapse_law='jam 02:00 karakter pingsan' in str(page.get('owner_goal', '')),
        mines_clinic_loss_law='bangun di klinik' in str(day.get('aturan_mines', '')),
        gift_action_law_controller_notice='langsung memberi hadiah tanpa konfirmasi' in str(
            (page.get('social_availability') or {}).get('action_notice', '')),
        food_restoration_explanation='dimakan memulihkan energi/HP' in str(day.get('makanan', '')))


def offer(**over):
    page = dict(owner_goal=RECORDED_GOAL, day=dict(date='Y1 winter 8', aturan_mines=MINES, makanan=FOOD),
                facts=dict(location='UndergroundMine1', knocked_out=KO),
                social_availability=dict(observed_npcs=[], status='unknown', held_item='(O)20', action_notice=GIFT),
                skills=[dict(name='pergi_ke', args=['mine', 'home'], after_travel={'mine': dict(label='Mines'),
                                                                                  'home': dict(label='kabinmu')})])
    page.update(over)
    return page


def test_recorded_session_goal_is_the_harness_goal_and_loses_its_collapse_clause():
    # Cause: the session goal is GOAL minus its grant prefix ("Arti, main mandiri "), as the owner
    # turn parses it; fact_line compared only GOAL[:400], so the clause rode 23-44 pages per run.
    assert RECORDED_GOAL == L.GOAL.split('main mandiri ', 1)[1]
    assert RECORDED_GOAL in F.harness_goals() and L.GOAL in F.harness_goals()
    for scale in (1, 2):
        got = F.present(offer(), {}, config=dict(ON, stardew_page_budget_scale=scale))
        assert got['owner_goal'] == 'Live7: main bebas satu hari penuh, kegiatanmu pilihanmu sendiri.'
    owner_words = 'Live7: main bebas. Fakta: jam 02:00 karakter pingsan kalau belum tidur, cadangan waktu pulang tetap dijaga.'
    assert F.present(offer(owner_goal=owner_words), {}, config=ON)['owner_goal'] == owner_words  # not the harness goal


def test_every_family_counts_zero_with_the_fact_line_on_and_off_is_unchanged():
    page = offer()
    assert families(page) == dict.fromkeys(families(page), True)
    got = F.present(page, {}, config=ON)
    assert not any(families(got).values())
    assert F.present(page, {}, config={}) is page  # off: the original object, byte for byte


def test_mines_rules_keep_the_program_rules_and_her_own_knock_out_record():
    got = F.present(offer(), {}, config=ON)
    rules = got['day']['aturan_mines']
    assert rules.startswith('Di Mines: Program: lawan_mine mulai HP 80')
    assert 'klinik' not in rules and 'pingsan 02:00' not in rules
    assert '(pulang ~255m + cadangan bersihkan jalan 60m)' in rules and 'dari lantai keluar 20:45' in rules
    assert got['facts']['knocked_out'] == KO  # her own dated knock-out: where she woke, what she lost
    assert 'knocked_out' not in F.present(offer(facts=dict(location='UndergroundMine1')), {}, config=ON)['facts']


def days(*rows, date=(1, 'fall', 19)):
    return [dict(date=list(date), skills=list(rows))]


def meal(qid='(O)404', name='Common Mushroom', energy=13.0, health=5, nested=True, verified=True):
    effect = dict(qid=qid, consumed=True, energy_gain=energy, health_gain=health)
    observed = dict(eat_effect=effect) if nested else effect
    return dict(skill='makan', verified=verified, reason='observed_eat_effect',
                evidence=dict(observed=observed, names={qid: name}))


def gift(arg='Leek ke Linus', verified=True):
    return dict(skill='ngobrol_hadiah', arg=arg, verified=verified, reason='observed_extras_effect', evidence={})


def test_own_outcomes_are_only_her_verified_meals_and_gifts():
    own = F.own_outcomes(days(meal(), meal('(O)296', 'Salmonberry', 25.0, 11, nested=False), meal('(O)20', 'Leek', verified=False),
                              gift(), gift('Daffodil ke Pam', verified=False), dict(skill='ngobrol_hadiah', arg='Linus', verified=True)))
    assert own == dict(meals={'Common Mushroom': 'kamu makan Y1 fall 19: energi +13, HP +5',
                              'Salmonberry': 'kamu makan Y1 fall 19: energi +25, HP +11'},
                       gifts=['Leek ke Linus, Y1 fall 19'])
    assert F.own_outcomes([]) == dict(meals={}, gifts=[])


def test_runtime_catalog_carries_her_outcomes_from_the_campaign():
    runtime = NS(_config=ON, _live7_mind_base=lambda: None,
                 _skill_session=NS(campaign=NS(days=days(meal(), gift()))))
    catalog = F.runtime_catalog(runtime, dict(scope=None))
    assert catalog['own_outcomes']['gifts'] == ['Leek ke Linus, Y1 fall 19']
    assert 'own_outcomes' not in F.runtime_catalog(NS(_config=ON, _live7_mind_base=lambda: None), dict(scope=None))


def test_receipts_then_lines_gift_and_meal():
    before = F.present(offer(), {}, config=ON)
    assert 'action_notice' not in before['social_availability']
    assert before['day']['makanan'] == 'makanan di tas: Common Mushroom x1, Wild Plum x1'
    own = F.own_outcomes(days(meal(), gift()))
    after = F.present(offer(), dict(own_outcomes=own), config=ON)
    assert after['social_availability']['action_notice'] == 'Hadiah terakhirmu: Leek ke Linus, Y1 fall 19 (dari hasil aksimu sendiri).'
    # Eating Common Mushroom releases its own record only; Wild Plum stays a plain name.
    assert after['day']['makanan'] == ('makanan di tas: Common Mushroom x1 (kamu makan Y1 fall 19: energi +13, HP +5), '
                                       'Wild Plum x1')
    assert not any(families(after).values())
    # A second projection of the same page (a deferred detail read) changes nothing more.
    assert F.present(after, dict(own_outcomes=own), config=ON)['day']['makanan'] == after['day']['makanan']
    assert F.present(after, dict(own_outcomes=own), config=ON)['social_availability'] == after['social_availability']


@pytest.mark.parametrize('page', ['a86-005', 'a86-011', 'a86-003'])
def test_recorded_a86_page_shapes(page):
    """The three shapes the attempt-86 audit counted: a floor page, the Mine exit, the gift notice page."""
    shapes = {'a86-005': offer(social_availability=dict(observed_npcs=[], status='unknown')),
              'a86-011': offer(day=dict(date='Y1 winter 8', aturan_mines=MINES), facts=dict(location='Mine', knocked_out=KO),
                               social_availability=dict(observed_npcs=[], status='unknown')),
              'a86-003': offer(day=dict(date='Y1 winter 8', aturan_mines=MINES), facts=dict(location='Mine', knocked_out=KO))}
    got = F.present(shapes[page], {}, config=ON)
    assert not any(families(got).values())
    assert json.dumps(got, ensure_ascii=False).count('klinik') == 0
