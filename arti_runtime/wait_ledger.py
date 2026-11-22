"""ADR 224: fail-open, exclusive Python wait accounting; never a control clock.

Only numeric timings, fixed vocabulary and existing correlation IDs leave here.
Overlapping threads use the most recently entered scope; held input has priority.
The latter is a Python command envelope, not proof of physical pad occupancy.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import heapq
import json
from pathlib import Path
import threading
import time

SETTING = 'stardew_wait_ledger_enabled'
CATEGORIES = ('poll', 'settle_readback', 'paced_inspect', 'route_read', 'catalog',
              'ui_dwell', 'bed', 'native_roundtrip', 'journal_archive', 'provider',
              'offer_build', 'render', 'validation', 'arming', 'worker_poll', 'ipc_retry')
current = ContextVar('wait_ledger', default=None)


class _Disabled:
    enabled = False
    def safe(self, *args, **kwargs):
        return None


_OFF = _Disabled()


class WaitLedger:
    def __init__(self, sink, *, enabled=True, clock=None):
        self.enabled = enabled
        self.sink = sink
        # Python 3.12/Windows monotonic_ns uses 15.625 ms GetTickCount64;
        # perf_counter_ns is monotonic QPC (100 ns on the rig). Control clocks stay untouched.
        self.clock = clock or (lambda: time.perf_counter_ns() // 1_000_000)
        self.lock = threading.RLock()
        self.last = None
        self.serial = 0
        self.scopes = {}
        self.windows = {}
        self.requests = {}
        self.held = set()
        self.action_serial = self.leg_serial = 0
        self.last_site = 0

    def safe(self, method, *args, **kwargs):
        if not self.enabled:
            return None
        with self.lock:
            if not self.enabled:
                return None
            try:
                return getattr(self, method)(*args, **kwargs)
            except Exception as exc:  # instrumentation must never affect play
                self.enabled = False
                try:
                    with using(None):
                        self.sink(dict(ev='FAULT', type='wait_ledger_error', exception=type(exc).__name__))
                except Exception:
                    pass  # even an unavailable telemetry disk cannot stop play
                return None

    def _tick(self):
        now = self.clock()
        elapsed = 0 if self.last is None else now - self.last
        self.last = now
        token = max(self.scopes, key=lambda t: (self.scopes[t]['category'] != 'worker_poll', t), default=None)
        scope = self.scopes.get(token)
        category = 'held_input' if self.held else scope['category'] if scope else 'unattributed'
        for w in self.windows.values():
            w['ms'][category] += elapsed
            if scope is not None and not self.held:
                w['scope_ms'][token] = w['scope_ms'].get(token, 0) + elapsed
                site = scope['site']
                w['site_ms'][site] = w['site_ms'].get(site, 0) + elapsed
            if category == 'unattributed' and elapsed:
                w['unattributed_after_site'][self.last_site] = w['unattributed_after_site'].get(self.last_site, 0) + elapsed
            if w is not self.windows.get('DECISION_GAP') and 'DECISION_GAP' in self.windows:
                w['gap_overlap_ms'][category] += elapsed
        if scope is not None and not self.held:
            scope['exclusive_ms'] += elapsed
        return now

    def enter(self, category, site):
        now = self._tick()
        self.serial += 1
        self.scopes[self.serial] = dict(category=category, site=site, start_ms=now, exclusive_ms=0)
        for w in self.windows.values():
            w['counts'][category] += 1
            w['site_counts'][site] = w['site_counts'].get(site, 0) + 1
        return self.serial

    def leave(self, token):
        now = self._tick()
        scope = self.scopes.pop(token, None)
        if scope is None:
            return
        self.last_site = scope['site']
        for w in self.windows.values():
            # Exclusive time in this window, clipped at its start.
            start = max(scope['start_ms'], w['start_ms'])
            ms = w['scope_ms'].pop(token, 0)
            if ms:
                ref = (ms, scope['site'], start, now, scope['category'])
                heapq.heappush(w['largest'], ref)
                if len(w['largest']) > 10:
                    heapq.heappop(w['largest'])

    def _open(self, kind, now, **ids):
        self.windows[kind] = dict(start_ms=now, ms={c: 0 for c in (*CATEGORIES, 'held_input', 'unattributed')},
            counts={c: int(any(s['category'] == c for s in self.scopes.values())) for c in CATEGORIES},
            largest=[], scope_ms={}, unattributed_after_site={},
            site_ms={}, site_counts={site: sum(s['site'] == site for s in self.scopes.values())
                                     for site in {s['site'] for s in self.scopes.values()}},
            gap_overlap_ms={c: 0 for c in (*CATEGORIES, 'held_input', 'unattributed')},
            offer_builds=0, renders=0, **ids)

    def _close(self, kind, now):
        w = self.windows.pop(kind, None)
        if w is None:
            return
        for token, ms in w['scope_ms'].items():
            scope = self.scopes[token]
            if ms:
                heapq.heappush(w['largest'], (ms, scope['site'], max(w['start_ms'], scope['start_ms']), now, scope['category']))
                if len(w['largest']) > 10:
                    heapq.heappop(w['largest'])
        row = dict(ev='wait_ledger', kind=kind, start_ms=w['start_ms'], end_ms=now,
                   wall_ms=now-w['start_ms'], held_input_ms=w['ms'].pop('held_input'),
                   unattributed_ms=w['ms'].pop('unattributed'), totals_ms=w['ms'], counts=w['counts'],
                   offer_builds=w['offer_builds'], renders=w['renders'],
                   open_input_requests=len(self.held),
                   gap_overlap_ms=w['gap_overlap_ms'],
                   unattributed_after_site=w['unattributed_after_site'],
                   site_ms=w['site_ms'], site_counts=w['site_counts'],
                   largest_waits=[dict(ms=ms, site=site, start_ms=start, end_ms=end, category=category)
                                  for ms, site, start, end, category in sorted(w['largest'], reverse=True)],
                   **{k: v for k, v in w.items() if k in ('action', 'leg', 'skill')})
        with using(None):
            self.sink(row)

    def start(self, skill, resume=False):
        now = self._tick()
        self._close('LEG', now)
        if not resume:
            self._close('ACTION', now)
            self.action_serial += 1
            self._open('ACTION', now, action=self.action_serial, skill=skill)
        self.leg_serial += 1
        self._open('LEG', now, action=self.action_serial, leg=self.leg_serial, skill=skill)

    def terminal(self):
        now = self._tick()
        self._close('LEG', now)
        self._close('ACTION', now)
        # No fabricated previous terminal after a restore/start.
        if 'DECISION_GAP' not in self.windows:
            self._open('DECISION_GAP', now, action=self.action_serial)

    def count(self, category):
        key = {'offer_build': 'offer_builds', 'render': 'renders'}.get(category)
        if key:
            for w in self.windows.values():
                w[key] += 1

    def sent(self, request_id, is_input):
        now = self._tick()
        if is_input:
            self._close('DECISION_GAP', now)
            self.held.add(request_id)
        self.requests[request_id] = now
        with using(None):
            self.sink(dict(ev='wait_ledger_native', request_id=request_id, phase=0, mono_ms=now, input=int(is_input)))

    def received(self, request_id, released=False):
        now = self._tick()
        with using(None):
            self.sink(dict(ev='wait_ledger_native', request_id=request_id, phase=1, mono_ms=now,
                           roundtrip_ms=now-self.requests[request_id] if request_id in self.requests else -1,
                           released=int(released)))
        if released:
            self.held.discard(request_id)
        if request_id not in self.held:
            self.requests.pop(request_id, None)

    def released(self, request_id):
        now = self._tick()
        if request_id in self.held:
            with using(None):
                self.sink(dict(ev='wait_ledger_native', request_id=request_id, phase=2, mono_ms=now, released=1))
        self.held.discard(request_id)
        self.requests.pop(request_id, None)


def for_runtime(runtime):
    ledger = getattr(runtime, '_wait_ledger', None)
    if ledger is None and hasattr(runtime, '_journal_dir'):
        # Allocation has no IO. The session keeps the initial setting, including faults.
        def sink(row):
            from arti_runtime import ipc
            path = Path(runtime._journal_dir) / 'wait_ledger.jsonl'
            if not ipc.append_text(path, json.dumps(row, separators=(',', ':'))+'\n', critical=False):
                raise OSError('wait_ledger_write')
        try:
            ledger = WaitLedger(sink, enabled=(getattr(runtime, '_config', None) or {}).get(SETTING, True))
        except Exception as exc:
            ledger = _OFF
            try:
                with using(None):
                    sink(dict(ev='FAULT', type='wait_ledger_error', exception=type(exc).__name__))
            except Exception:
                pass
        ledger = runtime.__dict__.setdefault('_wait_ledger', ledger)
    return ledger


@contextmanager
def using(ledger):
    token = current.set(ledger)
    try:
        yield
    finally:
        current.reset(token)


def note(method, *args, **kwargs):
    ledger = current.get()
    if ledger is not None:
        return ledger.safe(method, *args, **kwargs)


@contextmanager
def mark(category, site):
    ledger = current.get()
    token = ledger.safe('enter', category, site) if ledger is not None else None
    try:
        yield
    finally:
        if token is not None:
            ledger.safe('leave', token)


def call(category, site, fn, *args, **kwargs):
    with mark(category, site):
        return fn(*args, **kwargs)


def worker_wait(runtime, site, fn, *args, **kwargs):
    routine = getattr(getattr(runtime, '_active', None), 'routine_objective', None)
    bed = (getattr(runtime, '_bed_wait', None) or {}).get('holding') or getattr(routine, 'waiting_since', None) is not None
    return call('bed' if bed else 'worker_poll', site, fn, *args, **kwargs)


def task(runtime=None):
    """Capture telemetry only across an existing thread boundary."""
    ledger = for_runtime(runtime) if runtime is not None else current.get()
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with using(ledger):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def release_verdict(fn):
    @wraps(fn)
    def wrapped(raw, *, request_id=None, result=None):
        answer = fn(raw, request_id=request_id, result=result)
        if answer is True and request_id:
            note('released', request_id)
        return answer
    return wrapped


def span(category, site, *, runtime=False, lifecycle=None):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            ledger = current.get()
            if runtime:
                try:
                    ledger = for_runtime(args[0])
                except Exception:  # construction must also be fail-open
                    ledger = None
            with using(ledger):
                if lifecycle == 'start':
                    skill_call = args[2] if len(args) > 2 else kwargs.get('call')
                    if skill_call is not None:
                        note('start', skill_call.name, kwargs.get('resume') is not None)
                if fn.__name__ in ('_skill_boundary_request', '_render_messages'):
                    note('count', category)
                with mark(category, site):
                    result = fn(*args, **kwargs)
                if lifecycle == 'terminal':
                    note('terminal')
                return result
        return wrapped
    return decorate
