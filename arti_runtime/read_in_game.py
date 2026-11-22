"""ADR 172: native journal text -> attention candidates, through the existing relay.

No capture, inference or narration policy: only text the native adapter already
recorded. Old text remains historical evidence but cannot become fresh speech.
"""
from collections import OrderedDict
import json

from arti_stardew_screen_text import FAMILIES, game_text


class ReadInGameText(str):
    def __new__(cls, *, family, text, speaker, source, created_at, options=(), labels=(), truncated=False):
        facts = dict(witness="own game screen", family=family, speaker=speaker or "UNKNOWN",
                     source=source or "native game text", text=game_text(text),
                     options=[game_text(v, 64) for v in options[:8]], labels=[game_text(v, 64) for v in labels[:8]],
                     observed_at=created_at, text_truncated=bool(truncated))
        obj = super().__new__(cls, "[Read in game] "+json.dumps(facts, ensure_ascii=False))
        obj.created_at, obj.source_label, obj.screen_text = created_at, facts["source"], facts["text"]
        obj.speaker = facts["speaker"]
        obj.family = family
        obj.game_salience = 0.65
        obj.attention_facts = ("Native text witnessed on her own game screen, at the stated time. "
                               "This is context for a possible reaction, not a request to read everything aloud. "
                               "It is available to quote when asked; missing speaker is unknown.")
        return obj


class ReadInGameRelay:
    def __init__(self):
        self.pending = []
        self.seen = OrderedDict()

    def note(self, row):
        ev = row.get("ev")
        family = row.get("family") if ev == "screen_text" else row.get("kind")
        if ev not in {"screen_text", "ui_reading_dwell"} or family not in FAMILIES:
            return
        text = game_text(row.get("text"))
        options, labels = row.get("options") or [], row.get("labels") or []
        if not text and not options and not labels:
            return
        at = row.get("at")
        if type(at) not in (int, float):
            return  # Unknown capture time is never relabelled as a live presentation.
        identity = (ev, row.get("observation_session"), row.get("occurrence_seq"),
                    row.get("text_revision"), at, text)
        if identity in self.seen:
            return
        self.seen[identity] = True
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        self.pending.append(ReadInGameText(family=family, text=text,
            speaker=game_text(row.get("speaker"), 64), source=game_text(row.get("source"), 96) or family,
            created_at=at, options=options, labels=labels, truncated=row.get("text_truncated", False)))

    def deliver(self, enqueue):
        while self.pending:
            # Bridge ingress records it to session history even if too old to speak.
            enqueue(self.pending[0], trigger_type="read_in_game")
            self.pending.pop(0)
