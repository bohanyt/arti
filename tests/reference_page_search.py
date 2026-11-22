"""The full preference ladder with fix115A's search reuse disabled.

The integration proof also compares this against the pre-tempo renderer with
fix116A's content policy (d23a4b4e), independent of the optimized implementation.
"""
import json

import arti_stardew_skills as S
from arti_runtime.page_search import ACTIVE


def message_bytes(messages):
    """The exact UTF-8 serialization used by the pins, including outer dictionary order."""
    return json.dumps(messages, ensure_ascii=False).encode('utf-8')


class ReferenceSearch:
    def __init__(self):
        self.layout_count = 0
        self.render_count = 0

    def layout(self, function, offer, options):
        self.layout_count += 1
        return function(offer, **options)

    def bound(self, function, path, value, limit):
        return function(path[-1], value, limit, 'detail_read')

    def diet(self, function, payload, boundary_page, measure, grounding_limit, ceiling):
        return function(payload, boundary_page, measure, grounding_limit, ceiling)

    def size(self, messages, measure):
        return measure(messages)

    def message(self, body, system):
        self.render_count += 1
        return [dict(role='system', content=system), dict(role='user', content=json.dumps(
            body, ensure_ascii=False, separators=(',', ':'), sort_keys=True))]


def reference_messages(request, fits, *, emergency=False, metrics=None):
    search = ReferenceSearch()
    token = ACTIVE.set(search)
    try:
        messages = S.emergency_skill_messages(request, fits) if emergency else S._render_skill_messages(request, fits)
        if metrics is not None:
            metrics.update(layout_count=search.layout_count, render_count=search.render_count,
                           render_rung=S.RENDER_RUNGS[0])
        return messages
    finally:
        ACTIVE.reset(token)
