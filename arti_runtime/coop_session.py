"""Session facts that outlive provider threads and short conversation history (ADR 168)."""
import re

from arti_runtime.event_framing import safe_fact

SESSION_NOTE_MAX = 6
SESSION_NOTE_CHARS = 160


def owner_name(config):
    return safe_fact(config.get("streamer_name"), 80) or "Bohan"


class CoopSession:
    def __init__(self, config):
        self.enabled = config.get("livepath_mode") == "mabar"
        self.name = owner_name(config)
        limit = max(6, int(config.get("live7_session_facts_limit", 6)))
        self.facts = tuple(safe_fact(f, 300) for f in (config.get("live7_session_facts") or ())[:limit])
        self.corrections = []
        self.notes = []  # model-chosen [FAKTA_SESI] facts; in-process only

    def note_owner(self, text):
        if not self.enabled:
            return
        # Preserve the owner's actual correction, as a fact, even when this
        # line is history-only or the provider resets its thread next turn.
        if re.search(r"\b(?:jangan|jgn|nggak usah|gak usah|ga usah|stop)\b.{0,70}\b(?:bos|boss|owner|host)\b", str(text), re.I):
            fact = safe_fact(text, 300)
            if fact not in self.corrections:
                self.corrections = (self.corrections + [fact])[-4:]

    def remember(self, fact):
        """Store one model-chosen session fact. Newest wins; case-insensitive de-dupe."""
        fact = safe_fact(fact, SESSION_NOTE_CHARS)
        if not fact:
            return
        key = fact.casefold()
        self.notes = [n for n in self.notes if n.casefold() != key]
        self.notes.append(fact)
        self.notes = self.notes[-SESSION_NOTE_MAX:]

    def clear_notes(self):
        self.notes = []

    def clear_for_host_left(self):
        """Clear co-op session block, corrections and notes at once when the host leaves."""
        self.enabled = False
        self.facts = ()
        self.corrections = []
        self.notes = []

    def block(self):
        notes = (" Fakta sesi ini: %s." % " | ".join(self.notes)) if self.notes else ""
        if not self.enabled:
            return ("\n[STARDEW STATUS]%s\n" % notes) if notes else ""
        facts = " ".join(f for f in self.facts if f)
        correction = (" Koreksi sapaan dari %s: %s." % (self.name, " | ".join(self.corrections))
                      if self.corrections else "")
        return ("\n[STARDEW STATUS] Sesi co-op: nama teman mainku %s; sapaan namanya %s. %s%s%s\n"
                % (self.name, self.name, facts, correction, notes))
