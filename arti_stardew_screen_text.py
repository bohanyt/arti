"""Allowlisted game text only; existing ui.text -> existing cycle journal, no new store/transport."""
from collections import OrderedDict
from collections.abc import Mapping
import re
import unicodedata

MAX_TEXT = 2048
MAX_OPTIONS = 8
FAMILIES = frozenset({"dialogue", "tv", "event_dialogue", "letter", "hud", "level_up",
                      "shipping_summary", "ready_check", "menu_label"})
LABELS = {"dialogue": "dialog", "tv": "TV", "event_dialogue": "adegan", "letter": "surat",
          "hud": "HUD", "level_up": "naik level", "shipping_summary": "kiriman",
          "ready_check": "siap tidur", "menu_label": "menu"}
_PRIVATE = re.compile(r"https?://\S+|www\.\S+|@[\w.-]+|[A-Za-z]:[\\/][^\s]+|(?:/home/|/Users/)[^\s]+|"
                      r"(?:sk-[A-Za-z0-9_-]{8,}|(?:api[_-]?key|token|password|secret)\s*[:=]\s*[^\s]+)", re.I)
_TAG = re.compile(r"<[^>]*>|\[(?:STARDEW STATUS|SYSTEM|ASSISTANT|USER)[^\]]*\]", re.I)


def game_text(value, limit=MAX_TEXT):
    if not isinstance(value, str):
        return ""
    value = value[:MAX_TEXT * 4]  # bounded work even on malformed observations
    value = "".join(" " if c.isspace() else c for c in value
                    if c.isspace() or unicodedata.category(c) not in {"Cc", "Cf", "Cs"})
    return " ".join(_TAG.sub(" ", _PRIVATE.sub("[redacted]", value)).split())[:limit]


def page_fields(facts, kind):
    """Never serialize the UI record wholesale: text-input/chat/token fields stay private."""
    if kind not in {"dialogue", "question", "npc_question", "letter"} or not isinstance(facts, Mapping):
        return {}
    from arti_stardew_reading_dwell import page_text
    pad = facts.get("ui_pad") if isinstance(facts.get("ui_pad"), Mapping) else {}
    key = facts.get("ui_key") if isinstance(facts.get("ui_key"), Mapping) else {}
    options = key.get("options") or pad.get("options") or ()
    return {"text": game_text(page_text(facts)),
            "event_place": facts.get("location_id"),
            "options": [game_text(o.get("label"), 64) for o in options[:MAX_OPTIONS] if isinstance(o, Mapping)],
            "text_truncated": len(page_text(facts)) > MAX_TEXT}


class ScreenTextJournal:
    def __init__(self):
        self.seen = OrderedDict()

    def context_rows(self, record, now):
        """Reuse native draw-time text for readable menus, never chat/text entry.

        These strings are already captured by GenericUiRenderedText; this adds
        no native capture and never enumerates a quest log or unseen pages.
        """
        if (record is None or record.domain != "ui.context" or not record.authoritative
                or not record.available or record.stale or record.requires_resync):
            return []
        from arti_stardew_ui_pad import baca_ui_pad
        view = baca_ui_pad(record.facts, age_s=0)
        menu = getattr(view, "menu_type", None)
        allowed = {"Billboard", "QuestLog", "SpecialOrdersBoard", "ShopMenu", "GameMenu",
                   "CraftingPage", "SkillsPage", "CollectionsPage", "JunimoNoteMenu"}
        if menu not in allowed:
            return []
        lines = record.facts.get("rendered_ui_text")
        if not isinstance(lines, (list, tuple)):
            return []
        text = game_text("\n".join(game_text(s) for s in lines[:64] if isinstance(s, str)))
        if not text:
            return []
        identity = ("rendered", record.player_id, record.screen_id, record.session_id,
                    getattr(view, "receiver_token", None), menu, text)
        if identity in self.seen:
            return []
        self.seen[identity] = True
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        return [dict(ev="screen_text", at=now, family="menu_label", text=text, speaker="",
                     source="rendered_ui_text:"+menu, text_truncated=len(text) >= MAX_TEXT,
                     player_id=record.player_id, screen_id=record.screen_id,
                     observation_session=record.session_id)]

    def rows(self, record, event, now):
        if (record is None or record.domain != "ui.text" or not record.authoritative or not record.available
                or record.stale or record.requires_resync):
            return []
        binding = record.facts.get("binding")
        if (not isinstance(binding, Mapping) or binding.get("player_id") != record.player_id
                or binding.get("screen_id") != record.screen_id):
            return []
        ticks = binding.get("capture_ticks")
        if type(ticks) is not int or ticks < 0:
            return []
        # Only changed facts from an accepted envelope, never arbitrary fields or archived replay.
        body = event.get("payload", {}).get("body", {})
        facts = body.get("facts", {}) if event.get("payload", {}).get("kind") == "snapshot" else body.get("upserts", {})
        rows = []
        for name, occ in facts.items():
            if not isinstance(name, str) or not name.startswith("occ:") or not isinstance(occ, Mapping):
                continue
            family = occ.get("family")
            if family not in FAMILIES or occ.get("replayed") is not False:
                continue
            last, seq, rev = occ.get("last_capture_ticks"), occ.get("occurrence_seq"), occ.get("text_revision")
            if (type(last) is not int or type(seq) is not int or type(rev) is not int
                    or not 0 <= ticks - last <= 180 * 60):
                continue
            identity = (record.player_id, record.screen_id, record.session_id, seq, rev)
            if identity in self.seen:
                continue
            text = game_text(occ.get("text"))
            options = [game_text(o.get("label"), 64) for o in (occ.get("options") or ())[:MAX_OPTIONS]
                       if isinstance(o, Mapping)]
            labels = [game_text(s, 64) for s in (occ.get("labels") or ())[:8] if isinstance(s, str)]
            if not text and not any(options) and not any(labels):
                continue
            self.seen[identity] = True
            while len(self.seen) > 256:
                self.seen.popitem(last=False)
            rows.append({"ev": "screen_text", "at": now - (ticks - last) / 60.0,
                         "family": family, "text": text, "options": options, "labels": labels,
                         "speaker": game_text(occ.get("speaker"), 64),
                         "source": game_text(occ.get("source"), 96),
                         "text_truncated": occ.get("complete") is not True or len(occ.get("text", "")) > MAX_TEXT,
                         "player_id": record.player_id, "screen_id": record.screen_id,
                         "observation_session": record.session_id, "occurrence_seq": seq, "text_revision": rev,
                         "capture_ticks": last})
        return rows


def status_text(row):
    if row.get("ev") == "screen_text":
        family = row.get("family")
        if family not in FAMILIES:
            return ""
        label = LABELS[family]
    elif row.get("ev") == "ui_reading_dwell" and row.get("kind") in {"dialogue", "question", "npc_question", "letter"}:
        label = {"question": "pertanyaan", "npc_question": "pertanyaan NPC", "letter": "surat", "dialogue": "dialog"}[row["kind"]]
    else:
        return ""
    text = game_text(row.get("text"), 400)
    options = [game_text(o, 64) for o in (row.get("options") or ())[:MAX_OPTIONS] if isinstance(o, str)]
    labels = [game_text(o, 64) for o in (row.get("labels") or ())[:8] if isinstance(o, str)]
    detail = " | ".join(t for t in [text, *options, *labels] if t)
    if not detail:
        return ""
    from arti_runtime.place_names import source_place
    from arti_runtime.fact_names import text as named_text
    game = row.get("game") if isinstance(row.get("game"), Mapping) else {}
    place = row.get("place_name") or source_place(row.get("event_place") or game.get("location"))
    return (label + ' di ' + place + ': "' + named_text(detail[:440]) + '"' + (" (teks terpotong)" if row.get("text_truncated") else ""))
