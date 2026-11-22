"""The owner's metric is private, native-proven and excludes the daily board."""
from arti_runtime.finish_progress import measure, story_origins


def test_no_joja_progress_path_and_daily_board_never_counts():
    origins = {'1': dict(origin='letter', source='native:letter'), '2': dict(origin='event', source='native:event'),
               '3': dict(origin='letter', source='native:other')}
    catalog = dict(progress=dict(cc=dict(cc_complete=False, bundles_done=1, bundles_total=31, joja=True)),
        quest_log=dict(complete=True, quests=[dict(id='1', daily=False, completed=True),
            dict(id='2', daily=False, completed=False), dict(id='3', daily=True, completed=True),
            dict(id='4', daily=False, completed=True)]))
    result = measure(catalog, origins)
    assert result['cc'] == dict(cc_complete=False)  # alternative-route shared room flags get no CC credit
    assert [r['id'] for r in result['story_quests']] == ['1', '2']
    assert result['unclassified_non_daily'] == ['4'] and result['future_story_coverage'] == 'unknown'
    assert 'joja' not in repr(result).casefold()


def test_only_positive_native_letter_event_diffs_establish_story_origin():
    rows = [dict(item_id='event1', facts=dict(event_type='Event', changes=['quest:31: absent -> added'])),
            dict(item_id='letter1', facts=dict(event_type='LetterViewerMenu', changes=['quest:2: absent -> added'])),
            dict(item_id='unknown', facts=dict(event_type='Event', unknown=True, changes=['quest:99: absent -> added'])),
            dict(item_id='board', facts=dict(event_type='Billboard', changes=['quest:3: absent -> added']))]
    assert story_origins(rows) == {'31': dict(origin='event', source='native_story:event1'),
                                   '2': dict(origin='letter', source='native_story:letter1')}
    assert measure({})['cc'] == {} and measure({})['quest_log_complete'] is None
