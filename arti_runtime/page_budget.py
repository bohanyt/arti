"""Per-trial decision presentation budgets; no process-global mutations (ADR 209)."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps


@dataclass(frozen=True)
class PageBudget:
    scale: int = 1
    guard_bytes: int = 13000
    page_limit_bytes: int = 12000

    @classmethod
    def from_config(cls, config):
        values = [config.get(key, default) for key, default in (
            ('stardew_page_budget_scale', 1), ('stardew_render_guard_bytes', 13000),
            ('stardew_page_limit_bytes', 12000))]
        for key, value in zip(('stardew_page_budget_scale', 'stardew_render_guard_bytes',
                               'stardew_page_limit_bytes'), values):
            if type(value) is not int or value < 1:
                raise ValueError(key)
        return cls(*values)

    @property
    def guard(self):
        return self.guard_bytes * self.scale

    @property
    def page_limit(self):
        return self.page_limit_bytes * self.scale

    def telemetry(self):
        return dict(render_guard_bytes=self.guard, page_limit_bytes=self.page_limit,
                    page_budget_scale=self.scale)


DEFAULT = PageBudget()
ACTIVE = ContextVar('decision_page_budget', default=DEFAULT)


def for_request(request):
    return getattr(request, 'decision_page_budget', None) or DEFAULT


def scaled(value):
    return value * ACTIVE.get().scale


def sizes(values):
    return tuple(scaled(value) for value in values)


@contextmanager
def using(budget):
    token = ACTIVE.set(budget)
    try:
        yield
    finally:
        ACTIVE.reset(token)


def request_scope(function):
    """Renderer/provider methods share the immutable request's budget, including retries."""
    @wraps(function)
    def call(*args, **kwargs):
        request = kwargs.get('request', args[0] if args else None)
        if not hasattr(request, 'skill_offer') and len(args) > 1:
            request = args[1]
        with using(for_request(request)):
            return function(*args, **kwargs)
    return call


def fits_page(messages, *, budget=None):
    return len(messages[1]['content'].encode('utf-8')) <= (budget or ACTIVE.get()).page_limit
