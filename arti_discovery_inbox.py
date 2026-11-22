"""Generic, durable discovery ingress for the text-only persona.

The adapter supplies importance; this module knows no game, item or NPC names.
Files are acknowledged only after append-only transcript evidence. State is a
bounded recent context plus counts; full history stays on disk, never in a prompt.
High events coalesce into a pending batch, with a bounded excerpt and exact IDs.
Every omitted detail remains in the transcript and acknowledged source files.
"""
from arti_runtime import ipc
from arti_runtime.time_grounding import event_frame
from arti_runtime.event_framing import DiscoveryText, TIERS, discovery_event
from collections import Counter, deque
import json
from pathlib import Path

from arti_help_bus import MAX_JSON_BYTES
from arti_live7_moments import _decode


class DiscoveryInbox:
    def __init__(self, transcript, *, min_gap_sec=60, history_path=None):
        self.transcript = transcript
        self.min_gap_sec = max(0, float(min_gap_sec))
        self.last_turn_at = None
        self.counts = Counter()
        self.recent = deque(maxlen=8)
        self.pending = []
        self.seen = set()
        self.errors = set()
        if history_path and Path(history_path).is_file():
            history = json.loads(ipc.read_text(history_path, encoding='utf-8'))
            self.counts.update(history['counts'])
            self.recent.extend(history['recent'])
            self.seen.update(history['seen'])
            # Dated context survives; old unvoiced reactions are not new events.
        # A stopped process must not lose acknowledged files whose high-tier
        # reaction was still waiting. Rebuild compact state from our own journal.
        if transcript.path.is_file():
            completed = set()
            for line in ipc.read_text(transcript.path, encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ev") == "discovery_context":
                    event = row["event"]
                    if event["id"] not in self.seen:
                        self.seen.add(event["id"])
                        self.counts[event["importance"]] += event["count"]
                        self.recent.append(event)
                        if event["importance"] == "high":
                            self.pending.append(event)
                elif row.get("ev") == "discovery_delivery" and row.get("status") in ("reacted", "persona_silent"):
                    completed.update(row["event_ids"])
            self.pending = [e for e in self.pending if e["id"] not in completed]

    def snapshot(self):
        """ADR 144: compact ingress history for a farm continuation; no conversation replay."""
        return dict(counts=dict(self.counts), recent=list(self.recent), seen=sorted(self.seen))

    def poll(self, folder):
        if folder is None:
            return
        for path in sorted(Path(folder).glob("ev-*.json")):
            try:
                with ipc.open_shared(path, "rb") as stream:
                    raw = stream.read(MAX_JSON_BYTES + 1)
                data = json.loads(raw)
                # ADR 90: the bounded moment envelope lands here too, so test mode never loses one to a voice gap.
                if not isinstance(data, dict) or data.get("schema") not in ("arti.game_event/1", "arti.stardew_moment/1"):
                    continue
                _decode(raw)  # same source-tag/shape/size/time validation as the existing relay
                event = discovery_event(data, raw)
                identity, tier, count = event["id"], event["importance"], event["count"]
                # IDs are scoped by the adapter; never interpret their contents.
                duplicate = identity in self.seen
                if not duplicate:
                    receipt = self.transcript.write("discovery_context", event=event,
                                          disposition="pending_reaction" if tier == "high" else "silent_context")
                    if isinstance(receipt, dict) and receipt.get("_persisted") is False:
                        return  # retain sources; one denial cannot multiply across the backlog
                    self.seen.add(identity)
                    self.counts[tier] += count
                    self.recent.append(event)
                    if tier == "high":
                        self.pending.append(event)
                # Keep source evidence durable and outside both consumers' globs.
                ipc.replace(path, path.with_suffix(".json.persona-done"))
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                key = (str(path), type(exc).__name__)
                if key not in self.errors:
                    self.errors.add(key)
                    self.transcript.write("discovery_ingress_error", file=path.name,
                                          reason=type(exc).__name__)

    def context(self, current=None):
        if not self.counts:
            return ""
        # Deterministic history compaction, no extra model calls, exact full records on disk.
        # ADR 113: a50 `recent` repeated about 2.1 KB of [KINI]; every row here is the same one current pose.
        recent = [dict(id=e["id"], tier=e["importance"], count=e["count"],
                       **{k: v for k, v in event_frame(e, current or {}).items()
                          if k not in ("current_place", "current_time", "current_observed_ts", "moved_since")})
                  for e in self.recent]
        return json.dumps(dict(counts=dict(self.counts), recent=recent), ensure_ascii=False)

    def take(self, now, *, busy=False):
        if busy or not self.pending or (self.last_turn_at is not None and now-self.last_turn_at < self.min_gap_sec):
            return None
        batch, self.pending = self.pending[:8], self.pending[8:]
        self.last_turn_at = now
        # All IDs are attached to the turn; only explicitly included facts may ground a reply.
        shown = batch
        text = DiscoveryText("[Game] " + " | ".join(e["situation"] for e in shown))
        text.events = batch
        text.shown_ids = [e["id"] for e in shown]
        return text
