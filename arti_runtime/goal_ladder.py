"""Self-authored planning from witnessed game text only (ADR 221).

The existing mind ledger holds read receipts, questions/guesses and day-plan
receipts. It never supplies a goal or an answer. All entry points are opt-in in
the adapter; importing this module changes no legacy prompt or lifetime.
"""
from copy import deepcopy
import hashlib
import json
import re
import time
from datetime import datetime

import arti_mind as M
import arti_mind_reflect as R

SETTING = 'stardew_live7_goal_ladder'
CAPS = dict(M.GOAL_CAPS, season=3)
TIMEOUT_S = 20.0
INPUT_BYTES = 14000
# ADR 228 part B: speech kinds (owner, herself, a viewer or a donation; all ADR 216 receipts).
SPEECH = ('owner_said', 'arti_said', 'viewer_said')
# ADR 232 part A: lines said TO her (the owner, a viewer, a donation), as against her own spoken lines.
TOLD = ('owner_said', 'viewer_said')
SAID_PAGED_KEEP = 64  # receipt refs of said lines her answered play pages carried (one delivery row, no second store)
# The page's said rows have their own cap inside the mind block (which stays 600 B x scale).
# ADR 231 part B3: a row's date is the game day and clock ("Y1 winter 8 07:10", 17 bytes) instead of the stream date
# (10 bytes); the cap and the room grow by that difference for each of the up to three rows, nothing else changes.
SAID_PAGE_BYTES = 264
SAID_PLAN_KEEP = 6  # ADR 231 part D: the newest said lines the planning input keeps until her seen rows are deferred
SAID_ROOM = 88  # bytes the newest line leaves for each of up to two older lines that touch this page
# What a said word touches on her page: closed list of her action words -> choice names / places. Relevance only;
# it never adds a choice, a rule or a meaning (ADR 228).
SAID_TOUCHES = {'jual': ('jual', 'kirim'), 'kirim': ('kirim',), 'kotak': ('kirim',), 'beli': ('beli',),
                'mancing': ('tangkap_ikan',), 'pancing': ('tangkap_ikan',),
                'siram': ('siram',), 'nyiram': ('siram',), 'tanam': ('tanam',), 'nanam': ('tanam',),
                'benih': ('tanam',), 'bibit': ('tanam',), 'panen': ('panen',), 'tambang': ('mine',),
                'kebun': ('farm',), 'ladang': ('farm',), 'hadiah': ('ngobrol_hadiah',), 'ngobrol': ('ngobrol_hadiah',),
                'tidur': ('tidur',), 'peti': ('pindah_barang',), 'tas': ('inventori',)}
# Words of the always-offered control choices touch nothing by themselves (their destinations still do).
_GENERIC_WORDS = frozenset(('pergi', 'action', 'read', 'boundary', 'inspect', 'tunggu', 'wait', 'aksi', 'jalan',
                            'home', 'cek', 'jelajah'))


def _tag_words(tags):
    """Words of her current place, offered choice names and their arguments (with mapped destinations)."""
    from arti_runtime.stream_notes import _name_parts
    from arti_stardew_live7 import destination_map
    out = set()
    for tag in tags:
        tag = str(tag or '')
        if not tag or tag.startswith('jelajah-'):
            continue
        out.add(tag.casefold())
        place = destination_map(tag)
        for text in (tag, place or ''):
            for token in re.findall(r'[^\W\d_]+', text):
                out.update(p for p in _name_parts(token) if len(p) >= 3)
    return out - _GENERIC_WORDS


def said_touches(text, tag_words):
    """True when what was said names her place, a destination or a choice on this page."""
    from arti_runtime.stream_notes import said_words, _PREFIXES
    words = said_words(text)
    if words & tag_words:
        return True
    return any(word.startswith(prefix + stem) for word in words for prefix in _PREFIXES
               for stem, touched in SAID_TOUCHES.items() if set(touched) & tag_words)
GAME = 'stardew'
NOTES = 'goal_note'
PLAN = 'goal_plan'
DELIVERY = 'goal_delivery'  # display acknowledgements only, never evidence that she saw something
DECISION_NOTE_SCHEMA = {'anyOf': [{'type': 'null'}, {'type': 'object', 'additionalProperties': False,
    'properties': {'kind': {'type': 'string', 'enum': ['question', 'guess']},
                   'text': {'type': 'string', 'minLength': 1, 'maxLength': 100}}, 'required': ['kind', 'text']}]}

SYSTEM = (
    'Kamu ARTI. Ini waktu buat mengingat yang kamu lihat dan menulis keinginanmu sendiri, dalam bahasa Indonesia santai. '
    'yang_kulihat berisi yang kamu temui; text_available=false berarti teksnya belum tertangkap, bukan isi bacaannya. '
    'Itu bukan jawaban atas pertanyaanmu. '
    'dikatakan berisi ucapan pemilik, penonton (oleh "kata <nama>" atau "seorang penonton") atau ucapanmu dengan tanggal '
    'stream, waktu_game saat diucapkan dan pembicara, bukan penemuan, aturan atau bukti game. '
    'Ucapan pemilik adalah pelajaran yang kamu dengar; kalian sama-sama belajar. '
    'yang_kamu_suka adalah ingatan kesukaanmu sendiri, bukan bukti penyelesaian game. '
    'baru_kulihat menandai yang belum sempat ditunjukkan lagi. Kamu boleh penasaran, menduga, atau belum tahu. '
    'catatan.new: pertanyaan atau dugaanmu, prompted_by menunjuk ref yang memicunya; about berisi tempat atau nama '
    'pilihan yang menurutmu berkaitan. catatan.found: yang kamu temukan sendiri, dengan id catatan, text dan ref '
    'dari bukti yang kemudian kamu lihat atau lakukan. Catatanmu boleh tetap terbuka. '
    'catatan.drop berisi id catatan yang sudah nggak pengin kamu simpan terbuka. '
    'goals.new: keinginanmu sendiri, boleh kosong; long untuk jangka panjang, season untuk musim tanggal rencana, '
    'today untuk tanggal rencana, week untuk beberapa hari. source_note boleh id catatanmu atau new:0 untuk '
    'catatan baru pertama; parent boleh id keinginanmu yang mendasarinya, new:0 untuk keinginan baru pertama, atau kosong. '
    'watch boleh null; kalau ada, ref dari ukuran_yang_kulihat dan value angka yang kamu maksud. '
    'goals.done dan goals.progress memakai ref bukti, bukan alasan atau dugaanmu. '
    'Yang terpenuhi menurut angka game ditandai di samping tulisanmu; tulisanmu tetap milikmu. '
    'Keinginan season berlaku sampai hari terakhir musim itu; today, week dan long tetap terbawa sampai kamu '
    'melepas atau menyelesaikannya. today adalah ingatanmu; tomorrow adalah niatmu untuk tanggal_rencana, '
    'termasuk kalau baru sempat merencanakannya pagi. Isi yang tidak berubah boleh tetap. Balas JSON saja.'
)

SCHEMA = deepcopy(R.SCHEMA)
_new = SCHEMA['properties']['goals']['properties']['new']['items']
_new['properties']['horizon']['enum'] = list(M.SEASON_HORIZONS)
_new['properties'].update(
    source_note={'type': 'string', 'maxLength': 40}, parent={'type': 'string', 'maxLength': 40},
    watch={'anyOf': [{'type': 'null'}, {'type': 'object', 'additionalProperties': False,
        'properties': {'ref': {'type': 'string'}, 'value': {'type': 'integer', 'minimum': 1, 'maximum': 100000000}},
        'required': ['ref', 'value']}]})
_new['required'] += ['source_note', 'parent', 'watch']
SCHEMA['properties']['catatan'] = {'type': 'object', 'additionalProperties': False, 'properties': {
    'new': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {'kind': {'type': 'string', 'enum': ['question', 'guess']},
            'text': {'type': 'string', 'maxLength': 100}, 'prompted_by': {'type': 'string'},
            'about': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string', 'maxLength': 40}}},
        'required': ['kind', 'text', 'prompted_by', 'about']}},
    'found': {'type': 'array', 'maxItems': 3, 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {'id': {'type': 'string'}, 'text': {'type': 'string', 'maxLength': 120},
                       'ref': {'type': 'string'}}, 'required': ['id', 'text', 'ref']}},
    'drop': {'type': 'array', 'maxItems': 6, 'items': {'type': 'string'}}}, 'required': ['new', 'found', 'drop']}
SCHEMA['required'].append('catatan')


def enabled(config):
    return (config or {}).get(SETTING, False) is True


def rows(base, scope, kind):
    return M.ledger_rows(base, GAME, scope=scope, kind=kind) if base is not None and scope else []


def put(base, scope, kind, items, day):
    return M.record_items(base, GAME, scope=scope, kind=kind, items=items,
                          day=day, day_label=M.plan_dates.label(GAME, day))


def remember(base, scope, evidence, day):
    """Publish through fix123A's sole receipt API; captured text keeps its existing owner.

    No parallel first-seen store. Native discovery writers still own their text
    rows; this adapter adds current UI text to that same discoveries ledger only
    when it has no existing text ref. Delivery state is not a seen receipt.
    """
    from arti_runtime.seen_receipts import SeenReceipts
    import arti_state
    store = SeenReceipts(base, scope)
    from arti_runtime import jsonl_snapshot  # ADR 231 part C: parsed once per file revision
    existing = jsonl_snapshot.rows(store.path)
    text_ids = {r['id'] for r in existing if r.get('text') and r.get('scope_hash') == M._scope_hash(scope)}
    # fix129A: one ledger read per call, not one per evidence row. An exact repeat
    # returns what note() would return without re-reading; new rows still go
    # through note()/record_discoveries, which keep their own lock and re-read.
    known = store.index(existing)
    fresh = 0
    for e in evidence:
        if not isinstance(e, dict) or not isinstance(e.get('text'), str) or not e['text'].strip():
            continue
        if e.get('kind') not in ('text_read', 'dialogue_read', 'board_read', 'calendar_read', 'system_read',
                                  'item_seen', 'item_obtained', 'location_entered', 'menu_text',
                                  'story_event', 'npc_met', 'door_seen', 'place_learned', 'unknown'):
            continue
        if str(e.get('source') or '').startswith('stream:'):
            continue  # ADR 216 speech already has its own receipt; never mint a native read
        from arti_runtime.experience_facts import person_shown
        if not person_shown(e):
            continue  # ADR 230 N2: friendship data alone is not meeting her
        text = e['text'].strip()
        if text.startswith('dibaca dari menu game '):
            continue  # a receipt without displayed words is not their contents
        if e.get('kind') == 'place_learned':
            text = str(e.get('subject') or '')  # the observed name, no generated meaning/conclusion
        seen_day = e.get('day') if type(e.get('day')) is int else e.get('observed_day')
        if type(seen_day) is not int:
            stamp = str(e.get('date') or '')
            if re.fullmatch(r'\d+:(spring|summer|fall|winter):\d+', stamp):
                year, season, date = stamp.split(':')
                stamp = 'Y%s %s %s' % (year, season, date)
            seen_day = M.plan_dates.ordinal(GAME, stamp) or day
        if seen_day > day:
            continue
        kind = {'location_entered': 'place_entered', 'npc_met': 'villager_met',
                'item_obtained': 'item_first_held', 'story_event': 'event_watched',
                'board_read': 'calendar_page_read', 'calendar_read': 'calendar_page_read'}.get(e['kind'], e['kind'])
        identity = str(e.get('subject') or e.get('id') or hashlib.sha256(text.encode()).hexdigest()[:24])
        if kind in ('calendar_page_read', 'text_read', 'dialogue_read', 'system_read', 'menu_text'):
            identity = identity + ':' + hashlib.sha256(text.encode()).hexdigest()[:16]
        if e.get('bundle_id') is not None:
            kind, identity = 'bundle_note_opened', str(e['bundle_id'])
        elif (e.get('surface') in ('InventoryPage', 'SkillsPage', 'SocialPage', 'MapPage', 'CraftingPage',
                                  'AnimalPage', 'PowersTab', 'CollectionsPage', 'OptionsPage', 'ExitPage')
              and e.get('root_menu') in (None, 'GameMenu')):
            kind, identity = 'menu_tab_opened', e['surface']
        source = str(e.get('source') or 'native_source_unretained')
        if e.get('id') in text_ids:
            text_ref = e['id']
        else:
            # This is an existing text owner, not another receipt store. Give
            # direct accepted UI text the same read-receipt seam as other menus.
            event_id = str(e.get('id') or 'text_read:' + identity + ':' + hashlib.sha256(text.encode()).hexdigest()[:16])
            text_ref = hashlib.sha256((scope + '\0' + event_id).encode()).hexdigest()
            # record_discoveries skips an id it already holds; text_ids holds those ids.
            if (e.get('kind') == 'menu_text' or not e.get('id')) and text_ref not in text_ids:
                M.record_discoveries(base, GAME, scope=scope, day=seen_day,
                    day_label=M.plan_dates.label(GAME, seen_day), discoveries=[dict(id=event_id, kind='captured_text',
                    subject=str(e.get('subject') or identity), text=text, source=source,
                    date=M.plan_dates.label(GAME, seen_day), event_place=e.get('event_place'))])
                text_ids.add(text_ref)
        receipt = store.repeat(known, kind, identity, game_day=seen_day, clock=e.get('clock'),
                               place=e.get('event_place'), source=source, text_ref=text_ref)
        if receipt is None:
            receipt = store.note(kind, identity, game_day=seen_day, clock=e.get('clock'),
                                 place=e.get('event_place'), source=source, text_ref=text_ref)
            known.setdefault((kind, identity), []).append(receipt)
        fresh += int(receipt['first'])
        if e.get('newly_recorded'):
            key = 'seen:' + hashlib.sha256((kind + '\0' + identity).encode()).hexdigest()[:24]
            old_delivery = next((r.get('facts') or {} for r in rows(base, scope, DELIVERY)), {})
            if key in old_delivery.get('announced', ()):
                put(base, scope, DELIVERY, [dict(id='delivery', status='current', facts=dict(old_delivery,
                    announced=[r for r in old_delivery.get('announced', ()) if r != key],
                    delivered=[r for r in old_delivery.get('delivered', ()) if r != key and not r.startswith(key + '#')]))], day)
        # An enabling backfill is old experience, not a fresh first cue. A true
        # first edge from the existing writer can arrive in a late native frame.
        if receipt['first'] and seen_day < day and not e.get('newly_recorded') and e.get('observed_day') != day:
            # Only this receipt's rows carry its refs; project just those (fix129A).
            mine = facts(base, scope, only=(kind, identity))
            acknowledge(base, scope, [f['ref'] for f in mine], day, current=mine)
    return fresh


def facts(base, scope, *, only=None):
    """Receipt-backed evidence rows. ``only=(kind, id)`` projects that receipt's rows only.

    Each receipt projects independently, so the selected rows equal the full
    list filtered to that receipt (fix129A: one ledger read, not two).
    """
    if base is None or not scope:
        return []
    from arti_runtime.seen_receipts import SeenReceipts
    import arti_state
    store = SeenReceipts(base, scope)
    from arti_runtime import jsonl_snapshot  # ADR 231 part C: parsed once per file revision
    text_rows = jsonl_snapshot.rows(store.path)
    owners = {r['id']: r for r in text_rows if r.get('scope_hash') == M._scope_hash(scope) and r.get('text')}
    texts = {key: row['text'] for key, row in owners.items()}
    delivery = next((r.get('facts') or {} for r in rows(base, scope, DELIVERY)), {})
    announced, delivered = set(delivery.get('announced') or ()), set(delivery.get('delivered') or ())
    paged = set(delivery.get('said_paged') or ())
    out = []
    text_index = None
    selected = dict(kind=only[0], id=only[1]) if only is not None else {}
    from arti_runtime.experience_facts import person_shown
    for receipt in store.receipts(rows=text_rows, **selected):
        if not person_shown(receipt):
            continue  # ADR 230 N2: a pre-ADR-230 friendship row is kept, never shown as "met"
        if receipt.get('identity_only'):
            continue  # a save flag proves identity only, never displayed words
        native_kind, identity = receipt['kind'], receipt['id']
        kind = {'place_entered': 'location_entered', 'villager_met': 'npc_met',
                'item_first_held': 'item_obtained', 'event_watched': 'story_event',
                'calendar_day_read': 'board_read', 'calendar_page_read': 'board_read', 'bundle_note_opened': 'menu_text',
                'menu_tab_opened': 'menu_text'}.get(native_kind, native_kind)
        key = 'seen:' + hashlib.sha256((native_kind + '\0' + identity).encode()).hexdigest()[:24]
        text_ref = receipt.get('text_ref')
        if isinstance(text_ref, str) and text_ref.startswith('game_text:'):
            from arti_runtime.game_text_delivery import read_text, texts as game_texts
            if text_index is None:
                text_index = game_texts(base, scope)  # ADR 231 part C: one ledger snapshot for every pointer
            shown = read_text(base, scope, text_ref, index=text_index)
            if shown and shown.get('complete') is True:
                texts[text_ref] = shown['text']
        text = texts.get(text_ref, 'nama atau teks belum tertangkap') if isinstance(text_ref, str) else 'nama atau teks belum tertangkap'
        speech = native_kind in SPEECH
        # Stream text owners are speech even when a legacy text_read receipt points there.
        if not speech and str(receipt.get('source') or '').startswith('stream:'):
            continue
        for index in range(0, len(text), 240):
            ref = key if index == 0 else key + '#' + str(index // 240)
            out.append(dict(ref=ref, receipt_ref=key, receipt_kind=native_kind, receipt_id=identity,
                text_ref=receipt.get('text_ref'), text=text[index:index+240],
                text_available=isinstance(text_ref, str) and text_ref in texts,
                kind=kind, subject=identity, day=receipt['game_day']['absolute'],
                place=receipt['place'], source=receipt['source'],
                basis=native_kind if speech else 'native',
                **(dict(stream_date=(owners.get(text_ref, {}).get('stream') or {}).get('date'),
                        speaker=native_kind.removesuffix('_said'), clock=receipt.get('clock'),
                        **_said_by(native_kind, owners.get(text_ref))) if speech else {}),
                **(dict(paged=True) if speech and key in paged else {}),
                notified=ref in delivered, announced=key in announced,
                **(dict(surface=identity) if native_kind == 'menu_tab_opened' else {}),
                **(dict(bundle_id=identity) if native_kind == 'bundle_note_opened' else {})))
    return out


def said_when(row):
    """ADR 231 part B3: when a line was said, as her page dates it: the game day and clock ("Y1 winter 8 07:10"), so
    "hari ini" in it is anchored to its game day (attempt 87: the owner's festival line rode 37 pages over 8 game days
    with the stream date only). From the said receipt (``day``, ``clock``); a row without a clock gives the game date
    alone; a row without a game day keeps the stream date (rows written before stay readable). The stream date stays
    in the planning input and the voice digest."""
    day = row.get('day') if isinstance(row, dict) else None
    if type(day) is not int or day < 1:
        return row.get('stream_date') if isinstance(row, dict) else None
    text = M.plan_dates.label(GAME, day)
    clock = row.get('clock')
    if type(clock) is int and 0 <= clock <= 2600:
        text += ' %02d:%02d' % divmod(clock, 100)
    return text


def _said_by(kind, owner):
    """ADR 230: a viewer's line says who told her, "kata <name>"; a pre-ADR-230 hash row is "seorang penonton".

    ``mentions`` (viewer names the line itself names) rides for a training
    export's replacement only; the planner input and the page never select it.
    """
    owner = owner or {}
    mentions = dict(mentions=list(owner['mentions'])) if isinstance(owner.get('mentions'), list) and owner['mentions'] else {}
    if kind != 'viewer_said':
        return mentions
    from arti_runtime.stream_notes import said_label
    return dict(oleh=said_label(owner), **mentions)


def acknowledge(base, scope, refs, day, *, full=True, current=None):
    """``current``: facts rows the caller just read, holding every row of ``refs``."""
    refs = set(refs)
    old = next((r.get('facts') or {} for r in rows(base, scope, DELIVERY)), {})
    announced, delivered = set(old.get('announced') or ()), set(old.get('delivered') or ())
    for f in (facts(base, scope) if current is None else current):
        if f['ref'] in refs:
            announced.add(f['receipt_ref'])
            if full:
                delivered.add(f['ref'])
    put(base, scope, DELIVERY, [dict(id='delivery', status='current',
        facts=dict(old, announced=sorted(announced), delivered=sorted(delivered)))], day)


def said_shown(said_rows, evidence):
    """ADR 232 part A: the receipt refs of the said lines a page's ``dikatakan`` rows show (same game date, the same
    words or their visibly shortened start)."""
    shown = [r for r in said_rows or () if isinstance(r, dict) and isinstance(r.get('text'), str)]
    out = []
    for e in evidence:
        if e.get('basis') not in SPEECH or e.get('ref') != e.get('receipt_ref'):
            continue
        when = said_when(e)
        for r in shown:
            text = r['text']
            if r.get('tanggal') == when and (text == e['text'] or (text.endswith('…') and len(text) > 1
                                                                    and e['text'].startswith(text[:-1]))):
                out.append(e['receipt_ref'])
                break
    return out


def page_said(base, scope, refs, day):
    """ADR 232 part A: remember which said lines an answered play page carried, in the one delivery row."""
    refs = [r for r in refs if isinstance(r, str)]
    if not refs or base is None or not scope:
        return
    old = next((r.get('facts') or {} for r in rows(base, scope, DELIVERY)), {})
    paged = [r for r in old.get('said_paged') or () if r not in refs] + list(dict.fromkeys(refs))
    if paged == list(old.get('said_paged') or ()):
        return
    put(base, scope, DELIVERY, [dict(id='delivery', status='current',
        facts=dict(old, said_paged=paged[-SAID_PAGED_KEEP:]))], day)


def expire(goals, day):
    """The only automatic intent mutation: the explicitly chosen season ends."""
    result = []
    for row in goals:
        row = dict(row)
        if (row.get('horizon') == 'season' and row.get('status') == 'open'
                and day > ((row['set_on_day'] - 1) // 28 + 1) * 28):
            row.update(status='expired', expired_on_day=((row['set_on_day'] - 1) // 28 + 1) * 28 + 1)
        result.append(row)
    return result


def calendar_seen(evidence, calendar, day):
    """Only named entries in captured calendar words, never a loaded whole-season catalogue."""
    season = (day - 1) // 28
    read = [e for e in evidence if e.get('kind') in ('board_read', 'calendar_read') and
            type(e.get('day')) is int and (e['day'] - 1) // 28 == season and e.get('text_available', True)]
    if not read:
        return []
    words = '\n'.join(e['text'] for e in read).casefold()
    result = []
    for event in calendar:
        if not event.get('name') or event['name'].casefold() not in words:
            continue
        dates = [date for date in event.get('dates', ()) if
                 (d := M.plan_dates.ordinal(GAME, date)) is not None and (d - 1) // 28 == season
                 and str(date).split(' ', 1)[-1].casefold() in words]
        if dates:
            # Hours/place remain in the literal captured words when actually
            # displayed; matching a name does not reveal loaded hidden metadata.
            result.append(dict(id=event['id'], name=event['name'], dates=dates, kind=event.get('kind')))
    return result


def measurements(evidence, *, snapshot=None, wallet=None, inventory=None, bundles=(), day):
    """Offer only measurements of inspected things, with unambiguous native IDs."""
    from arti_runtime.goal_facts import measurements as legacy
    evidence = [e for e in evidence if e.get('basis') not in SPEECH]
    snap = snapshot or {}
    result = []
    def add(ref, value, label):
        if type(value) is int:
            result.append(dict(ref=ref, value=value, text=label, day=day))
    add('money:wallet', wallet, 'uang di dompet')
    # A physical acquisition/read receipt is required; a shop listing is not ownership.
    owned = {e.get('subject') for e in evidence if e.get('kind') == 'item_obtained'}
    for f in legacy(date=M.plan_dates.label(GAME, day), inventory=inventory):
        if f['kind'] == 'item' and f['name'] in owned:
            add('item:' + f['name'], f['value'], next(iter(f['aliases']), f['name']))
    if any(e.get('surface') == 'SocialPage' for e in evidence):
        for who, points in (snap.get('friends') or {}).items():
            add('heart:' + who, points // 250, who + ' hati')
    if any(e.get('kind') == 'location_entered' and
           str(e.get('subject')).startswith(('Mine', 'UndergroundMine')) for e in evidence):
        add('mine:depth', snap.get('deepest_mine_level'), 'lantai terdalam yang dicatat game')
    inspected = {str(e['bundle_id']) for e in evidence if e.get('bundle_id') is not None}
    for bundle in bundles:
        if str(bundle.get('id')) in inspected and type(bundle.get('complete')) is bool:
            add('bundle:' + str(bundle['id']), int(bundle['complete']), str(bundle.get('name') or bundle['id']))
    return list({r['ref']: r for r in result}.values())


def progress(goal, native):
    watch = goal.get('watch')
    if not isinstance(watch, dict):
        cited = goal.get('evidence') or ()
        return None, ('bukti dikutip hari %s' % cited[-1][0] if cited else 'bukti belum ada')
    fact = next((f for f in native if f['ref'] == watch.get('ref')), None)
    if fact is None or type(fact.get('value')) is not int or type(watch.get('value')) is not int:
        return None, 'belum terlihat lagi'
    if type(fact.get('day')) is int and fact['day'] < goal.get('set_on_day', fact['day']):
        return None, 'belum terlihat lagi'
    value, target = fact['value'], watch['value']
    return value >= target, '%s/%s' % (value, target)


def note_views(base, scope):
    return [dict(id=r['item_id'], text=r['text'], day=r['first_day'], status=r['status'], **r['facts'])
            for r in rows(base, scope, NOTES)]


def plan(base, scope, day):
    return next((r.get('facts') or {} for r in rows(base, scope, PLAN) if r['item_id'] == str(day)), {})


def build_input(base, scope, *, day, target_day, mode, evidence, native, calendar=(), daily=None, verified=(), remembered_day=None, max_bytes=INPUT_BYTES):
    goals = expire(M.load_goals(base, GAME), target_day)
    live = M.open_goals(goals, target_day, horizons=M.SEASON_HORIZONS)
    current_plan = plan(base, scope, target_day)
    wants = []
    for h in ('long', 'season', 'today', 'week'):
        for g in live[h]:
            met, line = progress(g, native)
            wants.append(dict(id=g['id'], horizon=h, text=g['text'], set=M.dated_goal(g, GAME).get('authored_on_day'),
                              maju=line, terpenuhi=met, **{k: g[k] for k in ('watch', 'source_note', 'parent') if k in g}))
    season_first = not any((r.get('facts') or {}).get('season') == (target_day - 1) // 28
                           and (r.get('facts') or {}).get('season_asked') for r in rows(base, scope, PLAN))
    prompts = []
    if not live['long']:
        prompts.append('Dari yang sudah kamu lihat, ada yang kamu inginkan dalam jangka panjang?')
    if season_first:
        prompts.append('Musim tanggal rencana baru mulai atau baru sempat kamu pikirkan. Apa yang kamu inginkan musim ini?')
    if mode == 'append':
        prompts.append('Ada kabar baru di bawah. Ada yang ingin kamu tambahkan ke rencanamu? Yang lain tetap.')
    else:
        prompts.append('Dengan keinginanmu musim ini dan yang kamu tahu, tanggal rencana mau kamu pakai buat apa?')
    native_refs = [dict(ref=f['ref'], text='%s: %s' % (f['text'], f['value']), value=f['value'],
                        day=f.get('day', day)) for f in native]
    payload = dict(tanggal=M.plan_dates.label(GAME, day), tanggal_rencana=M.plan_dates.label(GAME, target_day),
        sisa_hari_musim=28 - (target_day - 1) % 28, pertanyaan=prompts,
        keinginan=wants, slots={h: '%d/%d' % (len(live[h]), CAPS[h]) for h in M.SEASON_HORIZONS},
        catatan=[{k: r[k] for k in ('id', 'text', 'day', 'kind', 'prompted_by', 'prompt', 'about') if k in r}
                 for r in note_views(base, scope) if r['status'] == 'open'],
        yang_kulihat=[{k: e[k] for k in ('ref', 'text', 'day', 'place', 'basis', 'receipt_kind', 'receipt_id', 'text_ref', 'text_available', 'stream_date', 'speaker') if k in e}
                     for e in sorted(evidence, key=lambda e: bool(e.get('notified'))) if e.get('basis') not in SPEECH],
        # ADR 231 part D: who said it and when, the game day and clock too (the page's own `tanggal`), as facts.
        dikatakan=[dict({k:e[k] for k in ('ref','text','day','clock','basis','stream_date','speaker','oleh','text_available','receipt_kind','receipt_id','text_ref') if k in e},
                        **({'waktu_game': said_when(e)} if type(e.get('day')) is int else {}))
                   for e in evidence if e.get('basis') in SPEECH],
        baru_kulihat=[e['ref'] for e in evidence if e.get('basis') not in SPEECH and not e.get('announced', e.get('notified'))],
        ukuran_yang_kulihat=native_refs, kalender=calendar_seen(evidence, calendar, target_day),
        kabar=dict(daily or {}), rencana_sebelumnya=current_plan.get('text', ''),
        verified=[dict(ref=r['ref'], text=r['text'], day=r.get('day', day)) for r in verified],
        season_asked=season_first)
    likes = M.likes_lines(base, GAME, before_day=day)
    if likes:
        payload['yang_kamu_suka'] = likes
    if remembered_day is not None:
        payload['remembered_day'] = remembered_day
        payload['tanggal_yang_diingat'] = M.plan_dates.label(GAME, remembered_day)
    if not payload['kalender']:
        payload['kalender_baca'] = 'Belum ada bacaan tanggal acara musim ini yang tersimpan.'
    recent_plan = next(iter(reversed(rows(base, scope, PLAN))), {})
    if (recent_plan.get('facts') or {}).get('not_kept'):
        payload['not_kept_last_time'] = recent_plan['facts']['not_kept']
    # Old displayed text can be deferred, never required goal/note identity or new news.
    # ADR 231 part D (attempt 87: 16 planning calls, both said lines deferred on every one, `ucapan_ditunda: 2`, while
    # ~300 old seen rows were deferred after them anyway): said lines older than the newest SAID_PLAN_KEEP go first,
    # then the seen rows already shown, then the remaining said lines, oldest first.
    while R._size(payload) > max_bytes and len(payload['dikatakan']) > SAID_PLAN_KEEP:
        payload['dikatakan'].pop(0)
        payload['ucapan_ditunda'] = payload.get('ucapan_ditunda', 0) + 1
    while R._size(payload) > max_bytes and payload['yang_kulihat']:
        removed = payload['yang_kulihat'].pop()
        payload['bacaan_ditunda'] = payload.get('bacaan_ditunda', 0) + 1
        if removed['ref'] in payload['baru_kulihat']:
            payload['baru_kulihat'].remove(removed['ref'])
    while R._size(payload) > max_bytes and payload['dikatakan']:
        payload['dikatakan'].pop(0)
        payload['ucapan_ditunda'] = payload.get('ucapan_ditunda', 0) + 1
    if R._size(payload) > max_bytes:
        raise ValueError('planning_input_bound')
    return payload


def apply_reply(base, scope, raw, payload, *, day, target_day, mode):
    """Only her validated reply writes goals/questions/answers. No default goals."""
    obj = json.loads(raw)
    goals = expire(M.load_goals(base, GAME), target_day)
    open_rows = [g for g in goals if M.goal_alive(g, target_day)]
    fact_rows = payload['yang_kulihat'] + payload.get('dikatakan', []) + payload['verified'] + payload['ukuran_yang_kulihat']
    all_text = {r['ref']: r['text'] for r in fact_rows}
    fact_text = {r['ref']: r['text'] for r in fact_rows if r.get('basis') not in SPEECH and r.get('text_available') is not False}
    # fix131A: an invalid field is refused alone (reason in the row); the rest of her reply is kept.
    result, dropped = R.parse(raw, verified_refs=fact_text, open_goal_ids=[r['id'] for r in open_rows],
                              fact_text=fact_text, horizons=M.SEASON_HORIZONS, per_field=True)
    if mode == 'append':
        # Morning additions cannot silently replace last night's plan or long wants.
        result['goals']['done'] = []
        result['goals']['drop'] = []
    native = {r['ref']: r for r in payload['ukuran_yang_kulihat']}
    for item in list(result['goals']['done']):
        g = next(r for r in open_rows if r['id'] == item['id'])
        if g.get('watch') and progress(g, list(native.values()))[0] is not True:
            result['goals']['done'].remove(item)
            dropped.append(dict(kind='done', reason='native_target_not_met', id=g['id']))
    notes = note_views(base, scope)
    ids = {r['id'] for r in notes}
    note_new = {}
    raw_notes = obj.get('catatan') or {}
    if not isinstance(raw_notes, dict):
        dropped.append(dict(kind='field', field='catatan', reason='wrong_type:catatan'))
        raw_notes = {}
    for note_id in raw_notes.get('drop') or []:
        note = next((n for n in notes if n['id'] == note_id and n['status'] == 'open'), None)
        if note is not None:
            f = {k: v for k, v in note.items() if k not in ('id', 'text', 'day', 'status')}
            f['closed_day'] = day
            put(base, scope, NOTES, [dict(id=note_id, status='dropped', text=note['text'], facts=f)], day)
            note['status'] = 'dropped'
    for i, item in enumerate(raw_notes.get('new') or []):
        if not isinstance(item, dict) or item.get('kind') not in ('question', 'guess') or M.text_reason(item.get('text'), 100):
            dropped.append(dict(kind='note', reason='invalid'))
            continue
        if item.get('prompted_by') not in all_text:
            dropped.append(dict(kind='note', reason='unknown_prompt'))
            continue
        if sum(r['kind'] == item['kind'] and r['status'] == 'open' for r in notes) >= 6:
            dropped.append(dict(kind='note', reason='slot_full'))
            continue
        identity = ('q' if item['kind'] == 'question' else 'g') + str(day) + '-' + str(i + 1)
        while identity in ids:
            identity += 'n'
        tags = [t for t in item.get('about', ()) if isinstance(t, str) and len(t) <= 40][:5]
        f = dict(kind=item['kind'], prompted_by=item['prompted_by'], prompt=all_text[item['prompted_by']],
                 about=tags, known_refs=list(all_text))
        trigger = next(r for r in fact_rows if r['ref'] == item['prompted_by'])
        if trigger.get('receipt_kind'):
            f['source_receipt'] = dict(kind=trigger['receipt_kind'], id=trigger['receipt_id'], text_ref=trigger.get('text_ref'))
        put(base, scope, NOTES, [dict(id=identity, status='open', text=item['text'], facts=f)], day)
        notes.append(dict(id=identity, text=item['text'], status='open', day=day, **f))
        ids.add(identity)
        note_new['new:' + str(i)] = identity
    for item in raw_notes.get('found') or []:
        if not isinstance(item, dict):
            continue
        note = next((r for r in notes if r['id'] == item.get('id') and r['status'] == 'open'), None)
        evidence = next((r for r in fact_rows if r['ref'] == item.get('ref')), None)
        # Finding out requires a later sight/action, not recycling the prompt that raised the question.
        if (note is None or evidence is None or item['ref'] not in fact_text or item['ref'] == note['prompted_by']
                or item['ref'] in note.get('known_refs', ()) and evidence.get('day', day) <= note['day']
                or evidence.get('day', day) < note['day'] or M.text_reason(item.get('text'), 120)):
            dropped.append(dict(kind='found', reason='unproved'))
            continue
        f = {k: note[k] for k in ('kind', 'prompted_by', 'prompt', 'about', 'known_refs', 'source_receipt') if k in note}
        f.update(found=item['text'], evidence=item['ref'], evidence_text=fact_text[item['ref']], found_day=day)
        put(base, scope, NOTES, [dict(id=note['id'], status='found', text=note['text'], facts=f)], day)
    new_raw = (obj.get('goals') if isinstance(obj.get('goals'), dict) else {}).get('new') or []
    if not isinstance(new_raw, list):
        new_raw = []
    before = {g['id'] for g in goals}
    # A morning writes effective today; a night writes tomorrow, preserving actual authorship.
    result_rows, refused = R.apply(goals, result, day=day, game=GAME, calendar=payload['kalender'],
        effective_day=target_day, horizons=M.SEASON_HORIZONS, caps=CAPS)
    new_links = {}
    for i, source in enumerate(new_raw):
        if isinstance(source, dict):
            match = next((r for r in result_rows if r['id'] not in before and r['horizon'] == source.get('horizon')
                          and r['text'] == ' '.join(str(source.get('text') or '').split())), None)
            if match is not None:
                new_links['new:' + str(i)] = match['id']
    for g in result_rows:
        if g['id'] in before:
            continue
        g.update(why_set='planning', authored_on_day=day)
        source = next((r for r in new_raw if isinstance(r, dict) and r.get('horizon') == g['horizon']
                       and ' '.join(str(r.get('text') or '').split()) == g['text']), {})
        origin = note_new.get(source.get('source_note'), source.get('source_note'))
        if origin in ids:
            g['source_note'] = origin
        parent = new_links.get(source.get('parent'), source.get('parent'))
        if parent != g['id'] and parent in {r['id'] for r in result_rows if M.goal_alive(r, target_day)}:
            g['parent'] = parent
        watch = source.get('watch')
        if isinstance(watch, dict) and watch.get('ref') in native and type(watch.get('value')) is int and 1 <= watch['value'] <= 100000000:
            g['watch'] = dict(ref=watch['ref'], value=watch['value'])
        if g['horizon'] == 'season':
            g['ends_on_day'] = ((target_day - 1) // 28 + 1) * 28
    history_trimmed = M.save_goals(base, GAME, result_rows)
    summary = result['tomorrow']
    old_plan = plan(base, scope, target_day)
    text = old_plan.get('text', '') if mode == 'append' else summary
    additions = list(old_plan.get('additions') or [])
    if mode == 'append' and summary:
        additions.append(dict(day=day, text=summary))
    put(base, scope, PLAN, [dict(id=str(target_day), status='planned', text=text or ' ', facts=dict(
        text=text, additions=additions, season=(target_day - 1) // 28,
        season_asked=payload['season_asked'] or old_plan.get('season_asked', False),
        daily=payload['kabar'], observed_refs=[r['ref'] for r in payload['yang_kulihat'] + payload.get('dikatakan', [])],
        not_kept=[dict(horizon=r['horizon'], text=r['text']) for r in refused if r.get('reason') == 'cap'],
        input_bytes=R._size(payload)))], day)
    acknowledge(base, scope, [r['ref'] for r in payload['yang_kulihat'] + payload.get('dikatakan', [])], day)
    # Existing diary/journey surface remains the single home for her account of the day.
    remembered = payload.get('remembered_day', day)
    M.save_journey_row(base, GAME, dict(v=M.MIND_VERSION, schema=M.JOURNEY_SCHEMA,
        id=M.plan_dates.label(GAME, remembered), day=remembered, date=M.plan_dates.label(GAME, remembered),
        reflected_at=datetime.now().astimezone().isoformat(timespec='seconds'), reflected_at_day=day,
        reflection=dict(today=result['today'], tomorrow=result['tomorrow'], fun=result['fun'], not_fun=result['not_fun'])))
    M.save_likes(base, GAME, day=remembered, day_label=M.plan_dates.label(GAME, remembered),
                 fun=result['fun'], not_fun=result['not_fun'], evidence=fact_text)
    return dict(new=[g['id'] for g in result_rows if g['id'] not in before], dropped=dropped + refused,
                season_asked=payload['season_asked'], history_trimmed=history_trimmed)


REASK_SYSTEM = ('Kamu ARTI. Satu bagian tulisanmu di rencana tadi melebihi batasnya, jadi bagian itu belum '
                'tersimpan. Tulis ulang bagian itu dengan kata-katamu sendiri, dalam batasnya. Balas JSON saja.')
CAUSE_CHARS = 160


def reask(complete, raw):
    """fix131A: one bounded re-ask for an over-long text field only, stating its limit.

    Returns (raw, row). Her new words replace the refused field only when they
    are valid; otherwise the field stays refused and empty for this call. The
    program never cuts or rewrites her words. No other field is asked again.
    """
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return raw, None
    if not isinstance(obj, dict):
        return raw, None
    limits = dict(R.TEXT_FIELDS)
    fields = [(f, r) for f, r in R.refused_fields(obj) if f in limits and r.startswith('too_long:')]
    if not fields:
        return raw, None
    row = dict(fields=[f for f, _ in fields], reasons=[r for _, r in fields], accepted=[])
    ask = dict(tulisanmu={f: obj[f] for f, _ in fields},
               batas={f: 'paling banyak %d karakter (tulisanmu %s)' % (limits[f], r.rsplit(':', 1)[-1].split('/')[0])
                      for f, r in fields})
    schema = {'type': 'object', 'additionalProperties': False, 'required': [f for f, _ in fields],
              'properties': {f: {'type': 'string', 'maxLength': limits[f]} for f, _ in fields}}
    start = time.monotonic()
    try:
        again = json.loads(complete([{'role': 'system', 'content': REASK_SYSTEM},
                                     {'role': 'user', 'content': json.dumps(ask, ensure_ascii=False)}],
                                    schema, timeout_sec=TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - the re-ask is optional; the first reply still applies per field
        row.update(result='failed', cause=('%s: %s' % (type(exc).__name__, exc))[:CAUSE_CHARS],
                   seconds=round(time.monotonic() - start, 3))
        return raw, row
    for f, _ in fields:
        if isinstance(again, dict) and R.field_reason(again.get(f), limits[f], f) is None:
            obj[f] = again[f]
            row['accepted'].append(f)
        elif isinstance(again, dict):
            row.setdefault('still_refused', []).append(R.field_reason(again.get(f), limits[f], f))
    row.update(result='accepted' if row['accepted'] else 'refused', seconds=round(time.monotonic() - start, 3))
    return json.dumps(obj, ensure_ascii=False), row


def reflect(complete, base, scope, **inputs):
    start = time.monotonic()
    try:
        payload = build_input(base, scope, **inputs)
        messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
        raw = complete(messages, SCHEMA, timeout_sec=TIMEOUT_S)
        first = raw
        raw, reasked = reask(complete, raw)
        result = apply_reply(base, scope, raw, payload, day=inputs['day'], target_day=inputs['target_day'], mode=inputs['mode'])
        request_bytes = R._size(dict(messages=messages, schema=SCHEMA))
        return dict(ev='goal_planning', mode=inputs['mode'], day=inputs['day'], target_day=inputs['target_day'],
                    input_bytes=R._size(payload), input_token_estimate=(request_bytes + 3) // 4,
                    token_basis='UTF-8 request including schema / 4, estimate; provider archive has usage',
                    output_token_estimate=(len(first.encode()) + 3) // 4,
                    seconds=round(time.monotonic()-start, 3), **result, **({'reask': reasked} if reasked else {}))
    except Exception as exc:
        return dict(ev='goal_planning_failed', mode=inputs.get('mode'), day=inputs.get('day'),
                    seconds=round(time.monotonic()-start, 3), code=type(exc).__name__,
                    cause=str(exc)[:CAUSE_CHARS] or type(exc).__name__, alert=True)


def render(prefs, goals, *, day, evidence, native, notes=(), tags=(), legacy_targets=(), max_bytes=M.BLOCK_MAX_BYTES,
           said_bytes=None, newest_told_first=False):
    """One line set long -> season -> today, file order within each horizon.

    The bound is the entire mind block. One goal per horizon at 600 B; extras
    remain in reflection and are counted. No goal is modified or ranked.
    """
    live = M.open_goals(goals, day, horizons=M.SEASON_HORIZONS)
    # The old code-seeded finishing definition is not a lesson she heard or a
    # want she authored. Keep her stored wants and all other preferences intact.
    defaults = {p['id']: p['text'] for p in M.OWNER_PREFS if p['id'] == 'example-trait-b'}
    prefs = [p for p in prefs if not (p.get('id') in defaults and p.get('source') == 'owner'
                                    and p.get('text') == defaults[p['id']])]
    traits, _ = M.pick_prefs(prefs, game=GAME, tags=tags)
    block = {'tanggal_rencana': day, 'tujuan': {}}
    from arti_runtime.goal_facts import met_status
    for h in ('long', 'season', 'today'):
        remaining = [g for g in live[h] if progress(g, native)[0] is not True and not (
            not g.get('watch') and met_status(g['text'] + ' ' + str(g.get('done_when') or ''), legacy_targets))]
        if remaining:
            g = remaining[0]
            block['tujuan'][h] = [dict(id=g['id'], text=g['text'], set=M.dated_goal(g, GAME).get('authored_on_day'),
                                      maju=progress(g, native)[1])]
            if len(remaining) > 1:
                block.setdefault('rinci', {})[h] = len(remaining) - 1
    if live['week']:
        block.setdefault('rinci', {})['week'] = len(live['week'])
    if traits:
        block['sifat'] = [traits[0]['text']]
    native_text = [e for e in evidence if e.get('basis') not in SPEECH
                   and e.get('text_available') is not False]
    new = next((e for e in native_text if not e.get('announced', e.get('notified'))), None)
    if new:
        block['baru kamu lihat/lakukan'] = dict(ref=new['ref'], text=new['text'], day=new.get('day'))
    else:
        # The longest-term context is only her encountered words, never a checklist.
        learned = [e for e in native_text if e.get('kind') in ('text_read', 'dialogue_read', 'system_read', 'menu_text')]
        if learned:
            block['yang_kulihat'] = dict(ref=learned[-1]['ref'], text=learned[-1]['text'], day=learned[-1].get('day'))
    spoken = [e for e in evidence if e.get('basis') in SPEECH
              and e.get('stream_date') and e.get('text_available') is not False
              and e.get('ref') == e.get('receipt_ref')]
    said_cap = SAID_PAGE_BYTES * max(1, max_bytes // M.BLOCK_MAX_BYTES) if said_bytes is None else said_bytes
    said_rank = {}
    if spoken:
        # ADR 228 part B: the newest line, plus every stored line that touches her place, a destination or a
        # choice on this page, newest first, within the said rows' own cap. Always attributed, dated, verbatim.
        words = _tag_words(tags)
        order = [len(spoken) - 1] + [i for i in range(len(spoken) - 2, -1, -1) if said_touches(spoken[i]['text'], words)]
        # ADR 232 part A (mabar, 2026-11-17): her own spoken replies are said rows too, so the owner's line was never
        # the newest one when her next page came and rode it only if it touched the page. A line said TO her that is
        # newer than every said line her answered play pages carried rides first (newest first); before any page
        # carried one, the newest told line does. Her own line keeps main's place after them.
        told = [i for i, e in enumerate(spoken) if e.get('basis') in TOLD]
        last_paged = max((i for i, e in enumerate(spoken) if e.get('paged')), default=None)
        fresh = (told[-1:] if last_paged is None
                 else [i for i in told if i > last_paged and not spoken[i].get('paged')])[::-1]
        order = fresh + [i for i in order if i not in fresh]
        if newest_told_first:
            own = [i for i, e in enumerate(spoken) if e.get('basis') == 'arti_said']
            touching = [i for i in reversed(range(len(spoken))) if said_touches(spoken[i]['text'], words)]
            order = list(dict.fromkeys(told[-1:] + own[-1:] + touching))
        chosen = []
        for pos, i in enumerate(order if len(order) > 1 else ()):  # one row: exactly main's single newest line
            e = spoken[i]
            # The newest line leaves room for up to two touching lines; a row that does not fit is shortened
            # visibly (full words stay in reflection), never rewritten.
            limit = said_cap - (SAID_ROOM * min(2, len(order) - 1) if pos == 0 else 0)
            for width in (len(e['text']), 160, 120, 96, 72, 56, 40, 24):
                text = e['text'] if width >= len(e['text']) else e['text'][:width - 1] + '…'
                row = dict(oleh=e.get('oleh') or e['speaker'], tanggal=said_when(e), text=text)
                if M.block_bytes([r for _, r in chosen] + [row]) <= limit:
                    chosen.append((i, row))
                    break
        if not chosen:
            i = order[0] if newest_told_first and order else len(spoken) - 1
            e = spoken[i]
            chosen = [(i, dict(oleh=e.get('oleh') or e['speaker'], tanggal=said_when(e), text=e['text']))]
        # Drop order under byte pressure: the newest-only line first when it touches nothing, then the oldest.
        block['dikatakan'] = [r for _, r in (chosen if newest_told_first else sorted(chosen, key=lambda pair: pair[0]))]
        said_rank = {id(r): (2 if i in fresh else 0 if i == len(spoken) - 1 and not said_touches(spoken[i]['text'], words)
                             else 1, i) for i, r in chosen}
        if newest_told_first:
            said_rank = {id(r): (0, -priority) for priority, (_, r) in enumerate(chosen)}
    tag_set = M._tags(tags)
    relevant = next((n for n in notes if n['status'] == 'open' and M._tags(n.get('about') or ()) & tag_set), None)
    if relevant:
        block['penasaran'] = dict(id=relevant['id'], text=relevant['text'], day=relevant['day'],
                                  jenis='pertanyaanmu' if relevant['kind'] == 'question' else 'dugaanmu')
    # Trim excerpts uniformly, not goals by estimated value. Full authored text is retained on disk.
    for width in (72, 48, 32, 20, 12):
        if M.block_bytes(block) <= max_bytes:
            return block
        for views in block['tujuan'].values():
            for v in views:
                if len(v['text']) > width:
                    v['text'] = v['text'][:width-1] + '…'
        for key in ('baru kamu lihat/lakukan', 'yang_kulihat', 'penasaran'):
            if key in block and len(block[key]['text']) > width:
                block[key]['text'] = block[key]['text'][:width-1] + '…'
        for line in block.get('dikatakan', ()):
            if len(line['text']) > width:
                line['text'] = line['text'][:width-1] + '…'
        if width <= 48:
            block.pop('sifat', None)
    # Keep identity/progress for each nonempty horizon. Extra details stay in reflection.
    block.pop('rinci', None)
    for key in ('yang_kulihat', 'penasaran', 'dikatakan'):
        if key == 'dikatakan' and len(block.get('dikatakan') or ()) > 1:
            rows = sorted(block['dikatakan'], key=lambda r: said_rank.get(id(r), (1, 0)))
            while len(rows) > 1 and M.block_bytes(block) > max_bytes:
                rows.pop(0)
                block['dikatakan'] = [r for r in block['dikatakan'] if any(r is k for k in rows)]
        if M.block_bytes(block) > max_bytes:
            block.pop(key, None)
    if M.block_bytes(block) > max_bytes:
        raise ValueError('goal_ladder_bound')
    return block
