"""Place/time decision projection and executable snapshot reads (ADR 223).

The motor's offer is never modified. Private attributes retain read payloads and
frozen lazy builders; only the small dictionary is serialized as the whole offer.
Fact-line admission runs first, including on subsequently materialized details.
"""
from collections.abc import Mapping
from arti_runtime.native_rows import native_rows
from copy import deepcopy
from dataclasses import dataclass
from arti_runtime.page_snapshot import plain_snapshot, fact_policy_snapshot
from arti_runtime.place_page import RESULT_FIELD
from arti_runtime.self_hud import KEYS as VITALS
import hashlib
import json

SETTING = 'stardew_live7_place_time_page'
READ = 'place_time:'
PROTECTED = frozenset(('muat_ulang','siap_panen','ruang_tas','tujuan_dibawa','tas_kegunaan','tas_cara','bag_native','tas_native',
    'bedtime_facts','malam','cadangan_pulang','bedtime_return','home_unreachable','route_home_blocked',
    'day_rules','batas_waktu','festival_hari','festival_leave','quest_lanjut','kunjungan','warga',
    'di_sini','bisa_di_sini','knocked_out','risiko_tambang','lawan_mine','mancing','jalan_terhalang',
    'diserang','pindah_barang','item_move_outcomes','aksi_terakhir','perubahan_menu','nilai_kiriman','purchase_result',
    'journal_choice','journal_result','menu_kini','held_notice','grant_notice','readiness_notice',
    'native_transfer','transfer_actions','peti_ambil','peti_cocok','kontrol_efek','native_place',
    'pengumuman_game','kotak_surat',  # ADR 227: the game's own words today; the mailbox flag she saw
    'surat_hari_ini',  # the letters she opened today, in the game's words.
    'kiriman_kemarin','map_affordances',  # Recent earnings screen and the current map affordance count.
    'jalan_keluar','dunia_tidak_muat',  # ADR 237 A: the menu's way out (or its answers); world choices left off.
    'ditahan','pemulihan_terakhir',  # ADR 241: a withheld choice and why; what the ladder did, once.
    'tangan','tangan_hasil',  # ADR 243: her raw hands in one line; what the game answered to her last raw press.
    'hasil_berulang',  # what a spot has answered every time she pressed it.
    'tangan_ditahan'))  # ADR 249: a latched HALT holds her hands: why, and who clears it.
ROOT_KEEP = frozenset(('live7','fact_line','owner_goal','mind','budget','danger','gift','decision_feedback',
    '_game_text','action_facts','social_availability','counter_status','native_ui_notices','game_text',
    'halaman_baca','question_reads_closed','help_advisories','peer_sleep'))
# A world page's read is her own notes: what the game showed her and what she was told.
NOTES_FIELD = 'catatan_kubaca'
NOTES = ('game_facts', 'discoveries')
NOTES_BYTES = 6000
NOTE_CHARS = 600
SKILL_KEEP = frozenset(('name','args','nama','lantai','quantity','quantity_rule','batch','items','jurnal','jurnal_baca','peti'))


def enabled(config):
    return (config or {}).get(SETTING) is True


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()[:16]


def day_number(value):
    from arti_runtime.plan_dates import ordinal
    if isinstance(value, (list, tuple)) and len(value) == 3:
        value = 'Y%s %s %s' % tuple(value)
    if type(value) is int:
        return value
    return ordinal('stardew', value) if isinstance(value, str) else None


@dataclass(frozen=True)
class FactRecord:
    path: tuple
    value: object
    places: tuple = ()  # absent labels are uncertainty, never implicitly everywhere
    observed_day: int | None = None
    valid_day: int | None = None
    calendar: bool = False
    priority: int = 4
    historical: bool = False

    def matches(self, places, today):
        if self.valid_day is not None:
            if today is None or not today <= self.valid_day <= today + int(self.calendar):
                return False
        return not self.places or 'everywhere' in self.places or bool(set(self.places) & places)


@dataclass
class LazyDetail:
    kind: str
    arguments: dict

    def __post_init__(self):
        self.arguments = plain_snapshot(self.arguments, path='deferred.'+self.kind)

    def load(self):
        if self.kind == 'world':
            from arti_stardew_world import render, fact_lines
            catalog = self.arguments['catalog']
            return dict(world=render(catalog), game_facts=fact_lines(catalog))
        if self.kind == 'travel':
            return dict(skills=[dict(name='pergi_ke',args=list(self.arguments['after']),after_travel=self.arguments['after'])])
        if self.kind == 'progress':
            from arti_stardew_world import progress_block
            return dict(progress=progress_block(**self.arguments))
        if self.kind == 'recipes':
            rows = self.arguments['rows'] or {}
            return dict(skills=[dict(name='buat', recipes={arg: '%s x%s: %s' % (
                r.get('name') or arg, r.get('n', 1), ', '.join('%s %s/%s' % tuple(n)
                for n in r['needs'] if isinstance(n, list) and len(n) == 3)) for arg, r in rows.items()})])
        raise ValueError('unknown place/time detail builder')


class PageOffer(dict):
    """Serializable small offer plus immutable-by-ownership read snapshot."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.source = {}
        self.deferred = {}
        self.policy = None
        self.handle = ''
        self.records = []
        self.text_batch = None
        self.left_off = {}
        self.left_off_paths = set()
        self.notes = None  # the notes she has not read this session (rows); None = the snapshot read


def notes(source, read=()):
    """What the game showed her and what she was told, newest first, without the rows she has read this
    session. The fact policy has passed over the source."""
    found = [r.get('text') for r in native_rows(source.get('discoveries', ())) if isinstance(r, Mapping)]
    rows = [r for r in [*native_rows(source.get('game_facts', ()))][::-1] + found[::-1] if isinstance(r, str) and r.strip()]
    rows = dict.fromkeys(r if len(r) <= NOTE_CHARS else r[:NOTE_CHARS - 1].rstrip() + '…' for r in rows)
    return [r for r in rows if digest(r) not in read]


def notes_answered(session, messages):
    """The page she just answered carried notes: those rows are read, for this session. The rows stand on the
    session (`notes_unread`): a page reused after a rejected answer keeps its messages, not its offer's attributes."""
    unread = getattr(session, 'notes_unread', None)
    if not unread:
        return
    try:
        seen = json.loads(messages[1]['content']).get(NOTES_FIELD)
        session.notes_read.update(digest(r) for r in unread[:seen['sampai']] if seen)
    except (ValueError, TypeError, IndexError, KeyError, AttributeError):
        return


def places_for(offer, catalog=None):
    from arti_stardew_live7 import destination_map
    graph = (catalog or {}).get('world_graph') or {}
    here = (offer.get('facts') or {}).get('location') or graph.get('current')
    if not here:
        here = ((offer.get('unknown_ui') or {}).get('native_motor') or {}).get('location')
    places = {here} if here else set()
    aliases = {}
    for entry in native_rows(offer.get('skills', ())):
        if entry.get('name') != 'pergi_ke':
            continue
        after = _travel_by_arg(entry.get('after_travel'))
        for arg in native_rows(entry.get('args', ())):
            row = after.get(arg) or {}
            loc = row.get('location') or (graph.get('home') if arg == 'home' else destination_map(arg))
            if loc:
                places.add(loc)
                aliases[arg] = loc
    return here, places, aliases


def small_world(catalog, offer):
    """Select before the world renderer constructs any unrelated place/edge rows."""
    from arti_stardew_world import render
    here, places, _ = places_for(offer, catalog)
    graph = (catalog or {}).get('world_graph') or {}
    selected = dict(catalog or {}, world_graph=dict(graph,
        places=[p for p in native_rows(graph.get('places', ())) if p.get('id') in places or p.get('place') == 'everywhere'],
        connections=[e for e in native_rows(graph.get('connections', ())) if len(e) >= 2 and any(p in places for p in e[:2])]))
    return render(selected)


def destination_row(arg, row, now=None):
    arrival = row.get('arrival') or {}
    hours = arrival.get('native_entry_hours')
    hours = [[d.get('opens_at'), d.get('closes_at')] for d in hours] if isinstance(hours,list) else None
    counter = arrival.get('counter')
    counter = [counter.get('opens_at'),counter.get('closes_at'),counter.get('open_now',counter.get('open_on_arrival'))] if isinstance(counter,dict) else None
    status = arrival.get('entry_status') or None
    return [arg,row.get('label') or row.get('location'),row.get('jalan',arrival.get('travel_estimate_min')),
            row.get('pesan_pintu',hours),status,counter,row.get('can_do') or []]


def _travel_by_arg(travel):
    return {arg: row for arg, row in travel.items() if isinstance(row, Mapping)} if isinstance(travel, Mapping) else {}


def put(body, path, value):
    for key in path[:-1]:
        body = body.setdefault(key, {})
    body[path[-1]] = deepcopy(value)


def record(path, value, here, today):
    if path == ('facts', 'lantai'):
        return FactRecord(path, value, (here,) if here else (), None, None, False, 9, False)
    places, observed, valid = (), None, None
    calendar = path[0] == 'calendar'
    if isinstance(value, Mapping):
        loc = value.get('event_place') or value.get('location') or value.get('place')
        if isinstance(loc, str):
            places = (loc,)
        elif isinstance(loc, (list, tuple)):
            places = tuple(loc)
        for field in ('observed_day','date','day_label'):
            if (observed := day_number(value.get(field))) is not None:
                break
        validity = value.get('valid_time') or {}
        valid = day_number(validity.get('game_day'))
    # Formatters explicitly describe this native read, not arbitrary prose matching.
    if path[0] in ('day','facts') and path[-1] in ('crops','kebun','forage_terlihat','watering_can'):
        places = (here,) if here else ()
    priority = 1 if path[-1] in ('kalender','quest','festival_hari') or calendar else 2 if here in places else 4
    return FactRecord(path, value, places, observed, valid, calendar, priority,
                      path[0] in ('discoveries','game_facts','outcomes','recovery','forage_seen_on_way'))


def prepare(offer, *, config=None, budget=None, deferred=None, catalog=None, policy=None, notes_read=None):
    if not enabled(config):
        return offer
    from arti_runtime.page_budget import DEFAULT
    budget = budget or DEFAULT
    source = plain_snapshot(dict(offer), path='offer')
    result = PageOffer()
    result.source = source
    result.deferred = {key:LazyDetail(lazy.kind,lazy.arguments) for key,lazy in (deferred or {}).items()}
    result.text_batch = deepcopy(source.get('_game_text'))
    result.policy = fact_policy_snapshot(**policy) if policy is not None else None
    def read_identity(value):
        if isinstance(value,dict):return {k:read_identity(v) for k,v in value.items() if k!='captured_tick'}
        if isinstance(value,(list,tuple)):return [read_identity(v) for v in value]
        return value
    if source.get('fact_line') and not isinstance(source.get('unknown_ui'), Mapping):
        # The whole-snapshot read handle was bound to a world that moves, so any change in the world made it stale
        # (`Dunia berubah`). This handle covers only her notes and the scope. Her notes change only when she learns something.
        result.notes = notes(source, notes_read or ())
        result.handle = READ + digest(dict(notes=result.notes, scope=(catalog or {}).get('scope')))
        read = result.handle + ':0' if result.notes else None
    else:
        result.handle = READ + digest(read_identity(dict(source=source,scope=(catalog or {}).get('scope'),
            deferred={k:v.arguments for k,v in result.deferred.items()})))
        read = result.handle + ':0'
    here, places, aliases = places_for(source, catalog)
    today = day_number((source.get('day') or {}).get('date'))
    from arti_runtime.human_host import bounded_peer_sleep, waiting_for_peer_sleep
    waiting_sleep = waiting_for_peer_sleep(source)
    result['live7'] = source.get('live7', True)
    for key in ROOT_KEEP:
        if key == 'peer_sleep':
            if waiting_sleep and key in source:
                result[key] = bounded_peer_sleep(source[key])
            continue
        if key in source:
            result[key] = deepcopy(source[key])
    recent = source.get('recent_actions', [])
    if recent is None:recent=[]
    result['recent_actions'] = deepcopy(recent[-3:] if isinstance(recent, list) else recent)
    result['skills'] = []
    for entry in native_rows(source.get('skills', ())):
        if entry.get('name') == 'read_boundary':
            continue  # replaced with the one executable snapshot entry point
        row = {k:deepcopy(v) for k,v in entry.items() if k in SKILL_KEEP}
        if source.get('fact_line') and entry.get('name') == 'buat' and entry.get('recipes'):
            row['recipes'] = deepcopy(entry['recipes'])
        if entry.get('name') == 'pergi_ke':
            after = _travel_by_arg(entry.get('after_travel'))
            if after:
                row.pop('nama',None)  # names ride the destination rows once
                row['after_travel'] = dict(fields=['arg','name','walk','door_facts','door_now','counter_hours_open','today'],
                    unknown=None, rows=[destination_row(a,after[a]) for a in native_rows(entry.get('args', ())) if a in after])
                if any(RESULT_FIELD in facts for facts in after.values()):
                    row['after_travel']['fields'].append(RESULT_FIELD)
                    for travel in row['after_travel']['rows']:
                        travel.append(after[travel[0]].get(RESULT_FIELD))
            elif entry.get('arrival_facts'):
                row['arrival_facts'] = deepcopy(entry['arrival_facts'])
        result['skills'].append(row)
    if read:
        result['skills'].append(dict(name='read_boundary', args=[read]))
        result['detail_read'] = read
    for root in ('facts','day'):
        result[root] = {}
        for key,value in (source.get(root) or {}).items():
            if key in VITALS or key in PROTECTED or key in ('crops','kebun') or (root == 'facts' and key in ('recent_failures','menu_terakhir','pad_terakhir')):
                result[root][key] = deepcopy(value)
            else:
                result.records.append(record((root,key),value,here,today))
    ui = source.get('unknown_ui')
    if isinstance(ui, Mapping):
        from arti_stardew_generic_ui import buy_rows, ui_offer_pages, _UI_HEADER
        actions = next((e.get('args') or [] for e in native_rows(source.get('skills', ())) if e.get('name') == 'ui_action'), [])
        pages, _, overview_count = ui_offer_pages(ui, actions, page_bytes=10**9)
        result['unknown_ui'] = {k:deepcopy(v) for k,v in ui.items()
            if k in PROTECTED or k in ('text','held_item','ready_check','activity','crafting','bag_native','cursor_settle')}
        # Header predicates remain native; full machine identity/evidence stays behind read.
        result['unknown_ui'].update({k:deepcopy(ui[k]) for k in _UI_HEADER if k in ui
            and k not in ('world','discoveries','recovery_facts','native_motor','recent_ui_results')})
        if overview_count:
            result['unknown_ui']['overview'] = pages[0]
        if buys := buy_rows(ui, actions):
            result['unknown_ui']['buy_actions'] = buys
            if ui.get('shop'):
                from arti_runtime.shop_quantity import HINT
                result['unknown_ui']['buy_quantity'] = HINT
            if ui.get('inspect_page'):
                result['unknown_ui']['inspect_page'] = ui['inspect_page']
    world = source.get('world') or {}
    details = world.get('world_details') or {}
    rows = [p for p in native_rows(details.get('places', ())) if p.get('id') in places or p.get('place') == 'everywhere']
    if rows:
        result['world'] = dict(places=[{k:p[k] for k in ('id','label','reachable_now','locked') if k in p} for p in rows])
        for p in rows:
            path = ('world','facts',p['id'])
            if source.get('fact_line') or p.get('receipt'):
                # Already witnessed; current/offered places are floor. Raw file
                # metadata in an unprojected legacy offer is not a witness.
                put(result, path, p.get('facts', []))
            else:
                result.records.append(record(path,p.get('facts', []),here,today))
    skip_root = ROOT_KEEP | {'skills','recent_actions','facts','day','world','unknown_ui','boundary_page','render_budget','detail_read'}
    if not waiting_sleep:
        skip_root = skip_root - {'peer_sleep'}
    for key,value in source.items():
        if value is None or isinstance(value,(Mapping,list,tuple)) and not value:continue
        if key not in skip_root:
            # Native text is never interpreted as a place label. Unknown labels stay uncertainty.
            result.records.append(record((key,),value,here,today))
    from dataclasses import replace
    from collections import Counter
    result.left_off_paths = {r.path for r in result.records if not r.matches(places,today) or
        r.historical and (r.observed_day is None or today is None or today-r.observed_day>2)}
    def behind(r):
        value = dict(records=len(r.value) if isinstance(r.value,(list,dict)) else 1, observed_day=r.observed_day,
                     status='catatan lama' if r.observed_day is not None else 'waktu pengamatan tidak diketahui')
        if result.notes is None:
            value['read'] = read
        elif r.path[0] in NOTES:
            value.update(dict(read=read) if read else dict(status='sudah kamu baca, belum ada yang baru'))
        return value
    result.records = [replace(r, value=behind(r)) if r.historical and (r.observed_day is None or today is None or today-r.observed_day>2)
        else r for r in result.records if r.matches(places,today)]
    result.records.sort(key=lambda r:(r.priority, -(r.observed_day or -1), tuple(map(str,r.path))))
    # Whole source stays bounded independently of the provider's page budget.
    selected_records = []
    limit = min(16000, budget.page_limit)
    has_eyes = without_local_walks_eyes(result) is not result
    for r in result.records:
        candidate = deepcopy(dict(result)); put(candidate,r.path,r.value)
        size = len(encoded(candidate))
        if size <= limit:
            put(result,r.path,r.value)
            selected_records.append(r)
        elif has_eyes and r.path != ('facts', 'lantai') and size <= limit + 400:
            trimmed = without_local_walks_eyes(candidate)
            if trimmed is not candidate and len(encoded(trimmed)) <= limit:
                result['skills'] = trimmed.get('skills', result.get('skills', []))
                if 'facts' in trimmed:
                    result['facts'] = trimmed['facts']
                has_eyes = False
                put(result,r.path,r.value)
                selected_records.append(r)
            else:
                result.left_off_paths.add(r.path)
        else:
            result.left_off_paths.add(r.path)
    # The renderer may spend less, never draw more data from the read snapshot.
    result.records = selected_records
    result.left_off = dict(Counter('.'.join(map(str,p[:2])) for p in result.left_off_paths))
    result.left_off.update({'deferred.'+k:1 for k in result.deferred})
    return result


MENU_SKILLS = ('ui_action', 'read_boundary')
WORLD_LEFT_OFF = ('Pilihan kegiatan dunia tidak muat di halaman menu ini (%d pilihan). '
                  'Menu ini dan jalan keluarnya tetap lengkap; setelah menu ditutup, halaman dunia berikutnya memuatnya.')


def menu_floor(body):
    """fix137A (ADR 237 A): a menu page's floor is the menu's own parts and its way out.

    mabar 2026-11-18 c1 d2 J1560-J4864: her read journal carried the whole world page's calls one row each (135 offered,
    menu_tempo.world_calls), so the floor passed the page budget and 64 renders failed with
    place_time_mandatory_floor_exceeds_budget: no provider call, no decision, for ten minutes. The world rows that ride an
    already read menu are an addition; they leave first and the page says how many. A page that fits is unchanged."""
    ui = body.get('unknown_ui')
    if not isinstance(ui, Mapping):
        return body, 0
    rows = native_rows(body.get('skills', ()))
    kept = [e for e in rows if e.get('name') in MENU_SKILLS]
    left = sum(max(1, len(e.get('args') or ())) for e in rows if e.get('name') not in MENU_SKILLS)
    if not left:
        return body, 0
    body = dict(body, skills=kept, unknown_ui={k: v for k, v in ui.items() if k != 'dunia_setelah_baca'})
    body['unknown_ui']['dunia_tidak_muat'] = WORLD_LEFT_OFF % left
    return body, left


# ADR 241 C2: a page is never refused for its size. Over budget it is cut in one stated order and says how much left.
CUT_KEY = 'tidak_muat'
NEVER_CUT = frozenset(VITALS) | {CUT_KEY, 'jalan_keluar', 'dunia_tidak_muat', 'ditahan', 'pemulihan_terakhir',
                                   'tangan', 'tangan_hasil', 'tangan_ditahan', 'purchase_result'}
RAW_FLOOR = ('langkah', 'hadap', 'pakai')  # ADR 243: her raw hands are page floor (and `aksi depan`), never cut
RAW_FRONT = 'depan'
WORLD_ROWS = frozenset(('di_sini', 'bisa_di_sini', 'map_affordances', 'kunjungan', 'warga', 'native_place', 'crops',
    'kebun', 'pengumuman_game', 'risiko_tambang', 'jalan_terhalang', 'route_home_blocked', 'festival_hari', 'peti_cocok',
    'peti_ambil', 'kontrol_efek', 'siap_panen', 'kotak_surat', 'kiriman_kemarin', 'hasil_berulang'))  # rows about the world around her
CUT_LINE = ('Halaman ini terlalu panjang, jadi dipotong berurutan (baris dunia dulu, lalu fakta tertua): %s tidak muat. '
            'Yang tampil tetap lengkap dan bisa dipilih.')
SHORT_TEXT = 160


def _dated(value, found):
    if isinstance(value, Mapping):
        day = value.get('day', value.get('first_day', value.get('tanggal', value.get('date'))))
        clock = value.get('clock', value.get('time_of_day', value.get('jam')))
        if day is not None:
            found.append((str(day), clock if type(clock) is int else 0))
        for v in value.values():
            _dated(v, found)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _dated(v, found)
    return found


def _age(value):
    """Oldest first: the earliest (day, clock) a fact carries; an undated fact counts as the newest."""
    found = _dated(value, [])
    return (0, min(found)) if found else (1, ('', 0))


def _without_result(row):
    if isinstance(row, Mapping) and RESULT_FIELD in row:
        return {key: value for key, value in row.items() if key != RESULT_FIELD}
    return row


def _without_travel_results(travel):
    if not isinstance(travel, Mapping):
        return travel
    fields = native_rows(travel.get('fields'))
    if RESULT_FIELD in fields:
        column = fields.index(RESULT_FIELD)
        rows = native_rows(travel.get('rows'))
        if any(len(native_rows(row)) > column and row[column] is not None for row in rows):
            return dict(travel, fields=fields[:column] + fields[column + 1:],
                        rows=[row[:column] + row[column + 1:] if len(native_rows(row)) > column else row
                              for row in rows])
        return travel
    rows = {arg: _without_result(row) for arg, row in travel.items()}
    if any(row is not travel[arg] for arg, row in rows.items()):
        return rows
    return travel


def without_visit_results(body):
    """Remove the optional evidence without changing a destination arg or want."""
    if not isinstance(body, Mapping):
        return body
    updates = {}
    original_skills = native_rows(body.get('skills'))
    skills = []
    for entry in original_skills:
        travel = entry.get('after_travel') if isinstance(entry, Mapping) else None
        stripped = _without_travel_results(travel)
        skills.append(dict(entry, after_travel=stripped) if stripped is not travel else entry)
    if any(new is not old for new, old in zip(skills, original_skills)):
        updates['skills'] = skills
    mind = body.get('mind')
    wants = mind.get('tujuan') if isinstance(mind, Mapping) else None
    if isinstance(wants, Mapping):
        changed_wants = {}
        for horizon, views in wants.items():
            rows = native_rows(views)
            stripped = [_without_result(view) for view in rows]
            if any(new is not old for new, old in zip(stripped, rows)):
                changed_wants[horizon] = stripped
        if changed_wants:
            updates['mind'] = dict(mind, tujuan={**wants, **changed_wants})
    return dict(body, **updates) if updates else body


def without_local_walks_eyes(body):
    """Remove local-walk eyes fields first when over budget, before any existing fact."""
    from arti_runtime.local_walks import without_eyes
    return without_eyes(body)


def cut_to_fit(body, accepts, *, optional=()):
    """ADR 241 C2: the one rule for a page over its budget, instead of refusing it (no provider call, no decision).

    Order: (1) world rows: world choices riding a menu (ADR 237 `menu_floor`), then the page's rows about the world
    around her, the last listed first; (2) the oldest facts (her vitals, a menu's way out and these lines are never
    cut); (3) choices from the end of the page (a menu's own parts stay); (4) long texts shortened. The page then
    says how many left (`tidak_muat`). Returns (body, counts); a page that fits is returned unchanged."""
    from collections import Counter
    if accepts(body):
        return body, {}
    stripped_eyes = without_local_walks_eyes(body)
    if stripped_eyes is not body:
        body = stripped_eyes
        if accepts(body):
            return body, {}
    counts = Counter()
    without_results = without_visit_results(body)
    original = body
    body, world_calls = menu_floor(body) if without_results is body or not accepts(without_results) else (body, 0)
    counts['pilihan dunia'] += world_calls

    def noted(candidate):
        parts = [('%d %s' % (n, k)) for k, n in counts.items() if n]
        if parts:
            candidate = dict(candidate, facts=dict(candidate.get('facts') or {}))
            candidate['facts'][CUT_KEY] = CUT_LINE % ', '.join(parts)
        return candidate

    def fits():
        return accepts(noted(body))

    rows = []
    for root in ('day', 'facts'):
        for n, key in enumerate(body.get(root) or {}):
            if key in NEVER_CUT:
                continue
            world = key in WORLD_ROWS
            rows.append(((0, -n) if world else (1,) + _age(body[root][key]) + (-n,), root, key, world))
    for _, root, key, world in sorted(rows):
        if fits():
            break
        body = dict(body, **{root: {k: v for k, v in body[root].items() if k != key}})
        counts['baris dunia' if world else 'fakta tertua'] += 1
    # A visit result stays after lower-priority facts, but it must never cost a choice.
    if without_results is not original:
        body = deepcopy(body)
        for path in (*reversed(optional), ('recent_actions',), ('world',)):
            if fits():
                break
            parent = body
            for key in path[:-1]:
                parent = parent.get(key) if isinstance(parent, dict) else None
            if isinstance(parent, dict) and path[-1] in parent:
                parent.pop(path[-1])
                counts['baris dunia'] += 1
        if not fits():
            return cut_to_fit(without_results, accepts)
    menu_parts = ('ui_action', 'read_boundary')
    raw = bool((body.get('facts') or {}).get('tangan'))  # ADR 243: the raw-hands block is on this page
    floor = menu_parts + (RAW_FLOOR if raw else ())

    def kept(entry, arg):
        return raw and entry.get('name') == 'aksi' and arg == RAW_FRONT

    while not fits():
        skills = [dict(e) for e in body.get('skills', ())]
        cuttable = [i for i, e in enumerate(skills) if e.get('name') not in floor
                    and (not e.get('args') or any(not kept(e, a) for a in e['args']))]
        if not cuttable:
            break
        last = skills[cuttable[-1]]
        args = list(last.get('args') or ())
        rest = [n for n, a in enumerate(args) if not kept(last, a)]
        if len(args) > 1:
            last['args'] = args[:rest[-1]] + args[rest[-1] + 1:]
        else:
            skills.pop(cuttable[-1])
        counts['pilihan'] += 1
        body = dict(body, skills=skills)
    if not fits():
        for root in ('facts', 'day'):
            for key, value in list((body.get(root) or {}).items()):
                if isinstance(value, str) and len(value) > SHORT_TEXT and key not in (CUT_KEY, 'tangan'):  # ADR 243
                    body = dict(body, **{root: dict(body[root], **{key: value[:SHORT_TEXT] + '…'})})
                    counts['teks dipendekkan'] += 1
    return noted(body), {k: n for k, n in counts.items() if n}


def payload(request, offer):
    return dict(contract='GameDecision.v1', mode='skills',
        echo={k:getattr(request,k) for k in ('request_nonce','goal_id','goal_revision','context_ref')},
        reason=request.reason, **{k:v for k,v in offer.items() if k not in
            ('boundary_page','render_budget','read_page','question_reads_closed','_game_text')})


def full_read(offer):
    full = deepcopy(offer.source)
    for lazy in offer.deferred.values():
        value = lazy.load()
        if offer.policy:
            from arti_runtime.fact_line import present
            value = present(dict(full, **value), **offer.policy)
        for key,block in value.items():
            if key == 'skills':
                rows = native_rows(full.get('skills', ()))
                for n, entry in enumerate(block):
                    # One name can hold two entries (cek_pagi mail); each pairs with its own occurrence
                    same = [r for r in rows if r.get('name') == entry.get('name')]
                    k = sum(e.get('name') == entry.get('name') for e in block[:n])
                    if k < len(same):
                        same[k].update(entry)
            else:
                full[key] = block
    return full


def notes_page(body, offer, accepts):
    """The newest of her unread notes on an ordinary decision page. A read that was offered is always
    answered, however the world or her notes moved since."""
    rows = offer.notes

    def page(end):
        return notes_rows(body, offer, end)

    if not rows:
        return page(0)
    end, size = 0, 0
    while end < len(rows) and (not end or size + len(encoded(rows[end])) <= NOTES_BYTES):
        size += len(encoded(rows[end]))
        end += 1
    while end > 1 and not accepts(page(end)):
        end -= 1
    return page(end if accepts(page(end)) else 0)


def notes_rows(body, offer, end):
    """The page with her first `end` unread notes and the way to the next ones; with none it has no read at all."""
    out = {k: v for k, v in body.items() if k not in (NOTES_FIELD, 'detail_read')}
    out['skills'] = [e for e in body['skills'] if e.get('name') != 'read_boundary']
    if not end:
        return out
    out[NOTES_FIELD] = dict(baris=offer.notes[:end], sampai=end, belum_dibaca=len(offer.notes) - end)
    if end < len(offer.notes):
        out['skills'].append(dict(name='read_boundary', args=['%s:%d' % (offer.handle, end)]))
    return out


def render_selected(request, fits=None, *, metrics=None):
    from arti_stardew_skills import decision_system, encoded_render_bytes, reads_closed, RENDER_RUNGS, tabular_rows, shared_texts
    from arti_runtime.page_budget import for_request
    budget, offer = for_request(request), request.skill_offer
    system = decision_system(offer)

    def shop_pages():
        # The place/time cutter has spent optional context. Keep the menu's
        # own parts and page their costs with the existing lossless UI pager.
        from arti_stardew_skills import render_skill_messages, replace_offer
        from arti_runtime.page_budget import fits_page
        source = dict(offer.source, skills=[e for e in offer.source['skills'] if e['name'] == 'ui_action'])
        source['render_budget'] = min(budget.guard, offer.get('render_budget', budget.guard))
        source.pop('_game_text', None)
        return render_skill_messages(replace_offer(request, source),
            lambda ms: fits_page(ms, budget=budget) and (fits is None or fits(ms)), metrics=metrics)

    shop = (offer.get('unknown_ui') or {}).get('buy_actions')
    if shop and offer['unknown_ui'].get('inspect_page'):
        return shop_pages()
    def packed(body):
        return shared_texts(tabular_rows(reads_closed(body,offer)))
    def messages(body):
        return [dict(role='system',content=system), dict(role='user',content=encoded(packed(body)).decode())]
    schema_overhead = None
    def accepts(body):
        nonlocal schema_overhead
        content = encoded(packed(body)).decode()
        if offer.text_batch:
            from arti_runtime.game_text_delivery import inject
            content = inject([dict(role='system',content=system),dict(role='user',content=content)],offer.text_batch)[1]['content']
        quoted = len(json.dumps(content,ensure_ascii=True).encode())
        if schema_overhead is None:
            schema_overhead = encoded_render_bytes([dict(role='system',content=system),dict(role='user',content=content)]) - quoted
        return len(content.encode()) <= budget.page_limit and schema_overhead + quoted <= min(
            budget.guard, offer.get('render_budget',budget.guard))
    base = {k:deepcopy(v) for k,v in offer.items() if k not in ('boundary_page','render_budget')}
    # Repack once from the protected floor. Optional fields are considered in stable record order.
    for r in offer.records:
        parent = base
        for key in r.path[:-1]:
            parent = parent.get(key,{})
        parent.pop(r.path[-1],None)
    body = payload(request,base)
    world_left = 0
    cut = {}
    if not accepts(body):
        stripped = without_local_walks_eyes(body)
        if stripped is not body:
            body = stripped
    if not accepts(body):
        without_results = without_visit_results(body)
        if without_results is body or not accepts(without_results):
            body, world_left = menu_floor(body)
    if not accepts(body):
        # ADR 241 C2 (was `place_time_mandatory_floor_exceeds_budget`, a refusal: no provider call, no decision).
        body, cut = cut_to_fit(body, accepts)
    selected = offer.get('boundary_page')
    reading = offer.notes is not None and bool(selected) and str(selected[0]).startswith(READ)
    if reading:
        body = notes_page(body, offer, accepts)
    if offer.notes is not None:
        selected = None
    if selected and selected[0] == offer.handle:
        # Read chunks are lossless UTF-8 JSON slices of the complete frozen source.
        # Every field, including an arbitrarily long string, is reachable without truncation.
        raw = encoded(full_read(offer)).decode()
        chunk_size = max(64,(budget.page_limit - len(encoded(body))) // 4)
        chunks = [raw[i:i+chunk_size] for i in range(0,len(raw),chunk_size)] or ['']
        index = selected[1] if len(selected)>1 else 0
        if not 0 <= index < len(chunks):
            raise ValueError('place_time_read_page_not_offered')
        body['detail_page'] = dict(handle=offer.handle, page=index, pages=len(chunks),
            format='concatenate JSON fragments in page order', fragment=chunks[index])
        nav = ([offer.handle+':0'] if index else []) + ([offer.handle+':'+str(index+1)] if index+1<len(chunks) else [])
        body['skills'] = [e for e in body['skills'] if e.get('name')!='read_boundary'] + [dict(name='read_boundary',args=nav)]
        if not accepts(body):
            body, cut = cut_to_fit(body, accepts)  # ADR 241 C2 (was `place_time_read_floor_exceeds_budget`)
    else:
        if selected:
            body['halaman_baca'] = 'Dunia berubah; bacaan ini terikat snapshot sebelumnya.'
        has_eyes = without_local_walks_eyes(body) is not body
        for r in offer.records:
            if reading and r.path[0] in NOTES:
                continue  # the notes themselves are on this page, or none of them fits it
            candidate = deepcopy(body); put(candidate,r.path,r.value)
            if accepts(candidate):
                body = candidate
            elif has_eyes and r.path != ('facts', 'lantai'):
                trimmed = without_local_walks_eyes(candidate)
                if trimmed is not candidate and accepts(trimmed):
                    body = trimmed
                    has_eyes = False
    if metrics is not None and world_left:
        metrics['menu_world_left_off'] = world_left
    if metrics is not None:
        from collections import Counter
        omitted_paths = set(offer.left_off_paths)
        for r in offer.records:
            parent = body
            for key in r.path:
                parent = parent.get(key) if isinstance(parent,dict) else None
            if parent != r.value:
                omitted_paths.add(r.path)
        omitted = dict(Counter('.'.join(map(str,p[:2])) for p in omitted_paths))
        omitted.update({'deferred.'+k:1 for k in offer.deferred})
        metrics['selection_left_off'] = omitted
    result = messages(reads_closed(body,offer))
    if fits is not None and not fits(result):
        stripped = without_local_walks_eyes(body)
        if stripped is not body:
            body = stripped
            result = messages(reads_closed(body, offer))
    while fits is not None and NOTES_FIELD in body and not fits(result):
        # Her notes never cost a choice: they leave first, the oldest half each time, unread; a later page offers them.
        body = notes_rows(body, offer, body[NOTES_FIELD]['sampai'] // 2)
        result = messages(reads_closed(body, offer))
    if fits is not None and not fits(result):
        # ADR 241 C2 (was `place_time_provider_budget_exceeded`): the same cut, to the provider's own bound.
        body, more = cut_to_fit(body, lambda b: accepts(b) and fits(messages(reads_closed(b,offer))),
                                optional=tuple(r.path for r in offer.records))
        cut = {k: cut.get(k, 0) + more.get(k, 0) for k in set(cut) | set(more)}
        result = messages(reads_closed(body,offer))
    if shop and (not accepts(body) or fits is not None and not fits(result)):
        return shop_pages()
    if metrics is not None and cut:
        metrics['page_cut'] = cut
    RENDER_RUNGS[0] = 0
    if metrics is not None:
        metrics.update(budget.telemetry(), whole_page_bytes=len(encoded(offer)),whole_encoded_bytes=encoded_render_bytes(result),
            render_route=4 if 'unknown_ui' in body else 0,render_rung=0,render_count=1,layout_count=0,
            place_time_selected_bytes=len(encoded(offer)))
    return result
