"""Private measurement only: CC route and native-proven letter/event quests.

Never put this projection on a decision/reflection page. It is not a goal or a
finite checklist of future story quests. Daily board quests never count.
"""
import re


def story_origins(native_story_rows):
    """Reuse existing positive native story diffs, not titles or a quest-ID table."""
    out = {}
    for row in native_story_rows:
        facts = row.get('facts') or {}
        if facts.get('unknown'):
            continue
        event_type = str(facts.get('event_type') or '')
        origin = 'event' if event_type == 'Event' else 'letter' if 'Letter' in event_type else None
        if origin is None:
            continue
        for change in facts.get('changes') or ():
            match = re.fullmatch(r'quest:([^:]+): absent -> added', str(change))
            if match:
                out[match[1]] = dict(origin=origin, source='native_story:' + str(row.get('item_id') or row.get('id')))
    return out


def measure(catalog, origins=()):
    """Unknown provenance/partial log stays unknown; membership is never progress."""
    cc = ((catalog or {}).get('progress') or {}).get('cc') or {}
    log = (catalog or {}).get('quest_log') or {}
    sources = dict(origins)
    story, unclassified = [], []
    for q in log.get('quests') or ():
        if q.get('daily') is True:
            continue
        identity = str(q.get('id') or q.get('quest_id') or '')
        source = sources.get(identity)
        if q.get('daily') is not False or not source or source.get('origin') not in ('letter', 'event'):
            unclassified.append(identity)
            continue
        story.append(dict(id=identity, completed=q.get('completed') if type(q.get('completed')) is bool else None,
                          origin=source['origin'], source=source['source']))
    cc_progress = ({'cc_complete': False} if cc.get('joja') is True else
        {k: cc[k] for k in ('cc_complete', 'bundles_done', 'bundles_total', 'rooms_done', 'rooms_total') if k in cc})
    return dict(cc=cc_progress,
                story_quests=story, unclassified_non_daily=unclassified,
                quest_log_complete=log.get('complete'), future_story_coverage='unknown')
