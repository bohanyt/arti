"""Revision-keyed pure offer components (ADR 223); never cache observation writes."""
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import fields, is_dataclass
from functools import wraps
from threading import RLock
import io
import pickle


class _DataKey(pickle.Pickler):
    def reducer_override(self, value):
        # Built-in containers/scalars use pickle's C fast path. Custom runtime
        # objects must never supply identity-only __getstate__ cache keys.
        raise TypeError(type(value).__name__)


def _key(args, kwargs):
    stream = io.BytesIO()
    _DataKey(stream, protocol=5).dump((args, kwargs))
    return stream.getvalue()

SETTING = "stardew_offer_cache_enabled"
ENABLED = ContextVar("offer_cache_enabled", default=False)
STATS = {}

def revision(value):
    """Order- and type-preserving content revision, including every receipt occurrence.

    Unsupported runtime objects deliberately bypass caching; identity/repr is never
    treated as a revision of mutable state. No hashes/serialized private text escape.
    """
    if value is None or type(value) in (str, int, float, bool, bytes):
        return (type(value), value)
    if isinstance(value, Mapping):
        return (type(value), tuple((revision(k), revision(v)) for k, v in value.items()))
    if type(value) in (tuple, list):
        return (type(value), tuple(revision(v) for v in value))
    if type(value) in (set, frozenset):
        return (type(value), frozenset(revision(v) for v in value))
    if is_dataclass(value) and not isinstance(value, type):
        return (type(value), tuple((f.name, revision(getattr(value, f.name))) for f in fields(value)))
    raise TypeError(type(value).__name__)

@contextmanager
def using(enabled):
    token = ENABLED.set(enabled)
    try:
        yield
    finally:
        ENABLED.reset(token)

def memoized(function):
    cache, lock = OrderedDict(), RLock()
    name = function.__module__ + "." + function.__name__
    @wraps(function)
    def call(*args, **kwargs):
        if not ENABLED.get():
            return function(*args, **kwargs)
        try:
            key = _key(args, kwargs)
        except TypeError:
            return function(*args, **kwargs)
        with lock:
            counts = STATS.setdefault(name, dict(hits=0, misses=0))
            if key in cache:
                counts['hits'] += 1
                cache.move_to_end(key)
                value, flat = cache[key]
                return value.copy() if flat else deepcopy(value)
        result = function(*args, **kwargs)
        with lock:
            counts['misses'] += 1
            # A source index has immutable scalar values. Copy its container,
            # never traverse immutable strings on each hit. Other shapes keep
            # the independent-copy contract and are rejected by the benchmark.
            flat = type(result) is dict and all(type(k) is str and
                (v is None or type(v) in (str, int, float, bool, bytes)) for k,v in result.items())
            cache[key] = (result.copy() if flat else deepcopy(result), flat)
            while len(cache) > 8:
                cache.popitem(last=False)
        return result
    call.cache_clear = cache.clear
    return call


def offer_build_scope(function):
    """Read the runtime setting in the worker that actually builds the offer.

    Python threads do not inherit our ContextVar. Never rely on a context set
    when a runtime is constructed or on the caller's current cache scope.
    """
    @wraps(function)
    def build(runtime, *args, **kwargs):
        config = getattr(runtime, '_config', None) or {}
        with using(config.get(SETTING) is True):
            return function(runtime, *args, **kwargs)
    return build
