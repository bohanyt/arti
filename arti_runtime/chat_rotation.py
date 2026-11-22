"""Fair viewer-chat rotation (VOICE_CHAT_ANALYSIS change 5).

When CONFIG["mic_priority_mark"] is on, the flat per-viewer cooldown and the casual-chat
throttle are replaced by a bounded rotation: first-time and rare chatters first; a heavy
chatter's consecutive lines merge into one reply; greetings, thanks, goodbyes, hearts and
answers to her last question are always acknowledged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RARE_TURNS = 1
MERGE_CAP = 8
ANSWER_MAX_WORDS = 40

_GREET = re.compile(
    r"(?<!\w)(?:halo|hallo|hai|hey|hi|hello|pagi|siang|sore|malam|yo|oy)(?!\w)", re.I,
)
_THANKS = re.compile(
    r"(?<!\w)(?:makasih|thanks|thx|ty|terima\s*kasih|thank\s*you)(?!\w)", re.I,
)
_BYE = re.compile(
    r"(?<!\w)(?:bye|dadah|daah|goodbye|good\s*night|selamat\s*malam|pulang|gf+|gn+|see\s*ya|sampai\s*jumpa)(?!\w)",
    re.I,
)
_HEART = re.compile(r"[❤❤️💕💖💗💙💚💛🧡💜🤍🖤🤎😍🥰]|(?<!\w)(?:<3|love)(?!\w)", re.I)


def always_ack(text: str, last_reply: str = "") -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    if _GREET.search(raw) or _THANKS.search(raw) or _BYE.search(raw) or _HEART.search(raw):
        return True
    reply = str(last_reply or "").strip()
    if reply.endswith("?") and 0 < len(raw.split()) <= ANSWER_MAX_WORDS:
        return True
    return False


def ack_template(text: str) -> str:
    raw = str(text or "").strip()
    if _THANKS.search(raw):
        return "Sama-sama!"
    if _BYE.search(raw):
        return "Dadah, hati-hati ya!"
    if _HEART.search(raw):
        return "Hehe, makasih!"
    if _GREET.search(raw):
        return "Halo juga!"
    return "Oke!"


@dataclass
class Choice:
    admit: bool
    merge: bool
    rank: int
    template: str = ""
    reason: str = ""


@dataclass
class Rotator:
    admitted: dict = field(default_factory=dict)
    last_viewer: str | None = None

    def choose(self, viewer: str, text: str, *, baru: bool, wake: bool, last_reply: str = "") -> Choice:
        viewer = str(viewer or "")
        count = int(self.admitted.get(viewer, 0))
        ack = always_ack(text, last_reply)
        consecutive = self.last_viewer == viewer and count >= RARE_TURNS
        if ack:
            return Choice(True, False, -1 if (baru or count == 0) else 0, "", "ack")
        if wake or baru or count == 0:
            return Choice(True, False, -1, "", "first")
        if consecutive:
            return Choice(True, True, 1, ack_template(text) if ack else "", "merge")
        return Choice(True, False, 1, "", "heavy")

    def note(self, viewer: str) -> None:
        viewer = str(viewer or "")
        self.admitted[viewer] = int(self.admitted.get(viewer, 0)) + 1
        self.last_viewer = viewer
