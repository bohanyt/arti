"""Checkable home/night page facts; presentation only (ADR 206)."""
from arti_runtime import travel_time as T

RESERVE_MARGIN = 60  # game minutes: the existing decision/repair allowance


def observed_line(context, facts):
    opened = {f['target_location'] for f in context.get('targets', {}).values()
              if f.get('kind') in ('door', 'building_door', 'warp')
              and f.get('enterable') is True and f.get('target_location')}
    if facts.shop_open:
        opened.add(facts.location)
    bag = '?' if facts.free_slots is None else 'penuh' if facts.free_slots == 0 else 'sisa %d slot' % facts.free_slots
    now = facts.time_of_day
    midnight = '?' if type(now) is not int else max(0, 24 * 60 - (now // 100 * 60 + now % 100))
    return 'ke 24:00 %sm; buka teramati: %s; tas %s.' % (midnight, ','.join(sorted(opened)) or '?', bag)


def home_line(location, now, policy):
    if type(now) is not int:
        return None
    info = T.home(location, policy)
    # The internal admission estimate includes a bed interaction; this row says arrival home.
    walk = max(0, info['minutes'] - T.BED_MINUTES)
    latest = 26 * 60 - walk
    if now // 100 * 60 + now % 100 < latest - RESERVE_MARGIN:
        return None
    return ('Pulang ~%dm; kasur milikmu di rumah; jam %02d:%02d; tiba sebelum 02:00 bila berangkat sebelum %s '
            '(perkiraan; margin tampil %dm). %s.' %
            (walk, now // 100 % 24, now % 100, T.clock(latest), RESERVE_MARGIN, info['basis'].replace('; bed allowance 15m', '')))


def bed_line(context, facts, policy, calls, own_state=None):
    if not any(c.name == 'tidur' for c in calls):
        return None
    from arti_stardew_live7 import travel_options
    now = facts.time_of_day
    energy = (own_state or {}).get('energy') or {}
    maximum = energy.get('max')
    current = facts.energy
    vital = '%g/%g%s' % (current, maximum, ' (penuh)' if current == maximum else '') if (
        type(current) in (int, float) and type(maximum) in (int, float)) else '%s/?' % current
    opened = []
    offered = {c.arg for c in calls if c.name == 'pergi_ke'}
    for option in travel_options(None, None, None, facts, context, memory=getattr(policy, 'shop_memory', None)):
        if option['arg'] not in offered:
            continue
        arrival = option['arrival']
        eta = T.estimate(facts.location, option['route'], policy)['minutes']
        for door in arrival['native_entry_hours'] if isinstance(arrival['native_entry_hours'], list) else ():
            start, end = door['opens_at'], door['closes_at']
            if type(now) is int and start <= now < end:
                at = now // 100 * 60 + now % 100 + eta
                counter = arrival.get('counter')
                service = ''
                if counter:
                    end_counter = counter.get('closes_at')
                    service = (', jam konter s/d %02d:%02d' % (end_counter // 100, end_counter % 100)
                               if type(end_counter) is int else ', jam konter belum terbaca')
                opened.append('%s s/d %02d:%02d%s, ~%dm, tiba %s%s' % (
                    option['arg'], end // 100, end % 100, service, eta, T.clock(at),
                    ' lewat jam pintu' if at >= end // 100 * 60 + end % 100 else ''))
                break
    return ('Jam %02d:%02d; energi %s; kasur milikmu di rumah. Pintu buka: %s. '
            '02:00: belum tidur = pingsan; tidur melompati sisa hari.' %
            (now // 100 % 24, now % 100, vital, '; '.join(opened) or 'tidak ada jam buka teramati pada rute yang ditawarkan'))
