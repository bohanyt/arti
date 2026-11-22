"""One exact render search's pure-work memo; no cross-scene/page reuse."""
from contextvars import ContextVar
from copy import deepcopy
import json


ACTIVE = ContextVar("skill_page_search", default=None)


class PageSearch:
    def __init__(self):
        self.layouts = {}
        self.bounds = {}
        self.sizes = {}
        self.diets = {}
        self.immutable = {}
        self.encoded_messages = {}
        self.layout_count = 0
        self.render_count = 0

    def layout(self, function, offer, options):
        # Preserve insertion order and JSON types (True != 1 for page bytes).
        key = tuple(options.items()), json.dumps(offer, ensure_ascii=True, separators=(",", ":"))
        if key in self.layouts:
            return deepcopy(self.layouts[key])
        result = function(offer, **options)
        self.layout_count += 1
        self.layouts[key] = deepcopy(result)
        return result

    def bound(self, function, path, value, limit):
        key = path, limit, json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        if key in self.bounds:
            return deepcopy(self.bounds[key])
        result = function(path[-1], value, limit, "detail_read")
        self.bounds[key] = deepcopy(result)
        return result

    def message(self, body, system):
        key = id(body), system
        if self.immutable.get(id(body)) is body and key in self.encoded_messages:
            return self.encoded_messages[key]
        result = [{"role": "system", "content": system}, {"role": "user", "content":
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)}]
        self.render_count += 1
        if self.immutable.get(id(body)) is body:
            self.encoded_messages[key] = result
        return result

    def diet(self, function, payload, boundary_page, measure, grounding_limit, ceiling):
        key = tuple(boundary_page or ()), ceiling, json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if key in self.diets:
            return self.diets[key]
        result = deepcopy(function(payload, boundary_page, measure, grounding_limit, ceiling))
        self.diets[key] = result
        self.immutable[id(result)] = result
        return result

    def size(self, messages, measure):
        key = tuple((m["role"], m["content"]) for m in messages)
        if key not in self.sizes:
            self.sizes[key] = measure(messages)
        return self.sizes[key]
