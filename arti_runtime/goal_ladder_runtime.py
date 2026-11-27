"""Thin Live7 adapter for opt-in witnessed planning; no walk execution (ADR 221)."""
from arti_runtime.page_snapshot import plain_snapshot
from arti_runtime.page_budget import PageBudget
from arti_runtime.native_rows import native_rows
from collections.abc import Mapping
import hashlib
import json

from arti_runtime import goal_ladder as G
from arti_runtime import mind_scope
import arti_mind as M


def identity(owner, session):
    from arti_stardew_live7 import mind_day
    return owner._live7_mind_base(), getattr(owner, '_live7_discovery_scope', None), mind_day(session.campaign.date)


def observe(owner, session, catalog):
    if not G.enabled(owner._config) or not isinstance(catalog, dict):
        return
    base, scope, day = identity(owner, session)
    if base is None or not scope or day is None:
        return
    from arti_runtime.experience_facts import experiences
    # These are already accepted observation receipts. Never adopt loaded flags,
    # rewards, unopened bundle rooms or quest catalogue rows as seen text.
    G.remember(base, scope, [dict(e) for e in experiences(catalog)], day,
               mind_scoped=mind_scope.enabled(owner._config))
    owner._goal_ladder_catalog = catalog


def observe_ui(owner, session, view, record):
    if not G.enabled(owner._config) or record is None:
        return
    base, scope, day = identity(owner, session)
    if base is None or not scope or day is None:
        return
    if (not getattr(record, 'authoritative', False) or getattr(record, 'stale', True)
            or getattr(record, 'requires_resync', True) or not getattr(record, 'is_complete', False)):
        return
    from arti_stardew_live7 import mind_day
    actual_day = mind_day((record.facts.get('day_transition') or {}).get('date'))
    if actual_day is not None:
        day = actual_day
    # Reuse current draw text. No new game reader, unseen tabs, future script or
    # letter-body enumeration. Page class is already in UiPadView.
    text = '\n'.join(str(s) for s in (record.facts.get('rendered_ui_text') or ())[:64]) or view.text
    surface = getattr(view, 'page_type', None) or view.menu_type
    pose = None
    if hasattr(owner, '_binding_view') and hasattr(owner, '_state'):
        from scripts.live7.native_facts import bound_pose
        pose = bound_pose(owner)
    entry = dict(kind='menu_text', subject=surface, surface=surface, root_menu=view.menu_type, text=text or '',
                 event_place=getattr(pose, 'location', None), source='native_menu:' + view.menu_type,
                 clock=(record.facts.get('day_transition') or {}).get('time_of_day'))
    bundle = getattr(view, 'bundle', None)
    if bundle is not None:
        entry['bundle_id'] = bundle['index']
        # Requirements of this opened bundle are already in the canonical UI view.
        entry['text'] = (entry['text'] + '\n' + str(bundle['name']) + ': ' + '; '.join(
            '%s %s q%s%s' % (r.get('name') or r['item'], r['need'], r['quality'],
                             ' sudah' if r.get('persistent_completed') is True else '')
            for r in bundle.get('ingredients', ()))).strip()
    G.remember(base, scope, [entry], day, mind_scoped=mind_scope.enabled(owner._config))


def presentation_catalog(owner, session, catalog):
    """Do not let the legacy one-note permission reveal an entire CC catalogue.

    This changes presentation only. Native admission and the explicit opened
    bundle view keep their full state. The ladder recalls displayed note text.
    """
    if catalog.get('_goal_ladder_public'):
        return catalog
    observe(owner, session, catalog)
    result = plain_snapshot(catalog)
    progress = result.get('progress')
    if isinstance(progress, dict):
        owner._goal_ladder_native_progress = plain_snapshot(progress)
        owner._goal_ladder_native_scope = getattr(owner, '_live7_discovery_scope', None)
        base, scope, day = identity(owner, session)
        if base is not None and scope and day is not None:
            from arti_runtime.finish_progress import measure, story_origins
            origins = story_origins(M.ledger_rows(base, 'stardew', scope=scope, kind='native_story'))
            private = measure(catalog, origins)
            key = (scope, json.dumps(private, sort_keys=True))
            if getattr(owner, '_goal_finish_last', None) != key:
                owner._goal_finish_last = key
                owner._journal(dict(ev='goal_finish_progress', day=day, progress=private))
        progress.pop('cc', None)
    result['_goal_ladder_public'] = True
    return result


def _context(owner, session, facts=None, context=None):
    base, scope, day = identity(owner, session)
    if base is None or not scope or day is None:
        return None
    context = context or {}
    catalog = context.get('world_knowledge') or getattr(owner, '_goal_ladder_catalog', None) or {}
    evidence = G.facts(base, scope)
    from arti_runtime.fact_line import goal_evidence
    evidence = goal_evidence(evidence)
    snapshot = getattr(owner, '_live7_progress_last', None)
    raw_progress = (getattr(owner, '_goal_ladder_native_progress', None)
                    if getattr(owner, '_goal_ladder_native_scope', None) == scope else None)
    if raw_progress:
        from arti_stardew_world import progress_snapshot
        snapshot = progress_snapshot(dict(progress=raw_progress))
    wallet = context.get('money') if context.get('ui_complete') else getattr(owner, '_live7_goal_wallet', None)
    inventory = getattr(owner, '_live7_goal_inventory', None)
    bundles = (((raw_progress or catalog.get('progress') or {}).get('cc') or {}).get('bundles') or ())
    native = G.measurements(evidence, snapshot=snapshot, wallet=wallet, inventory=inventory, bundles=bundles, day=day)
    calendar = catalog.get('calendar') or ()  # view-filtered, then season-read filtered by build_input
    daily = {}
    source = getattr(owner, '_live7_day_facts', None) or {}
    if source.get('weather') is not None:
        daily['weather'] = source['weather']
    live = session.campaign.live
    if source.get('weather_tomorrow') is not None and (getattr(live, 'read_today', None) or {}).get('weather'):
        daily['weather_tomorrow'] = source['weather_tomorrow']  # a loaded forecast is not a watched TV forecast
    if context:
        route = (context.get('mines_ui') or {}).get('goal_route') or {}
        if route.get('weather'):
            daily['weather'] = route['weather']
    # Native unread count is a HUD indicator, not the letter's unviewed text.
    mail = catalog.get('mail') or {}
    seen = getattr(owner, '_goal_unread_seen', None)
    fresh = type(mail.get('unread_count')) is int
    if fresh:  # kept under the farm of the catalog it was read from, not the scope at read time
        seen = owner._goal_unread_seen = dict(scope=str(catalog.get('scope') or scope), count=mail['unread_count'], day=day)
    if fresh and seen['scope'] == scope:
        daily['surat_belum_dibaca'] = mail['unread_count']
    elif not fresh and isinstance(seen, dict) and seen.get('scope') == scope:
        # No letter is opened while she sleeps; the first page after waking has no count yet, so the last
        # count she saw on this farm stands, with its day. A fresh count replaces it; another farm's never shows.
        daily['surat_belum_dibaca'] = seen['count']
        daily['surat_terakhir_terlihat'] = M.plan_dates.label(G.GAME, seen['day'])
    crop_memory = getattr(live, 'crop_memory', None)
    if crop_memory is not None:
        from scripts.live7.live7_common import own_crop_reflection_facts
        daily['tanamanmu'] = own_crop_reflection_facts(crop_memory.crops, session.campaign.date)
    shops = getattr(live, 'shop_memory', None)
    if shops is not None:
        witnessed = {e.get('place') for e in evidence if e.get('receipt_kind') in ('shop_menu_seen', 'door_message')}
        saved = shops.dump()
        schedules = {where: value for where, value in (saved.get('schedules') or {}).items() if where in witnessed}
        if schedules:
            daily['jam_konter_yang_dibaca'] = schedules
    return dict(base=base, scope=scope, day=day, evidence=evidence, native=native, calendar=calendar, daily=daily,
                budget=getattr(session, 'page_budget', None) or PageBudget.from_config(getattr(owner, '_config', None) or {}))


def _clock(owner):
    try:
        _, pid = owner._binding_view()
        motor = owner._state.latest(pid)
        from arti_stardew_live7 import mind_day, minutes
        return dict(day=mind_day(owner._date_tuple(motor)), minute=minutes(motor.time_of_day))
    except (AttributeError, TypeError, ValueError):
        return None


def _call(owner, data, *, target_day, mode, verified=(), complete=None):
    provider = getattr(owner, '_provider', None)
    planning = None
    if complete is None:
        # ADR 229: planning follows the session's decision lane. The API provider plans on its own
        # lane, cap and archive (complete_planning); a subscription session keeps its reflection lane.
        planning = getattr(provider, 'complete_planning', None)
        complete = planning or getattr(provider, 'complete_reflection', None)
    if complete is None:
        row = dict(ev='goal_planning_failed', mode=mode, day=data['day'], code='no_reflection_lane',
                   cause='the session provider has no reflection lane', alert=True)
    else:
        before = _clock(owner)
        row = G.reflect(complete, data['base'], data['scope'], day=data['day'], target_day=target_day, mode=mode,
                        evidence=data['evidence'], native=data['native'], calendar=data['calendar'],
                        daily=data['daily'], verified=verified, remembered_day=data.get('remembered_day'),
                        **({'unread': data['unread'], 'unread_seen': data.get('unread_seen')} if 'unread' in data else {}),
                        max_bytes=G.INPUT_BYTES * data['budget'].scale,
                        mind_scoped=mind_scope.enabled(getattr(owner, '_config', None)))
        after = _clock(owner)
        cost = ((after['day'] - before['day']) * 1440 + after['minute'] - before['minute']
                if before and after and before['day'] is not None and after['day'] is not None else None)
        row.update(clock_before=before, clock_after=after, game_minutes=cost,
                   clock_basis='native observations; no pause or clock-free claim', timeout_s=G.TIMEOUT_S,
                   decisions=0)
        if planning is not None:
            row['lane'] = getattr(provider, 'last_planning_lane', None)
    owner._journal(row)
    return row


def morning(owner, session, facts, context):
    """Once before this day's first action page. Append only news; never replan from scratch."""
    if not G.enabled(owner._config):
        return False
    base, scope, day = identity(owner, session)
    if base is None or day is None or getattr(owner, '_goal_ladder_morning', None) == day:
        return False
    catalog = context.get('world_knowledge')
    if catalog is not None:
        owner._live7_discovery_scope = str(catalog.get('scope') or scope or '')
        scope = owner._live7_discovery_scope
        from arti_runtime.experience_facts import view
        observe(owner, session, view(owner._live7_experience_catalog(catalog)))
    if not scope:
        return False
    data = _context(owner, session, facts, context)
    if data is None:
        return False
    owner._goal_ladder_morning = day
    prior = G.plan(base, scope, day)
    mode = 'first_morning' if not prior else 'append'
    if prior:
        expected = prior.get('daily') or {}
        now = data['daily']
        news = {}
        if now.get('weather') is not None and now.get('weather') != expected.get('weather_tomorrow', expected.get('weather')):
            news['weather'] = now['weather']
        if now.get('surat_belum_dibaca') != expected.get('surat_belum_dibaca'):
            news['surat_belum_dibaca'] = now.get('surat_belum_dibaca')
            if 'surat_terakhir_terlihat' in now:
                news['surat_terakhir_terlihat'] = now['surat_terakhir_terlihat']
        old_refs = set(prior.get('observed_refs') or ())
        # What an earlier plan was already made from is no news, however far down its list it stood.
        fresh = [e for e in data['evidence'] if e['ref'] not in old_refs and not e.get('notified')]
        if not news and not fresh:
            owner._journal(dict(ev='goal_planning_unchanged', day=day, decisions=0))
            return False
        data['unread'] = now.get('surat_belum_dibaca')  # letters that still lie there, also when none is new
        data['unread_seen'] = now.get('surat_terakhir_terlihat')  # a carried count says its day
        data['daily'] = news
        data['evidence'] = fresh
    _call(owner, data, target_day=day, mode=mode)
    return True  # caller re-reads facts after the bounded call


def deed(row):
    """One verified row of her day in words she can recognise and cite: what, where, the native catch, how it
    was verified. A machine ref or a tile is no word of hers and is left out."""
    evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
    text, arg = str(row.get('skill')), row.get('arg')
    if isinstance(arg, str) and arg.strip() and ':' not in arg and ',' not in arg and not arg.startswith(('(', 'jelajah-')):
        text += ' ' + arg.strip()[:60]
    place = evidence.get('place_name')
    if isinstance(place, str) and place.strip() and 'belum diketahui' not in place:
        text += ' di ' + place.strip()[:60]
    catch = evidence.get('landed_catch')
    if isinstance(catch, dict) and isinstance(catch.get('name'), str) and catch['name'].strip():
        text += ' (%s)' % catch['name'].strip()[:60]
    return '%s: %s' % (text, row.get('reason'))


def deeds(skills, day):
    """Her verified rows of one day, the same deed once with how often; its first row is the evidence."""
    found = {}
    for i, row in enumerate(skills, 1):
        if row.get('verified') is True:
            found.setdefault(deed(row), []).append(i)
    return [dict(ref='action:%d:%d' % (day, at[0]), day=day, text=text if len(at) == 1 else '%s (%d kali)' % (text, len(at)))
            for text, at in found.items()]


def night(owner, session, complete=None):
    """Night-side plan at the existing verified-sleep terminal; bounded-cost fallback.

    That terminal is already next morning, not a retained native save barrier.
    Therefore this is explicitly measured clock cost, not claimed zero time.
    """
    data = _context(owner, session)
    if data is None:
        return dict(ev='goal_planning_failed', code='scope_unavailable',
                    cause='no save scope or mind folder for planning', alert=True)
    played_day = data['day']
    target_day = played_day + 1
    at = _clock(owner)
    if at and type(at.get('day')) is int and at['day'] >= played_day:
        data['day'] = at['day']  # actual authorship, not backdated to the night being remembered
        target_day = max(target_day, at['day'])
    data['remembered_day'] = played_day
    verified = deeds(session.campaign.days[-1].get('skills', ()), played_day)
    row = _call(owner, data, target_day=target_day, mode='night', verified=verified, complete=complete)
    from arti_runtime import experience_digest  # ADR 228: the day's end, after her own summary (off by default)
    experience_digest.live7_write(owner, session)
    return row


def page(owner, session, facts, calls, offer, prefs, goals):
    data = _context(owner, session, facts)
    if data is None:
        return None
    if mind_scope.enabled(getattr(owner, '_config', None)):
        target_hash = mind_scope.scope_hash(data['scope'])
        legacy_map = mind_scope.load_legacy_scopes(data['base'], G.GAME)
        goals = [g for g in goals if legacy_map.goal_scope(g) == target_hash]
    owner._goal_ladder_place = facts.location
    tags = [str(facts.location or '')] + [c.name for c in calls] + [str(c.arg or '') for c in calls]
    if (offer.get('unknown_ui') or {}).get('menu_type'):
        tags.append(offer['unknown_ui']['menu_type'])
    block = G.render(prefs, goals, day=data['day'], evidence=data['evidence'], native=data['native'],
                     notes=G.note_views(data['base'], data['scope']), tags=tags,
                     legacy_targets=getattr(owner, '_live7_goal_targets', ()),
                     newest_told_first=(getattr(owner, '_config', None) or {}).get('live7_fact_line_enabled') is True,
                     plan_on_page='_live7_day_plan' in offer,
                     max_bytes=M.BLOCK_MAX_BYTES * data['budget'].scale)
    want_visits(block, getattr(session.campaign.live, 'place_visits', None) if session.campaign else None,
                getattr(owner, '_live7_native_place_names', None), data['day'],
                M.BLOCK_MAX_BYTES * data['budget'].scale, texts={g['id']: g['text'] for g in goals})
    return block


def page_plan(owner, session, snapshot, offer):
    """Use the existing world/actor scope, never a menu or a festival page."""
    from arti_stardew_live7 import mind_day
    motor = getattr(snapshot, 'motor', None)
    if (not G.plan_on_page_enabled(owner._config) or offer.get('gift') or getattr(motor, 'active_menu', None)
            or (offer.get('day') or {}).get('festival_leave')
            or (getattr(snapshot, 'event_movement', None) or {}).get('is_festival') is True
            or (getattr(snapshot, 'event_actions', None) or {}).get('festival_id')):
        return None
    base, scope, day = identity(owner, session)
    if base is None or day is None:
        return None
    if day != mind_day(owner._date_tuple(motor)):
        return G.PagePlan(day, 'no_plan')
    return G.read_page_plan(base, scope, day)


def want_visits(block, records, names, day, max_bytes, *, texts=None):
    """ADR 232 part C: a want that names a place she has a record of carries that record (what she did there and
    what came of it), from the same place_visit rows as the trip label; only when it fits the block. Her own words
    are matched to the native place names her page uses; nothing about what would make the place useful."""
    from arti_runtime.fact_line import _name_pattern
    from arti_runtime.place_page import RESULT_FIELD, result_label, visit_label
    if not isinstance(records, Mapping) or not records or not isinstance(block, dict) or not isinstance(names, dict):
        return block
    wants = block.get('tujuan')
    if not isinstance(wants, Mapping):
        return block
    texts = texts if isinstance(texts, Mapping) else {}
    today = M.plan_dates.label(G.GAME, day)
    for views in wants.values():
        for view in native_rows(views):
            if not isinstance(view, dict):
                continue
            text = str(texts.get(view.get('id')) or view.get('text') or '')
            for place, label in names.items():
                if (isinstance(place, str) and place in records and isinstance(label, str)
                        and any(len(alias) >= 4 and _name_pattern(alias).search(text)
                                for alias in (label.split(' (', 1)[0], place))):
                    if result := result_label(records, place):
                        view[RESULT_FIELD] = result
                        if M.block_bytes(block) > max_bytes:
                            # Same expendable mind details as the ladder's byte trim.
                            extras = {}
                            for key in ('sifat', 'rinci', 'yang_kulihat', 'penasaran'):
                                if key in block:
                                    extras[key] = block.pop(key)
                                if M.block_bytes(block) <= max_bytes:
                                    break
                            else:
                                view.pop(RESULT_FIELD)
                                block.update(extras)
                    line = visit_label(records, place, today)
                    if line:
                        view['kunjungan'] = line
                        if M.block_bytes(block) > max_bytes:
                            view.pop('kunjungan')
                    break
    return block


def received(owner, session, mind, *, request=None):
    """A notification is consumed only by a returned decision on a page containing its text."""
    if not G.enabled(owner._config):
        return
    base, scope, day = identity(owner, session)
    new = mind.get('baru kamu lihat/lakukan') or {}
    if base is not None and scope and day is not None and new.get('ref'):
        # The cue is delivered once; an excerpt's full captured text still reaches reflection.
        full = next((e for e in G.facts(base, scope) if e['ref'] == new['ref']), None)
        if full:
            G.acknowledge(base, scope, [new['ref']], day, full=full['text'] == new.get('text'))
    said = mind.get('dikatakan')
    if base is None or not scope or day is None or not said:
        return
    final_page = G.plan_on_page_enabled(owner._config)
    if final_page:
        render = getattr(getattr(owner, '_provider', None), '_render_messages', None)
        if request is None or not callable(render):
            return
        # The provider's request-scoped render cache also covers retry reuse.
        shown = json.loads(render(request)[1]['content']).get('mind') or {}
        said = shown.get('dikatakan') if isinstance(shown, Mapping) else None
    if said:
        G.page_said(base, scope, G.said_shown(said, G.facts(base, scope), require_attribution=final_page), day)


def decision_note(owner, session, decision, request):
    """An optional independent authored note, dated and bound to the prompting page.

    Called after the model returned; never reads or changes choice admission.
    A malformed/absent/off-mode note does not affect the action.
    """
    if not G.enabled(owner._config):
        return
    note = getattr(decision, 'catatan', None)
    if (not isinstance(note, dict) or note.get('kind') not in ('question', 'guess')
            or M.text_reason(note.get('text'), 100)):
        return
    base, scope, day = identity(owner, session)
    if base is None or not scope or day is None:
        return
    page_ref = 'page:' + hashlib.sha256(json.dumps([
        getattr(request, 'request_nonce', None), getattr(request, 'context_ref', None),
        getattr(request, 'goal_id', None), getattr(request, 'goal_revision', None)], ensure_ascii=False).encode()).hexdigest()[:24]
    identity_note = ('q' if note['kind'] == 'question' else 'g') + str(day) + '-' + page_ref[5:17]
    existing = G.note_views(base, scope)
    if any(r['id'] == identity_note for r in existing):
        return
    offer = request.skill_offer or {}
    pose = getattr(getattr(request, 'snapshot', None), 'motor', None)
    if pose is not None:  # the page's own day, before room is made: a note of today is never one of a day ahead
        from arti_stardew_live7 import mind_day
        date = (owner._date_tuple(pose) if hasattr(owner, '_date_tuple') else
                tuple(getattr(pose, k, None) for k in ('year', 'season', 'day')))
        if (actual_day := mind_day(date)) is not None:
            day = actual_day
    here = [t for t in (getattr(pose, 'location', None), (offer.get('facts') or {}).get('location')) if t]
    if not G.make_room(base, scope, existing, note['kind'], day, note['text'], here):
        owner._journal(dict(ev='goal_note_not_kept', day=day, reason='slot_full', kind=note['kind'], page_ref=page_ref))
        return
    place = getattr(pose, 'location', None) or (offer.get('facts') or {}).get('location') or getattr(owner, '_goal_ladder_place', None)
    seen = (offer.get('mind') or {}).get('baru kamu lihat/lakukan') or {}
    # A generic walk being offered everywhere does not make every note relevant
    # everywhere. Reuse the actual place/menu/encounter identity only.
    source_fact = next((e for e in G.facts(base, scope) if e['ref'] == seen.get('ref')), {})
    about = [str(t) for t in dict.fromkeys((place, (offer.get('unknown_ui') or {}).get('menu_type'),
                                         source_fact.get('subject'))) if t]
    f = dict(kind=note['kind'], prompted_by=page_ref, page_ref=page_ref,
             prompt=seen.get('text') or 'halaman game yang kamu lihat', place=place, about=about,
             known_refs=[r['ref'] for r in G.facts(base, scope)])
    if source_fact.get('receipt_kind'):
        f['source_receipt'] = dict(kind=source_fact['receipt_kind'], id=source_fact['receipt_id'],
                                   text_ref=source_fact.get('text_ref'))
    G.put(base, scope, G.NOTES, [dict(id=identity_note, status='open', text=note['text'], facts=f)], day)
    owner._journal(dict(ev='goal_note', id=identity_note, day=day, place=place, page_ref=page_ref, kind=note['kind']))
