"""Viewer reading dwell before ARTI confirms a dialogue/menu page (Saturday 2026-10-24, smoke item 8).

Owner (smoke 2026-10-23): "[recorded remark omitted]". TV, bed, level-up and dialogue
boxes were confirmed the moment they were ready. A page now stays up for a short reading time scaled by
its visible text before the confirming key is sent.

Pure policy + a first-seen clock. Zero I/O and zero input. Callers wait BEFORE acquiring a lease or
writing a request, never while holding one: the time a page was already on screen (typing, provider
latency) counts toward its dwell, so a slow model adds no extra wait.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Tests flip this off (tests/conftest.py) so unrelated suites keep instant fakes; live config keeps it on.
DEFAULT_ENABLED = True
# ui_key kinds / ui_pad contexts that show something worth reading. Cancel/cleanup keys never dwell.
DWELL_KINDS = frozenset({"dialogue", "letter", "question", "npc_question", "level_up", "level_up_profession",
                         "shipping_summary"})
DWELL_CONTEXTS = frozenset({"dialogue", "letter", "question"})
MENU_KINDS = frozenset({"level_up", "level_up_profession", "shipping_summary"})
SEEN_MAX = 64


@dataclass(frozen=True)
class ReadingDwell:
    enabled: bool | None = None  # None: follow DEFAULT_ENABLED at use time (config sets an explicit bool)
    min_s: float = 2.0       # an empty or one-word page
    max_s: float = 6.0       # long pages; keeps day-start well inside its 240 s wall bound (8 actions x 6 s)
    chars_per_s: float = 20.0  # a relaxed viewer reading pace, added on top of min_s
    menu_s: float = 4.0      # level-up / shipping screens publish no text

    @classmethod
    def default(cls) -> "ReadingDwell":
        return cls()

    @property
    def on(self) -> bool:
        return DEFAULT_ENABLED if self.enabled is None else self.enabled

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None) -> "ReadingDwell":
        """``stardew_reading_dwell_enabled`` / ``_min_s`` / ``_max_s`` / ``_chars_per_s`` / ``_menu_s``."""
        cfg = cfg or {}
        enabled = cfg.get("stardew_reading_dwell_enabled")
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("stardew_reading_dwell_enabled must be a bool")
        values = {}
        for name in ("min_s", "max_s", "chars_per_s", "menu_s"):
            raw = cfg.get("stardew_reading_dwell_" + name, getattr(cls, name))
            if type(raw) not in (int, float) or not 0 <= raw <= 30:
                raise ValueError("stardew_reading_dwell_%s must be a number in 0..30" % name)
            values[name] = float(raw)
        if values["chars_per_s"] <= 0 or values["min_s"] > values["max_s"]:
            raise ValueError("stardew_reading_dwell: chars_per_s > 0 and min_s <= max_s")
        return cls(enabled=enabled, **values)

    def seconds(self, text: str | None, *, kind: str | None) -> float:
        """Reading time for one page: min_s + chars / chars_per_s, clamped to [min_s, max_s]."""
        if not self.on or kind not in DWELL_KINDS:
            return 0.0
        chars = len(" ".join(str(text or "").split()))
        if chars == 0 and kind in MENU_KINDS:
            return min(self.max_s, max(self.min_s, self.menu_s))
        return min(self.max_s, max(self.min_s, self.min_s + chars / self.chars_per_s))


def page_text(facts: Mapping | None) -> str:
    """The page the viewer sees: the mod's DialogueBox/letter text plus any answer labels."""
    if not isinstance(facts, Mapping):
        return ""
    interactive = facts.get("interactive") if isinstance(facts.get("interactive"), Mapping) else {}
    pad = facts.get("ui_pad") if isinstance(facts.get("ui_pad"), Mapping) else {}
    key = facts.get("ui_key") if isinstance(facts.get("ui_key"), Mapping) else {}
    texts = [t for t in (interactive.get("visible_text"), pad.get("text")) if isinstance(t, str) and t.strip()]
    body = max(texts, key=len) if texts else ""
    labels = [o.get("label") for o in (key.get("options") or pad.get("options") or ())
              if isinstance(o, Mapping) and isinstance(o.get("label"), str)]
    return " ".join([body, *labels]).strip()


def page_key(facts: Mapping | None) -> tuple | None:
    """(receiver_token, text, ui_key tokens) of the current interactive page, else None.

    The mod's ui_key target tokens hash the page's current string, so two text-less pages of one
    DialogueBox receiver are still two pages (each gets its own reading time)."""
    if not isinstance(facts, Mapping) or not isinstance(facts.get("interactive"), Mapping):
        return None
    receiver = facts["interactive"].get("receiver_token")
    if not isinstance(receiver, str) or not receiver:
        return None
    key = facts.get("ui_key") if isinstance(facts.get("ui_key"), Mapping) else {}
    tokens = tuple(sorted(str(k.get("target_token")) for k in key.get("keys") or () if isinstance(k, Mapping)))
    return receiver, page_text(facts), tokens


class PageClock:
    """First time each page was seen (monotonic seconds); bounded, oldest first out."""

    def __init__(self, limit: int = SEEN_MAX):
        self._seen: OrderedDict = OrderedDict()
        self._limit = limit

    def note(self, key, now: float) -> float:
        if key not in self._seen:
            self._seen[key] = now
            while len(self._seen) > self._limit:
                self._seen.popitem(last=False)
        return self._seen[key]

    def remaining(self, key, need: float, now: float) -> float:
        if key is None or need <= 0:
            return 0.0
        return max(0.0, need - (now - self.note(key, now)))
