"""ADR 164: local speech admission only; no game policy, I/O or inference."""
from __future__ import annotations

import math
import re

from arti_live_safety import is_backstage
from arti_wake import is_arti_wake_call


def addressed(text, config):
    names = config.get("asr_address_names")
    if not names:
        return is_arti_wake_call(str(text))
    if isinstance(names, str):
        names = [names]
    return any(re.search(r"(?<!\w)" + re.escape(name.strip()) + r"(?!\w)",
                         str(text), re.I) for name in names if isinstance(name, str) and name.strip())


def normalized(text):
    return " ".join(re.findall(r"\w+", str(text).casefold()))


class Transcript(str):
    """Optional ASR quality survives the existing string API; PTT is unchanged."""
    def __new__(cls, text, segments=()):
        obj = super().__new__(cls, text)
        def values(key):
            return [v for s in segments if isinstance(s, dict)
                    and isinstance(v := s.get(key), (int, float)) and math.isfinite(v)]
        obj.no_speech_prob = max(values("no_speech_prob"), default=None)
        obj.avg_logprob = min(values("avg_logprob"), default=None)
        return obj


class PassiveGate:
    def __init__(self):
        self.previous = ""

    def reject(self, text, config, *, allow_short=False):
        current = normalized(text)
        duplicate = bool(current and current == self.previous)
        self.previous = current
        # A name-addressed owner event must survive even repeated/short calls.
        if addressed(text, config):
            return None
        if duplicate:
            return "duplicate"
        words = current.split()
        if not allow_short and len(words) < int(config.get("asr_pasif_reply_min_words", 3)):
            return "terlalu_pendek"
        if (len(words) > 1 and len(set(words)) == 1) or any(re.fullmatch(r"(.{1,3})\1{2,}", w) for w in words):
            return "repeated_syllables"
        if getattr(text, "no_speech_prob", None) is not None and text.no_speech_prob >= 0.6:
            return "no_speech"
        if getattr(text, "avg_logprob", None) is not None and text.avg_logprob < -1.0:
            return "low_confidence"
        return None


def natural_intent(why):
    """Fail closed: speak only an intact, short first-person model sentence."""
    if not isinstance(why, str) or not 8 <= len(why.strip()) <= 220:
        return None
    text = why.strip()
    if (not re.match(r"^(?:aku|gue|gua|saya|i)\b", text, re.I)
            or re.match(r"^(?:aku|gue|gua|saya|i)\s+(?:lihat|melihat|ngeliat|see|can see|berada|am at)\b", text, re.I)
            or is_backstage(text) or re.search(r"[\[\]{}<>\n]|\b\w+_\w+\b|\b(?:skill|target|objective|verified)\b", text, re.I)):
        return None
    return text


def own_play_expired(trigger, now, pending=False):
    text = trigger.text
    return (pending or now - getattr(text, "created_at", trigger.queued_at) > 20.0)
