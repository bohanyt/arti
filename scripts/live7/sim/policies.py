"""Deterministic stub model policies. A policy maps the rendered offer (``runtime.View``) to a decision.

Decisions: ``("act", name)``, ``("act", name, arg)``, ``("finish", "goal")``, ``("ask_owner",)`` or
``("invalid",)`` (an unoffered pick the real ``live7.choice`` must refuse). Policies only ever see what the
production render left in the request, exactly like the hosted model.
"""
from __future__ import annotations


def act(name, arg=None):
    return ("act", name, arg)


FINISH = ("finish", "goal")
ASK = ("ask_owner",)
INVALID = ("invalid",)


class Prefer:
    """Ordered rules ``(spec, when)``; the first offered spec whose condition holds wins.

    ``spec`` is ``"name"``, ``"name:arg"``, ``"name:*"`` (first listed arg) or a callable(view) returning a
    decision/None. ``when`` is an optional predicate on the view. ``otherwise`` is used when nothing matches.
    """

    def __init__(self, *rules, otherwise=ASK):
        self.rules = [r if isinstance(r, tuple) else (r, None) for r in rules]
        self.otherwise = otherwise

    def __call__(self, view):
        for spec, when in self.rules:
            if when is not None and not when(view):
                continue
            decision = spec(view) if callable(spec) else self._pick(view, spec)
            if decision is not None:
                return decision
        return self.otherwise(view) if callable(self.otherwise) else self.otherwise

    @staticmethod
    def _pick(view, spec):
        name, _, arg = spec.partition(":")
        if not view.has(name):
            return None
        if not arg:
            return act(name)
        if arg == "*":
            return act(name, view.args(name)[0]) if view.args(name) else act(name)
        return act(name, arg) if view.has(name, arg) else None


def at(*locations):
    return lambda view: view.location in locations


def after(hhmm):
    return lambda view: view.clock >= hhmm


def before(hhmm):
    return lambda view: view.clock < hhmm


def both(*preds):
    return lambda view: all(p(view) for p in preds)


class Once:
    """Wrap a rule so it fires at most once (e.g. a one-off model ``finish``)."""

    def __init__(self, decision):
        self.decision, self.used = decision, False

    def __call__(self, view):
        if self.used:
            return None
        self.used = True
        return self.decision


class Replay:
    """Replay recorded rehearsal choices in order while each is still offered at the recorded place.

    ``steps`` are fixture rows ``{"location", "choice"}``; a step whose place differs, or whose
    choice is no longer offered, is skipped and the ``fallback`` policy decides instead.
    """

    def __init__(self, steps, fallback):
        self.steps, self.fallback, self.used = list(steps), fallback, []

    def __call__(self, view):
        while self.steps:
            step = self.steps[0]
            if step["location"] != view.location:
                break
            self.steps.pop(0)
            choice = tuple(step["choice"])
            arg = choice[2] if len(choice) > 2 else None
            if choice[0] != "act" or (view.has(choice[1], arg)
                    and (arg is not None or len(view.args(choice[1])) <= 1)):
                self.used.append(step)
                return choice
        return self.fallback(view)


def gardener():
    """A gardening stub that prefers daytime work; production bed remains selectable at any hour."""
    seeds = lambda v: (v.day.get("crops") or {}).get("seeds", 0)
    fortune = Once(act("cek_pagi", "fortune"))
    return Prefer(
        ("tidur", lambda v: v.clock >= 1800 or v.energy < 60),
        (fortune, lambda v: v.has("cek_pagi", "fortune")), "tanam", "siram",
        ("cangkul", lambda v: seeds(v) and not (v.day.get("crops") or {}).get("hoed_tiles")),
        ("beli:*", lambda v: not seeds(v)),
        ("pergi_ke:seed shop", lambda v: not seeds(v)),
        ("pergi_ke:farm", at("Cabin", "SeedShop")),
        ("pergi_ke:home", after(1800)),
        "tebang", "pecah_batu", "bersihkan:*", "pergi_ke:home", otherwise=ASK)
