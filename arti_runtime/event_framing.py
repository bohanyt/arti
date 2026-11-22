"""Shared discovery facts and trigger text for rig and lossy live ingress (ADR 110)."""
import hashlib
import re

from arti_runtime.time_grounding import event_line

_TAGS = re.compile(r"\[[^\]]*\]|<[^>]*>")
TIERS = ("high", "low", "flood")
REACTIVE_PLAY_INSTRUCTION = "Ceritakan dengan satu kalimat pendek apa yang barusan terjadi di game, dari sudut pandangmu sendiri. Pakai hanya fakta di atas."
REACTIVE_INTENT_INSTRUCTION = "Bilang dengan satu kalimat pendek kamu mau ke mana atau mau ngapain, dari sudut pandangmu sendiri."
DISCOVERY_INSTRUCTION = (
    "\nKabar kejadian lampau dari permainanmu, bukan posisi sekarang. Kamu sendiri menilai pentingnya; "
    "boleh [diam]. Kalau bereaksi, seperti teman/streamer, singkat dan alami. "
    "Jangan mendeskripsikan apa yang kamu lihat. Fakta hanya dari kejadian "
    "yang disertakan dalam giliran ini; jangan mengarang rincian, sebab, "
    "hadiah, tindakan, atau kemampuan yang belum terbukti. Unknown tetap unknown."
)


def safe_fact(value, limit=500):
    return " ".join(_TAGS.sub(" ", str(value or "")).replace("[", " ").replace("]", " ").split())[:limit]


def discovery_event(data, raw):
    """Normalize an already wire-validated envelope, identically to the rig inbox."""
    tier = data.get("importance", "high")
    if tier not in TIERS:
        tier = "high"
    identity = str(data.get("id") or hashlib.sha256(raw).hexdigest())
    facts = data.get("facts") or {}
    if not isinstance(facts, dict):
        raise ValueError("facts")
    count = data.get("count", 1)
    if type(count) is not int or not 1 <= count <= 1000000:
        raise ValueError("count")
    return dict(id=identity, importance=tier, count=count, ts=data["ts"],
                game_time=data.get("game_time"), event_time=data.get("event_time") or facts.get("event_time") or data.get("game_time"),
                event_place=data.get("event_place") or facts.get("event_place"), facts=facts,
                situation=safe_fact(data["situation"]))


class MomentText(str):
    """Internal source marker, preserved by both voice queues for log redaction."""

    _live7_moment = True


class DiscoveryText(MomentText):
    """Reuses the existing react-rather-than-narrate prompt branch."""
    _generic_discovery = True

    def grounded(self, current, native=None):
        text = DiscoveryText("[Game] " + " | ".join(event_line(e, current, native) for e in self.events))
        text.events, text.shown_ids = self.events, self.shown_ids
        return text


class OwnPlayText(MomentText):
    """Passing thought/outcome, separate from owner and chat events."""
    _own_play = True

    def __new__(cls, text, *, created_at, direct_speech=None, facts=None):
        if facts is not None:
            # Own-play remains a MomentText: this is the action's receipt, not a discovery.
            lines = ["niat: " + safe_fact(facts["niat"])] if facts.get("niat") else []
            lines.extend(safe_fact(line) for line in facts.get("outcome", ()) if line)
            if facts.get("skill"):
                lines.append("skill: " + safe_fact(facts["skill"]) + "; arg: " + safe_fact(facts.get("arg")))
            instruction = REACTIVE_INTENT_INSTRUCTION if facts.get("kind") == "own_intent" else REACTIVE_PLAY_INSTRUCTION
            text = "[Game]\n" + "\n".join(lines) + "\n" + instruction
            direct_speech = None
        obj = super().__new__(cls, text)
        obj.created_at = created_at
        obj.direct_speech = direct_speech
        if facts is not None:
            obj.play_facts = facts
            obj.play_instruction = instruction
        return obj
