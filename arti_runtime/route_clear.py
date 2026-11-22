"""ADR 176: the shared repair planner's threat view and factual page projection."""
from collections import Counter
from math import hypot

from arti_stardew_debris_route import BLOCKER_NEEDS, blocking_path


def walking_context(context, here):
    """Keep native closed cells closed and exclude monster contact cells, never attack them.

    The actor cell remains a search root. A rock beyond contact reach may open
    the escape even while a monster is behind her. No other unsafe cell is open.
    """
    from arti_stardew_skills_mines import CONTACT_RADIUS_TILES
    from arti_stardew_native_targets import xy
    here = xy(here)
    if here is None:
        return None
    monsters = [m for m in (context.get('combat') or {}).get('monsters', ())
                if not (type(m.get('hp')) is int and m['hp'] <= 0)]
    tiles = [xy(m.get('tile')) for m in monsters]
    if any(t is None for t in tiles):
        return None
    grid = {cell: value for cell, value in context['nav_grid'].items()
            if cell == tuple(here) or all(hypot(cell[0]-t[0], cell[1]-t[1]) > CONTACT_RADIUS_TILES for t in tiles)}
    return dict(context, nav_grid=grid)


def plan(context, here, destinations, **kwargs):
    return blocking_path(context['nav_grid'], here, destinations, context['targets'],
                         [f.get('qualified_item_id') for f in context['inventory']], **kwargs)


def clearing_reaches(context, entries):
    """Read/repair goals only; a refused native side is never a movement stand."""
    from arti_stardew_debris_route import debris_kind, footprint
    from arti_stardew_world_extras import stand_tiles
    facts = context.get('targets', {})
    natural = {t for f in facts.values() if debris_kind(f) in ('stone', 'weeds', 'twig', 'tree') for t in footprint(f)}
    occupied = {t for f in facts.values() for t in footprint(f)}
    out = []
    for entry in entries:
        out.extend(tuple(s) for s in (stand_tiles(entry) or ()))
        for miss in entry.get('stand_misses') or ():
            if (isinstance(miss, (list, tuple)) and len(miss) == 3
                    and all(type(n) is int for n in miss[:2]) and miss[2] in ('object', 'terrain_feature')
                    and abs(miss[0]-entry['x'])+abs(miss[1]-entry['y']) == 1
                    and (tuple(miss[:2]) in natural or tuple(miss[:2]) not in occupied)):
                out.append(tuple(miss[:2]))
    return tuple(dict.fromkeys(out))


def describe(result, energy):
    """Counts refer only to identified pieces on the computed path, never nearby clutter.

    Native facts expose stamina, not per-rock health or future swing costs. State
    that limit instead of presenting an invented exact energy quote.
    """
    pieces = (result or {}).get('obstacles', ())
    counts = Counter(p['kind'] for p in pieces)
    names = {'stone': 'batu', 'twig': 'ranting', 'weeds': 'gulma/daun', 'tree': 'pohon liar', 'protected': 'objek dilindungi',
             'hollow_log': 'kayu besar', 'stump': 'tunggul', 'boulder': 'batu besar', 'meteorite': 'meteorit'}
    line = 'Jalan tertutup: ' + (', '.join('%s %d' % (names[k], n) for k, n in sorted(counts.items()))
                                       if counts else 'jenis dan jumlah penghalang belum terbaca') + '.'
    missing = sorted({BLOCKER_NEEDS[p['kind']] for p in pieces if not p['removable'] and p['kind'] in BLOCKER_NEEDS})
    if any(p['kind'] == 'protected' for p in pieces):
        line += ' Jenis native: ' + ', '.join(sorted({p['label'] for p in pieces if p.get('label')})) + '; tidak termasuk debris.'
    if missing:
        line += ' ' + '; '.join(missing) + '.'
    elif pieces and all(p['removable'] for p in pieces):
        line += ' Alat di tas sesuai untuk debris pada lintasan ini.'
    if pieces and all(p['kind'] == 'weeds' for p in pieces):
        line += ' Biaya sabit 0 energi.'
    else:
        line += ' Biaya energi total belum terbaca (jumlah ayunan native belum diketahui).'
    line += ' Energi kini %s; alat berenergi memerlukan minimal 20, sabit 0.' % (
        '%g' % energy if isinstance(energy, (int, float)) and not isinstance(energy, bool) else 'belum terbaca')
    if (result or {}).get('blocker'):
        line += ' Hasil pemeriksaan: %s.' % result['blocker']
    return line
