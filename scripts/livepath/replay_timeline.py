"""Replay a bridge log on a virtual clock, with no devices, providers or sleeps.

Usage (from repo root, no game or OBS processes)::

    python scripts/livepath/replay_timeline.py --help

The observed gate decisions are inputs, not recomputed ASR/model decisions.
Classic admission/dequeue/expiry/supersession execute current bridge definitions;
desk selection
uses attention_bridge with its documented deterministic unavailable-selector
fallback. Turn costs, including silent/error turns, come from the input logs.
Generation, synth delay and speech lengths are sampled with a fixed seed.
Observed metrics are reported separately: they never replace simulated metrics.
No semantic correctness claim is made: 'answered' means its own or merged trigger voiced.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from contextlib import ExitStack, redirect_stdout
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import heapq
import io
import json
import math
from pathlib import Path
import random
import re
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import arti_voice_queue as Q
import arti_live_safety as safety
from arti_runtime import attention_bridge as AB, attention_desk as AD
from scripts.livepath import replay_policy
from arti_runtime.event_framing import MomentText, OwnPlayText
from arti_runtime.streamer_voice import addressed

WIB = timezone(timedelta(hours=7))
STAMP = re.compile(r"^(\d\d:\d\d:\d\d\.\d{3})\s?(.*)$")
QUEUE = re.compile(r'\[Queue\] Trigger \(([^)]+)\)(?: depth=\d+)?: "(.*)"$')
HISTORY = re.compile(r"\[History Recorded\] \[[^]]+\] \[Streamer\] (.*)$")
CHAT = re.compile(r"💬 \[YT Chat\] @([^:]+): (.*)$")
OWNER_KINDS = {"mic", "mic_pasif", "ptt", "wake_word", "mc_chat"}
GAME_KINDS = {"game", "moment", "status", "own_play", "read_in_game", "scene_ended"}


def percentile(values, fraction):
    rows = sorted(values)
    if not rows:
        return None
    index = (len(rows)-1)*fraction
    lo = int(index)
    return rows[lo] + (rows[min(lo+1, len(rows)-1)]-rows[lo])*(index-lo)


@dataclass
class Event:
    at: float
    type: str
    text: str = ""
    kind: str = ""
    viewer: str | None = None
    bot: bool = False
    gate: str = "unclassified"
    turn_id: str | None = None
    named: bool = False
    queued_at: float | None = None
    line: int = 0
    data: dict = field(default_factory=dict)

    @property
    def question(self):
        return self.type == "owner" and self.data.get('should_answer', "?" in self.text or self.named)


@dataclass
class Turn:
    id: str
    kind: str
    start: float
    source: str = "unknown"
    reply_at: float | None = None
    chars: int = 0
    voiced_at: float | None = None
    audio_start: float | None = None
    end: float | None = None
    synth: float = 0
    play: float = 0
    cancelled: bool = False
    silent: bool = False
    failed: bool = False
    named: bool = False
    reply_text: str = ""
    stages: dict = field(default_factory=dict)

    @property
    def generation(self):
        return max(0, (self.reply_at if self.reply_at is not None else self.end or self.start)-self.start)


@dataclass
class Session:
    path: Path
    start: float
    end: float
    events: list[Event]
    turns: list[Turn]
    counters: dict
    ready: float | None = None


@dataclass(frozen=True)
class ReplayTrigger:
    """Fake bridge value object; attention_bridge uses dataclasses.replace."""
    text: str
    trigger_type: str
    viewer_name: str | None = None
    queued_at: float = 0
    turn_id: str | None = None
    stardew_voice_epoch: int | None = None
    owner_marked: bool = True
    rotation_rank: int = 0
    owner_reply_required: bool = False
    ptt_generation: int | None = None
    host_story_retry: bool = False


def read_session(path, session_date=None):
    """Parse once, joining queue witnesses to their exact ingress and turn.

    The transcript counter restarts in each bridge process; even within C the
    initial chat has t-0001 before the first queued mic reuses t-0001. Ordered
    start/end records, rather than a dict keyed by turn_id, preserve that fact.
    """
    path = Path(path)
    found = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
    if not found and session_date is None:
        raise ValueError('log filename needs YYYY-MM-DD or --session-date')
    day = session_date or date.fromisoformat(found[0])
    midnight = datetime.combine(day, datetime.min.time(), WIB).timestamp()
    events, turns, counters = [], [], Counter()
    owner = chat = pending = current = latency_turn = last_game = None
    previous = start = end = ready = None
    for number, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
        match = STAMP.match(line)
        if not match:
            continue
        h, m, s = map(float, match[1].split(":"))
        at = midnight + h*3600+m*60+s
        if previous is not None and at < previous-43200:
            midnight += 86400
            at += 86400
        previous, end = at, at
        if start is None:
            start = at
        body = match[2]
        if '[Mode]' in body and ready is None:
            ready = at
        if m := HISTORY.search(body):
            owner = Event(at, "owner", m[1], "mic_pasif", line=number)
            owner.named = addressed(owner.text, {})
            events.append(owner)
        elif '[ASR Info] Pasif:' in body and owner is not None:
            owner.gate = "passive"
        elif (m := re.search(r"\[ASR Pasif\] History only \(([^)]+)\)", body)) and owner is not None:
            owner.gate = "history_only:" + m[1]
        elif m := CHAT.search(body):
            chat = Event(at, "chat", m[2], "yt_chat", viewer=m[1], line=number)
            # Bot metadata is not printed by the 11-11 reader. Explicit markers
            # in synthetic/exported logs survive; absent markers mean unknown.
            chat.bot = bool(re.search(r"\[bot\]", m[1], re.I))
            chat.data["bot_flag_logged"] = chat.bot
            events.append(chat)
        if chat is not None and '[History Recorded]' in body and '(YouTube)]' in body:
            chat.data['history_recorded'] = True
        if chat is not None and '[YT Chat Info] Perintah bot dari' in body:
            chat.gate = 'bot_command'
        if chat is not None and re.search(r'\[YT Chat Info\].*(?:[Bb]ot|[Ll]ayanan bot)', body) and 'Perintah bot' not in body:
            chat.bot = True
            chat.data['bot_flag_logged'] = True
        if m := QUEUE.search(body):
            kind, text = m.groups()
            if kind in OWNER_KINDS:
                if owner is None or at-owner.at > 1 or not (owner.text.startswith(text) or text.startswith(owner.text[:100])):
                    owner = Event(at, "owner", text, kind, line=number)
                    owner.named = addressed(text, {})
                    events.append(owner)
                pending = owner
                pending.gate = "name_addressed" if pending.named else "passive"
            elif kind == "yt_chat":
                pending = Event(at, "enqueue", text, kind, viewer=chat.viewer if chat else None, line=number)
                if chat is not None:
                    pending.data['chat_line'] = chat.line
                    pending.data['chat_text'] = chat.text
                    chat.data['queue_witness'] = True
                events.append(pending)
            else:
                pending = Event(at, "enqueue", text, kind, line=number)
                events.append(pending)
            pending.kind, pending.queued_at = kind, at
            if kind == 'game':
                last_game = pending
        if '[HostStory] voiced lines=' in body and last_game is not None and at-last_game.at < 1:
            # The log's "voiced" here is queue acceptance, not audio. The real
            # HostStoryRelay.text() always returns a held (300s) MomentText.
            last_game.data.update(held=True, host_story=True)
        if '[Livepath] ' in body:
            try:
                row = json.loads(body.split('[Livepath] ', 1)[1])
            except ValueError:
                counters["malformed_livepath"] += 1
                continue
            ev = row.get("event")
            if ev == "queued" and pending is not None:
                pending.turn_id = row.get("turn_id")
                pending.named = bool(row.get("name_addressed", pending.named))
                if pending.type == "owner" and pending.named:
                    pending.gate = "name_addressed"
                pending.data["witness_kind"] = row.get("kind")
                pending = None
            elif ev == "start":
                current = Turn(row["turn_id"], row["kind"], at)
                turns.append(current)
            elif ev == "voiced" and current is not None:
                current.voiced_at, current.source = at, row.get("source", "unknown")
                latency_turn = current
            elif ev == "end" and current is not None:
                current.end = at
                current = None
            elif ev == 'attention_voiced':
                if latency_turn is not None:
                    latency_turn.stages['merged_turn_ids'] = row.get('deferred_turn_ids', [])
        if current is not None:
            if m := re.search(r'Arti menjawab: "(.*)"$', body):
                current.reply_at, current.chars = at, len(m[1])
                current.reply_text = m[1]
            if '[Codex] gpt-' in body:
                current.source = "codex"
            elif '[Groq API] Mengirim' in body:
                current.source = "groq"
            if 'tidak diucapkan:' in body:
                current.cancelled = True
            if 'Arti memilih diam' in body:
                current.silent = True
            if re.search(r'\b413\b|\[Brain Error\]', body):
                current.failed = True
        if '[Latency]' in body and latency_turn is not None:
            stages = {k: float(v)/1000 for k, v in re.findall(r"(\w+)=(\d+)ms", body)}
            latency_turn.stages.update(stages)
            latency_turn.play = stages.get("tts_play", 0)
            latency_turn.synth = stages.get("tts_synth", 0)
            if latency_turn.play:
                latency_turn.audio_start = latency_turn.voiced_at-latency_turn.play
            latency_turn = None
        if m := re.search(r"Kedaluwarsa .*?: (\d+) dibuang", body):
            counters["expired"] += int(m[1])
        if '[Queue] Drop stale ' in body:
            counters['selection_expired'] += 1
        if '[Queue] Dibuang:' in body:
            for kind, count in re.findall(r"(\w+)=(\d+)", body):
                counters["replaced_"+kind] += int(count)
        if 'Balasan yt_chat tidak diucapkan:' in body:
            counters["chat_cancelled"] += 1
        if '[ASR] Mendengar suara' in body:
            events.append(Event(at, "vad", data={"speaking": True}))
        if '[ASR] Selesai bicara' in body:
            events.append(Event(at, "vad", data={"speaking": False}))
        if m := re.search(r'\[Video\] Playback hold ([\d.]+)s', body):
            events.append(Event(at, 'media_hold', data={'seconds': float(m[1])}))
    if start is None:
        raise ValueError(f"no timestamped lines: {path}")
    for turn in turns:
        matching = [e for e in events if e.turn_id == turn.id and e.kind == turn.kind
                    and e.queued_at is not None and e.queued_at <= turn.start]
        if matching:
            turn.named = max(matching, key=lambda e: e.queued_at).named
    return Session(path, start, end, events, turns, dict(counters), ready)


def read_journals(paths):
    """Native game facts only; untimestamped rows inherit the preceding at.

    These facts inform desk salience, never create imaginary voiced comments.
    Voice events come from the bridge's actual queued game/own-play witnesses.
    """
    rows = []
    for path in paths:
        at = None
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            row = json.loads(line)
            if type(row.get("at")) in (float, int):
                at = float(row["at"])
            if at is not None:
                rows.append((at, row))
    return sorted(rows, key=lambda r: r[0])


def enrich_game_events(sessions, journals):
    """Recover redacted moment metadata, using only witnessed journal emits."""
    emitted = [(row.get('ts', at), row) for at, row in journals if row.get('ev') == 'live7_moment_emitted']
    for session in sessions:
        for event in session.events:
            if event.kind != 'game' or event.data.get('host_story'):
                continue
            matches = [(at, row) for at, row in emitted if 0 <= event.at-at <= 300]
            if matches:
                at, row = max(matches, key=lambda r: r[0])
                event.data.update(game_kind=row.get('kind'), held=row.get('kind') == 'bed')


@dataclass(frozen=True)
class Cost:
    generation: float
    synth: float
    play: float
    speaks: bool = True
    cleanup: float = 0.45
    reply_text: str = field(default='', repr=False)


@dataclass(frozen=True)
class GenerationDistribution:
    samples: tuple[float, ...]
    label: str
    path: Path
    sha256: str

    @classmethod
    def load(cls, path):
        path = Path(path)
        payload = path.read_bytes()
        row = json.loads(payload.decode('utf-8-sig'))
        if not isinstance(row, dict) or not isinstance(row.get('label'), str) or not row['label'].strip():
            raise ValueError('generation distribution needs a nonempty label')
        samples = row.get('samples_sec')
        if not isinstance(samples, list) or not samples or any(type(v) not in (int, float) for v in samples):
            raise ValueError('samples_sec must be a nonempty array of finite nonnegative numbers')
        try:
            samples = tuple(float(v) for v in samples)
        except OverflowError as exc:
            raise ValueError('samples_sec values must fit finite seconds') from exc
        if any(not math.isfinite(v) or v < 0 for v in samples):
            raise ValueError('samples_sec must be a nonempty array of finite nonnegative numbers')
        return cls(samples, row['label'].strip(), path.resolve(),
                   hashlib.sha256(payload).hexdigest())

    def metadata(self):
        return dict(label=self.label, path=str(self.path), sha256=self.sha256,
                    samples_sec=list(self.samples))


class GenerationOverride:
    """Change generation latency only; keep measured reply/silence/audio profiles."""
    def __init__(self, measured, distribution, seed):
        self.measured, self.distribution, self.seed = measured, distribution, seed
        self.rng = random.Random(seed)

    def sample(self, *args, **kwargs):
        cost = self.measured.sample(*args, **kwargs)
        key = kwargs.get('event_key')
        rng = (random.Random(int.from_bytes(hashlib.sha256(f'{self.seed}:gen-dist:{key}'.encode()).digest(), 'big'))
               if key is not None else self.rng)
        return replace(cost, generation=rng.choice(self.distribution.samples))

    def summary(self):
        return self.measured.summary()


@dataclass
class GenerationRecord:
    kind: str
    started_at: float
    sampled_sec: float
    spent_sec: float = 0
    completed: bool = False
    spoken: bool = False
    discarded: bool = False
    reason: str | None = None


class DurationModel:
    def __init__(self, turns, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        self.turns = [t for t in turns if t.end is not None]
        self.pools = defaultdict(list)
        for t in self.turns:
            self.pools[t.kind].append(t)
        self.rates = [t.chars/t.play for t in self.turns if t.chars and t.play]
        self.fixed_cost = None

    @classmethod
    def fixed(cls, generation, synth, play):
        obj = cls([], 0)
        obj.fixed_cost = Cost(generation, synth, play)
        return obj

    @classmethod
    def from_sessions(cls, sessions, seed=1111):
        return cls([t for session in sessions for t in session.turns], seed)

    def sample(self, kind, named=False, *, now=None, event_key=None, provider=None):
        if self.fixed_cost:
            return self.fixed_cost
        pool = self.pools.get(kind) or self.pools.get("moment" if kind == "game" else kind)
        if not pool:
            pool = self.turns
        if provider is not None:
            pool = [t for t in pool if t.source == provider] or [t for t in self.turns if t.source == provider]
        if not pool:
            raise ValueError("no completed turns available for duration sampling")
        if kind in OWNER_KINDS:
            conditioned = [t for t in pool if t.named == named]
            pool = conditioned or pool
        # Preserve observed provider outage epochs (not online success/failure
        # guesses). Otherwise A's late 413 burst gets spread over its opening,
        # erasing the long silence at the restart boundary.
        if now is not None and any(t.failed for t in pool):
            nearest = min(pool, key=lambda t: abs(t.start-now))
            pool = [t for t in pool if t.failed == nearest.failed]
        # Common random numbers: the same input gets the same draw in every
        # config, even if another policy selects more intervening turns.
        rng = (random.Random(int.from_bytes(hashlib.sha256(f'{self.seed}:{event_key}'.encode()).digest(), 'big'))
               if event_key is not None else self.rng)
        template = rng.choice(pool)
        # Cancelled outputs are censored at the real supersession check. They
        # are response opportunities, not evidence of a model choosing silence.
        speaks = bool(template.audio_start is not None or template.cancelled)
        if not speaks:
            return Cost(template.generation, 0, 0, False,
                        max(0, template.end-template.start-template.generation))
        voiced = [t for t in self.turns if t.source == template.source and t.chars and t.play and t.reply_at is not None]
        if not voiced:
            voiced = [t for t in self.turns if t.chars and t.play and t.reply_at is not None]
        if not voiced:
            raise ValueError("no measured chars/second and synth samples")
        # Bootstrap whole measured profiles to retain the covariance between
        # generation latency, reply length and speech speed. Censored replies
        # have no chars/play measurement, so borrow one from their provider.
        speech = template if template.audio_start is not None else rng.choice(voiced)
        cps = speech.chars/speech.play
        generation = template.generation
        if template.cancelled:
            # No printed reply exists for this right-censored output. Draw
            # start->reply from measured, completed replies on its provider.
            generation = speech.generation
        # tts_synth is a stage total across chunks. Playback onset minus the
        # printed reply captures the actual wait before the first audio.
        onset = max(0, speech.audio_start-speech.reply_at)
        return Cost(generation, onset, speech.chars/cps, True,
                    max(0, speech.end-speech.voiced_at), speech.reply_text)

    def summary(self):
        groups = defaultdict(list)
        for t in self.turns:
            groups[t.source].append(t)
        return {source: dict(turns=len(rows), generation_samples=sum(t.reply_at is not None for t in rows),
            failed_turns=sum(t.failed for t in rows),
            start_to_reply_median_sec=percentile([t.generation for t in rows if t.reply_at is not None], .5),
            silent_or_error_turns=sum(not t.chars and not t.cancelled for t in rows),
            measured_chars_per_sec_median=percentile([t.chars/t.play for t in rows if t.chars and t.play], .5))
            for source, rows in sorted(groups.items())}


def build_config(mode, session, run=ROOT, *, recorded=None, arms=None):
    """Arms are config overlays; future flags need only an arms JSON entry."""
    if mode == 'before':
        if recorded is None:
            raise ValueError('before requires an explicit recorded config')
        return dict(recorded['sessions'][session.path.name] if 'sessions' in recorded else recorded)
    from scripts.livepath.mabar import mabar_bridge_config
    cfg = mabar_bridge_config(run, passive=True, youtube_video_id='offline-replay', desk=mode == 'desk')
    overlays = {'plain': {}, 'desk': {}}
    overlays.update(arms or {})
    if mode not in overlays:
        raise ValueError('unknown config arm')
    cfg.update(overlays[mode])
    return cfg


def read_labels(path, sessions):
    """Apply exact log/turn identities. Labels define the complete question set."""
    import csv
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        required = {'log', 'turn_id', 'category', 'should_answer', 'she_answered', 'sensitive'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('labels TSV missing required columns')
        labels = {}
        for row in reader:
            if any(not isinstance(row.get(key), str) for key in required):
                raise ValueError('incomplete label row')
            key = (Path(row['log']).name, row['turn_id'])
            if key in labels:
                raise ValueError('duplicate label identity')
            for flag in ('should_answer', 'she_answered', 'sensitive'):
                if row[flag].lower() not in ('yes', 'no'):
                    raise ValueError('label flags must be yes/no')
            labels[key] = row
    matched = set()
    for session in sessions:
        for event in session.events:
            # Mic labels describe owner lines only; an enqueue/chat event that shares a turn id is not a label target.
            if event.type != 'owner':
                if event.turn_id is not None:
                    event.data.setdefault('should_answer', False)
                continue
            key = (session.path.name, event.turn_id)
            row = labels.get(key)
            event.data['should_answer'] = bool(row and row['should_answer'].lower() == 'yes')
            if row:
                if key in matched:
                    raise ValueError('ambiguous label identity within log')
                matched.add(key)
                event.data['measured_answered'] = row['she_answered'].lower() == 'yes'
    if matched != set(labels):
        raise ValueError('unmatched label identity')
    return len(matched)


def active_windows(active):
    if len(active) == 2 and isinstance(active[0], (float, int)):
        active = [active]
    merged = []
    for a, b in sorted(active):
        if not math.isfinite(a) or not math.isfinite(b) or b <= a:
            raise ValueError('active intervals must have finite increasing bounds')
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def longest_silence(intervals, active):
    cursor, end = active
    longest = 0.0
    for a, b in sorted(intervals):
        if b <= cursor or a >= end:
            continue
        longest = max(longest, max(0, a-cursor))
        cursor = max(cursor, min(b, end))
    return max(longest, end-cursor)


def metrics(owners, audio, counts, active, chats=()):
    windows = active_windows(active)
    is_active = lambda at: any(a <= at < b for a, b in windows)
    by_event = {}
    for a, b, kind, event in audio:
        if event is not None and event.type == 'owner' and b > a:
            by_event[id(event)] = min(by_event.get(id(event), float('inf')), a-event.at)
    questions = [e for e in owners if e.question]
    waits = [by_event[id(e)] for e in questions if id(e) in by_event]
    all_waits = list(by_event.values())
    answered_chat = set()
    for a, b, kind, event in audio:
        if event is not None and b > a and not event.data.get('chat_acknowledgement'):
            answered_chat.update(event.data.get('chat_ids', []))
    active_sec = sum(b-a for a, b in windows)
    play_lines = sum(kind in GAME_KINDS and b > a and is_active(a) for a, b, kind, e in audio)
    return dict(owner_questions=dict(total=len(questions), answered=len(waits),
                    share_answered=len(waits)/len(questions) if questions else None,
                    audio_latency_median_sec=percentile(waits, .5), audio_latency_p90_sec=percentile(waits, .9)),
        owner_all_voiced=dict(count=len(all_waits), audio_latency_median_sec=percentile(all_waits, .5),
                              audio_latency_p90_sec=percentile(all_waits, .9)),
        chat_messages=dict(incoming=len(chats), answered=len(answered_chat),
                           share_answered=len(answered_chat)/len(chats) if chats else None),
        chat_replies_active=sum(kind == "yt_chat" and b > a and is_active(a) for a, b, kind, e in audio),
        chat_replies_total=sum(kind == "yt_chat" and b > a for a, b, kind, e in audio),
        game_comments_active=play_lines,
        active_play_sec=active_sec,
        play_lines_per_2min=120*play_lines/active_sec if active_sec else None,
        items={**{reason: sum(counts[reason].values()) for reason in ("replaced", "expired", "cancelled")},
               **{reason+"_by_kind": dict(sorted(counts[reason].items())) for reason in counts}},
        longest_silence_active_sec=max((longest_silence([(a, b) for a, b, kind, e in audio], w)
                                       for w in windows), default=None))


def observed_metrics(sessions, active):
    owners, audio, chats = [], [], []
    unmatched = 0
    counts = {k: Counter() for k in ("replaced", "expired", "cancelled")}
    for session in sessions:
        owners.extend(e for e in session.events if e.type == "owner")
        chats.extend(e for e in session.events if e.type == 'chat')
        for turn in session.turns:
            if turn.audio_start is None:
                continue
            # Match only an earlier enqueue of the same id AND lane, since C's
            # initial chat shares t-0001 with a later mic event.
            matches = [e for e in session.events if e.turn_id == turn.id and e.queued_at is not None
                       and e.queued_at <= turn.start and (e.kind == turn.kind or e.kind == "game" and turn.kind in ('moment', 'status'))]
            e = max(matches, key=lambda e: e.queued_at) if matches else None
            unmatched += e is None
            if e is not None and e.kind == 'yt_chat':
                e = replace(e, data={**e.data, 'chat_ids': [f'{session.path.name}:{e.data.get("chat_line", e.line)}']})
            if e is not None and e.data.get('measured_answered') is False:
                e = None
            audio.append((turn.audio_start, turn.voiced_at, turn.kind, e))
        counts["expired"]["buffer_unclassified"] += session.counters.get("expired", 0)
        counts["expired"]["selection_unclassified"] += session.counters.get("selection_expired", 0)
        counts["cancelled"]["yt_chat"] += session.counters.get("chat_cancelled", 0)
        for key, value in session.counters.items():
            if key.startswith("replaced_"):
                counts["replaced"][key[9:]] += value
    result = metrics(owners, audio, counts, active, chats)
    labelled = [e for e in owners if e.question and 'measured_answered' in e.data]
    if labelled:
        answered = sum(e.data['measured_answered'] for e in labelled)
        result['owner_questions'].update(answered=answered, share_answered=answered/len(labelled))
    result['items']['buffer_ttl_expired'] = sum(s.counters.get('expired', 0) for s in sessions)
    result['items']['selection_expired'] = sum(s.counters.get('selection_expired', 0) for s in sessions)
    result['unmatched_audio_turns'] = unmatched
    return result


class Replay:
    """Virtual voice worker, with an optional measure-only parallel chat arm."""
    def __init__(self, model, active, *, chat_slot=False):
        self.model, self.active = model, active
        self.audio, self.owners = [], []
        self.counts = {k: Counter() for k in ("replaced", "expired", "cancelled")}
        self.buffer_expired = 0
        self.now = 0.0
        self.chat_slot = chat_slot
        self.generations = []
        self.generation_records = {}
        self.chats = []

    def start_turn(self, trigger, session, config):
        self.b.tts_stop_flag = False
        candidate = getattr(trigger.text, 'attention_candidate', None)
        raw = candidate.raw if candidate else None
        event = self.inputs.get(id(raw)) if raw is not None else next((e for e in session.events
            if e.turn_id == trigger.turn_id and e.queued_at == trigger.queued_at), None)
        event = self.selected_events.get((trigger.trigger_type, trigger.turn_id)) or event
        if event is not None and isinstance(trigger.text, Q.CombinedChat):
            event = replace(event, data={**event.data, 'chat_acknowledgement': True})
        key = (f'{session.path.name}:{event.line}' if event else
               f'{session.path.name}:{trigger.trigger_type}:{round(self.now, 1)}')
        provider = ('codex' if config.get('codex_agent_enabled') and config.get('codex_primary_voice')
            and trigger.trigger_type in (config.get('cursor_trigger_types') or ['yt_chat']) else None)
        cost = self.model.sample((event.data.get('witness_kind') if event else None)
            or trigger.trigger_type, bool(event and event.named), now=self.now, event_key=key, provider=provider)
        state = (trigger, event, cost, self.b.speech_control.owner_revision, self.b.speech_control.epoch)
        record = GenerationRecord(trigger.trigger_type, self.now, cost.generation)
        self.generations.append(record)
        self.generation_records[id(state)] = record
        return state

    def generated(self, state):
        record = self.generation_records[id(state)]
        record.completed, record.spent_sec = True, record.sampled_sec

    def discard(self, state, reason):
        record = self.generation_records[id(state)]
        if not record.completed:
            record.spent_sec = min(record.sampled_sec, max(0, self.now-record.started_at))
        record.discarded, record.reason = not record.spoken, reason

    def generation_summary(self):
        return dict(started=len(self.generations), completed=sum(g.completed for g in self.generations),
            sampled_generation_sec=sum(g.sampled_sec for g in self.generations),
            spent_generation_sec=sum(g.spent_sec for g in self.generations),
            discarded_before_audio=sum(g.discarded for g in self.generations),
            discarded_generation_sec=sum(g.spent_sec for g in self.generations if g.discarded),
            discarded_by_reason=dict(sorted(Counter(g.reason for g in self.generations if g.discarded).items())),
            started_by_kind=dict(sorted(Counter(g.kind for g in self.generations).items())))

    def expired(self, trigger):
        result = self.real_speech_expired(trigger)
        if result:
            self.counts["expired"][trigger.trigger_type] += 1
        return result

    def game_expired(self, trigger):
        result = self.real_game_expired(trigger)
        if result:
            self.counts["expired"]["game"] += 1
        return result

    def normalize(self, item):
        return ReplayTrigger(text=item.text, trigger_type=item.trigger_type, viewer_name=item.viewer_name,
                  queued_at=item.enqueued_at, turn_id=item.turn_id, stardew_voice_epoch=None,
                  owner_marked=item.owner_marked, rotation_rank=item.rotation_rank,
                  owner_reply_required=item.owner_reply_required, ptt_generation=item.ptt_generation,
                  host_story_retry=item.host_story_retry)

    def capture_removed(self, reason, before):
        remaining = {id(x) for x in self.q._items}
        for item in before:
            if id(item) not in remaining and not (reason == 'replaced' and id(item) in self.expired_in_mutation):
                self.counts[reason][item.trigger_type] += 1

    def purge(self):
        before = tuple(self.q._items)
        self.original_purge()
        self.buffer_expired += len(before)-len(self.q._items)
        remaining = {id(x) for x in self.q._items}
        self.expired_in_mutation.update(id(x) for x in before if id(x) not in remaining)
        self.capture_removed("expired", before)

    def admit_chat(self, event):
        """Run the current bridge reader callback; observe identities, not policy."""
        if event.bot or event.gate == 'bot_command':
            return
        chat_id = f'{self.session_name}:{event.data.get("chat_line", event.line)}'
        event = replace(event, data={**event.data, 'chat_ids': [chat_id]})
        self.current_event = event
        before = tuple(self.q._items)
        merged = {id(item): getattr(item, '_merged_n', 1) for item in before}
        self.expired_in_mutation = set()
        if any(self.b.CONFIG.get(flag) for flag in ('mic_priority_mark', 'chat_quiet_answer_all', 'media_watch_mode')):
            self.b.on_line(replay_policy.ssn_chat.ChatLine(
                event.data.get('platform', 'YouTube'), event.viewer or '',
                event.data.get('chat_text', event.text), event.turn_id or chat_id))
        else:
            # Historic admissions are recorded queue witnesses. Preserve them
            # when no counterfactual chat policy is enabled, including missing
            # viewer metadata; do not apply today's cooldown a second time.
            self.b.queue_voice_trigger(event.text, 'yt_chat', event.viewer)
        self.capture_removed('replaced', before)
        for item in self.q._items:
            key = (item.trigger_type, item.turn_id)
            if id(item) not in merged:
                target = event
                prior = self.selected_events.get(key)
                if prior is not None and isinstance(item.text, Q.CombinedChat):
                    target = replace(event, data={**event.data, 'chat_ids': [*prior.data.get('chat_ids', ()), chat_id]})
                self.inputs[id(item)] = target
                self.selected_events[key] = target
            elif getattr(item, '_merged_n', 1) > merged[id(item)]:
                target = self.selected_events[key]
                target.data['chat_ids'].append(chat_id)

    def ingress(self, event):
        if event.type == 'media_hold':
            self.b.hold_media_playback(event.data['seconds'], gate_desktop=False)
            return
        if event.type == 'chat':
            self.chats.append(event)
            # Quiet/media admission precedes cooldown/throttle and may accept
            # raw messages which the recording's older config never queued.
            if (self.b.CONFIG.get('chat_quiet_answer_all') or self.b.CONFIG.get('media_watch_mode')
                    or self.b.CONFIG.get('mic_priority_mark') and event.data.get('history_recorded')
                    and not event.data.get('queue_witness')):
                witness = self.chat_witnesses.get(event.line, event)
                self.handled_chat_lines.add(event.line)
                self.admit_chat(replace(witness, at=event.at, queued_at=event.at,
                    turn_id=witness.turn_id or f'chat-line-{event.line}', bot=event.bot, gate=event.gate,
                    data={**witness.data, 'chat_line': event.line, 'chat_text': event.text}))
            return
        if event.kind == 'yt_chat':
            if event.data.get('chat_line') not in self.handled_chat_lines:
                self.admit_chat(event)
            return
        if event.type == "owner":
            self.owners.append(event)
            if event.queued_at is None and not self.b.speech_control.command(event.text):
                return  # Recorded real ASR quality gate: history only.
        text = event.text
        if event.kind == "game":
            text = MomentText(text)
            text._live7_hold = event.data.get("held", False)
            text._host_story = event.data.get('host_story', False)
            text.created_at = event.at
            if event.data.get('game_kind'):
                text.game_salience = AD.native_salience({'kind': event.data['game_kind']})
                text.game_event_kind = event.data['game_kind']
            if event.data.get('witness_kind') == 'status':
                text._live7_status = True
                text.is_current = lambda: True
        elif event.kind == "own_play":
            text = OwnPlayText(text, created_at=event.at)
        elif event.kind == "read_in_game":
            text = AD.AttentionText(text)
            text.created_at = event.at
        before = tuple(self.q._items)
        self.expired_in_mutation = set()
        self.current_event = event
        marked = self.b._mic_pasif_owner_marked(text) if event.kind == 'mic_pasif' else True
        accepted = self.b.queue_voice_trigger(text, event.kind, event.viewer,
            owner_marked=marked)
        self.capture_removed("replaced", before)
        if accepted:
            for item in self.q._items:
                if item.turn_id == self.current_id or (
                    isinstance(item.text, Q.CombinedOwnerQuestions)
                    and all(item is not old for old in before)
                ):
                    self.inputs[id(item)] = event
                    self.selected_events[(item.trigger_type, item.turn_id)] = event
                    break

    async def session(self, session, config, journals):
        if not config.get('voice_queue_enabled', True):
            raise ValueError('replay requires the recorded priority buffer; legacy queue is unsupported')
        config = {**config, 'voice_queue_enabled': True, 'rehearsal_parent_pid': None}
        self.session_name = session.path.name
        self.selected_events = {}
        self.chat_witnesses = {e.data['chat_line']: e for e in session.events
                               if e.type == 'enqueue' and e.kind == 'yt_chat' and 'chat_line' in e.data}
        self.handled_chat_lines = set()
        self.holds = replay_policy.chat_media.HoldClock()
        self.now = session.start
        self.expired_in_mutation = set()
        native = {}
        self.b = NS(CONFIG=config, _brain_busy=False,
            tts_is_playing=False, hotkey_active=False, speech_control=safety.SpeechControl(),
            _brain_busy_lock=threading.RLock(), _game_reaction_queue_lock=threading.RLock(),
            _normalize_voice_trigger=self.normalize, _speech_trigger_expired=self.expired,
            _game_reaction_expired=self.game_expired, time=NS(time=lambda: self.now),
            _live7_help_relay=NS(_status_facts=NS(attention_native=native, attention_traits=[])),
            arti_http_util=NS(groq_session=lambda: None), VoiceTrigger=ReplayTrigger)
        replay_policy.bind(self.b, ReplayTrigger)
        self.q = self.b.voice_trigger_buffer = self.b._make_voice_trigger_buffer()
        self.original_purge = self.q._purge_expired
        self.q._purge_expired = self.purge
        self.b._live7_help_relay = NS(_status_facts=NS(attention_native=native, attention_traits=[]),
            speech_is_current=lambda at: True, note_owner_text=lambda *a: None,
            note_said=lambda *a, **kw: None, _said_owner_name=lambda: '', note_priority_turn=lambda: None,
            note_viewer_command=lambda *a: False)
        def identify(*args, **kwargs):
            self.current_id = self.current_event.turn_id or f'input-{self.current_event.line}'
            return self.current_id
        self.b.session_transcript = NS(log_trigger=identify)
        self.real_speech_expired = self.b._speech_trigger_expired
        self.real_game_expired = self.b._game_reaction_expired
        self.b._speech_trigger_expired = self.expired
        self.b._game_reaction_expired = self.game_expired
        self.inputs = {}
        heap, order = [], 0
        def schedule(at, tag, data=None):
            nonlocal order
            order += 1
            heapq.heappush(heap, (at, order, tag, data))
        for event in session.events:
            at = event.queued_at if event.queued_at is not None else event.at
            schedule(at, "input", event)
        for at, row in journals:
            if session.start <= at <= session.end and row.get("ev") == "native_vitals":
                schedule(at, "native", row)
        schedule(session.ready or session.start, "tick")
        state = None
        vad = False
        # Patch module references, not the process-wide time module. No timer,
        # background worker, model client or real bridge initialization occurs.
        async def offline_choice(*args, **kwargs):
            raise ValueError("offline: use real deterministic desk fallback")
        clock = NS(time=lambda: self.now)
        with ExitStack() as stack:
            stack.enter_context(patch.object(replay_policy.chat_media, 'holds', self.holds))
            for module in (Q, safety, AB):
                stack.enter_context(patch.object(module, "time", clock))
            stack.enter_context(patch.object(AD, "fast_choice", offline_choice))
            stack.enter_context(patch("scripts.livepath.bridge.emit", lambda row: None))
            if self.chat_slot:
                from scripts.livepath.replay_slots import run_parallel
                await run_parallel(self, session, config, native, heap, schedule)
                return
            while heap:
                self.now, _, tag, data = heapq.heappop(heap)
                if self.now > session.end:
                    break  # No draining beyond the actual stop/restart window.
                if tag == "input":
                    if data.type == "vad":
                        vad = data.data["speaking"]
                    else:
                        self.ingress(data)
                        media_interrupt = (data.type == 'media_hold' and self.b.tts_is_playing
                                           and self.b.tts_stop_flag)
                        media_blocked = (state is not None and config.get('media_watch_mode')
                                         and not self.b._media_hold_allows(state[0]))
                        if state is not None and (not self.b.speech_control.allows(state[4]) or media_interrupt or media_blocked):
                            if self.b.tts_is_playing and self.audio:
                                a, b, kind, e = self.audio[-1]
                                self.audio[-1] = (a, min(b, self.now), kind, e)
                            self.counts['cancelled'][state[0].trigger_type] += 1
                            self.discard(state, 'media_hold' if media_interrupt or media_blocked else 'owner_control')
                            if self.b.speech_control.allows(state[4]):
                                self.b._retain_chat_trigger(state[0])
                                AB.finish(self.b, state[0])
                            state = None
                            self.b._brain_busy = self.b.tts_is_playing = False
                elif tag == "native":
                    native.update(own_state={"health": {"observed_ts": self.now,
                        "value": {"current": data.get("hp"), "max": data.get("max_hp")}}},
                        current_menu={"observed_ts": self.now, "value": data.get("active_menu")})
                elif tag == "generated" and state is data:
                    self.generated(state)
                    trigger, e, cost, revision, epoch = state
                    if self.now < self.b._media_playback_until and not self.b._media_hold_allows(trigger):
                        schedule(self.b._media_playback_until, 'generated', state)
                    elif (not self.b.speech_control.allows(epoch) or self.b._voice_reply_superseded(trigger, revision)
                            or trigger.trigger_type == 'own_play' and self.real_speech_expired(trigger)):
                        self.counts["cancelled"][trigger.trigger_type] += 1
                        self.discard(state, 'superseded_or_owner_control')
                        schedule(self.now+cost.cleanup, "done", state)
                    elif not cost.speaks:
                        schedule(self.now+cost.cleanup, "done", state)
                    elif AD.enabled(config) and not AB.may_speak(self.b, trigger):
                        self.counts["cancelled"][trigger.trigger_type] += 1
                        self.discard(state, 'desk_may_speak')
                        schedule(self.now+cost.cleanup, "done", state)
                    else:
                        schedule(self.now+cost.synth, "audio", state)
                elif tag == "audio" and state is data:
                    trigger, e, cost, revision, epoch = state
                    if self.now < self.b._media_playback_until and not self.b._media_hold_allows(trigger):
                        schedule(self.b._media_playback_until, 'audio', state)
                    elif self.b.speech_control.allows(epoch) and not self.b._voice_reply_superseded(trigger, revision):
                        self.audio.append((self.now, min(self.now+cost.play, session.end), trigger.trigger_type, e))
                        self.generation_records[id(state)].spoken = True
                        self.b.tts_is_playing = True
                        schedule(self.now+cost.play, "voiced", state)
                    else:
                        self.counts["cancelled"][trigger.trigger_type] += 1
                        self.discard(state, 'owner_control')
                        schedule(self.now, "done", state)
                elif tag == "voiced" and state is data:
                    AB.voiced(self.b, state[0])
                    self.b._last_arti_reply_ts = self.now
                    self.b.last_arti_reply_text = state[2].reply_text
                    if state[0].trigger_type == 'own_play':
                        self.b._note_own_play_voiced()
                    self.b.tts_is_playing = False
                    schedule(self.now+state[2].cleanup, "done", state)
                elif tag == "done" and state is data:
                    if self.b.speech_control.allows(state[4]):
                        AB.finish(self.b, state[0])
                    state = None
                    self.b._brain_busy = self.b.tts_is_playing = False
                elif tag == "tick":
                    if AD.enabled(config):
                        AB.audio(self.b, vad, .1)
                    if state is None and not self.b.speech_control.held and (
                            self.now >= self.b._media_playback_until or self.b._waiting_hold_pass()):
                        # Track desk TTL removals without counting selected items.
                        self.q._purge_expired()
                        pool = tuple(self.q._items)
                        use_desk = AD.enabled(config) and not (config.get('media_watch_mode') and (
                            replay_policy.chat_media.watch_held(config, self.now) or self.q.depth_for('video')))
                        trigger = self.b._dequeue_buffered_trigger_if_brain_ready(owner_only=use_desk)
                        if use_desk and trigger is None:
                            trigger = await AB.dequeue(self.b)
                        if use_desk:
                            for item in pool:
                                if item.trigger_type in {"own_play", "read_in_game"} and self.now-item.enqueued_at > AD.GAME_TTL_SEC:
                                    self.counts["expired"][item.trigger_type] += 1
                        # The live main loop revalidates every selected trigger,
                        # including selections returned by the attention desk.
                        if trigger is not None and (self.expired(trigger) or self.game_expired(trigger)):
                            self.b._brain_busy, self.b._brain_busy_since = False, 0.0
                            trigger = None
                        if trigger is not None:
                            state = self.start_turn(trigger, session, config)
                            schedule(self.now+state[2].generation, "generated", state)
                    schedule(self.now+.1, "tick")
        if state is not None and not self.generation_records[id(state)].spoken:
            self.discard(state, 'shutdown')
        # Pending humans remain unanswered at shutdown; they are not fake expiry.


def replay(sessions, model, mode, *, config_factory=build_config, active, journals=(), chat_slot=False):
    worker = Replay(model, active, chat_slot=chat_slot)
    async def run():
        for session in sessions:
            await worker.session(session, config_factory(mode, session), journals)
    with redirect_stdout(io.StringIO()):
        asyncio.run(run())
    result = metrics(worker.owners, worker.audio, worker.counts, active, worker.chats)
    result['items']['buffer_ttl_expired'] = worker.buffer_expired
    result['items']['selection_expired'] = result['items']['expired']-worker.buffer_expired
    result['generations'] = worker.generation_summary()
    return result


def main(argv=None):
    from scripts.livepath.replay_report import calibrate, table
    parser = argparse.ArgumentParser(description='Offline voice timeline replay; no devices or providers.')
    parser.add_argument('--run', type=Path, help='run folder; discovers session_logs/*_bridge.log and runtime/live7/**/g1_journal.jsonl')
    parser.add_argument('--log', type=Path, action='append', help='bridge log; repeat for process/restart windows')
    parser.add_argument('--journal', type=Path, action='append', help='live7 journal; repeat as needed')
    parser.add_argument('--recorded-config', type=Path, required=True, help='recorded config object, or {sessions: {log basename: config}}')
    parser.add_argument('--labels', type=Path, help='TSV: log, turn_id, category, should_answer, she_answered, sensitive')
    parser.add_argument('--arms', type=Path, help='JSON config overlays; e.g. {desk_v2: {attention_desk_v2: true}}')
    parser.add_argument('--config', action='append', help='arm name; repeat; default: before, plain, desk')
    parser.add_argument('--active-start', action='append', help='ISO timestamp; repeat paired with --active-end')
    parser.add_argument('--active-end', action='append', help='ISO timestamp; excludes gaps between pairs')
    parser.add_argument('--session-date', type=date.fromisoformat, help='date for undated synthetic/exported filenames')
    parser.add_argument('--seed', type=int, default=1111)
    parser.add_argument('--gen-dist', type=Path, help='optional generation latency distribution JSON')
    parser.add_argument('--chat-slot', action='store_true', help='experimental extra chat generator, plain arm only')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        paths = args.log or (sorted((args.run / 'session_logs').glob('*_bridge.log')) if args.run else [])
        if not paths:
            raise ValueError('provide --run or --log')
        paths = sorted(set(p.resolve() for p in paths))
        journals = args.journal or (sorted((args.run / 'runtime/live7').rglob('g1_journal.jsonl')) if args.run else [])
        target = args.output_dir.resolve()
        inputs = paths + journals + [args.recorded_config] + ([args.labels] if args.labels else []) + ([args.arms] if args.arms else []) + ([args.gen_dist] if args.gen_dist else [])
        if not target.is_relative_to(ROOT) or any(p.resolve().is_relative_to(target) for p in inputs):
            raise ValueError('output must be in this checkout, separate from every input')
        if args.run and target.is_relative_to(args.run.resolve()):
            raise ValueError('output must be outside the input run')
        recorded = json.loads(args.recorded_config.read_text(encoding='utf-8-sig'))
        arms = json.loads(args.arms.read_text(encoding='utf-8-sig')) if args.arms else {}
        if not isinstance(recorded, dict) or not isinstance(arms, dict) or 'before' in arms:
            raise ValueError('configs must be objects; before cannot be overridden')
        if any(not re.fullmatch(r'[a-z][a-z0-9_-]*', k) or not isinstance(v, dict) for k, v in arms.items()):
            raise ValueError('arm names must be safe identifiers and overlays must be objects')
        modes = list(dict.fromkeys(['before'] + (args.config or ['plain', 'desk'])))
        if args.chat_slot and any(m not in ('before', 'plain') for m in modes):
            raise ValueError('chat-slot supports --config plain only')
        sessions = sorted([read_session(p, args.session_date) for p in paths], key=lambda s: s.start)
        if len({s.path.name for s in sessions}) != len(sessions):
            raise ValueError('log basenames must be unique within a replay')
        if any(b.start < a.end for a, b in zip(sessions, sessions[1:])):
            raise ValueError('overlapping bridge processes require separate replay runs')
        if args.labels:
            read_labels(args.labels, sessions)
        journal_rows = read_journals(journals)
        enrich_game_events(sessions, journal_rows)
        if bool(args.active_start) != bool(args.active_end) or len(args.active_start or []) != len(args.active_end or []):
            raise ValueError('active-start and active-end must be paired')
        if not args.active_start:
            raise ValueError('explicit --active-start/--active-end intervals required; journal presence does not prove active play')
        def timestamp(value):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError('active timestamps require timezone offsets')
            return parsed.timestamp()
        active = active_windows([(timestamp(a), timestamp(b)) for a, b in zip(args.active_start, args.active_end)])
        distribution = GenerationDistribution.load(args.gen_dist) if args.gen_dist else None
        def make_model(override=True):
            measured = DurationModel.from_sessions(sessions, args.seed)
            return GenerationOverride(measured, distribution, args.seed) if distribution and override else measured
        results = {}
        for mode in modes:
            results[mode] = replay(sessions, make_model(mode != 'before'), mode,
                config_factory=lambda m, s: build_config(m, s, args.run or ROOT, recorded=recorded, arms=arms),
                active=active, journals=journal_rows, chat_slot=args.chat_slot and mode == 'plain')
        if args.chat_slot:
            baseline = replay(sessions, make_model(), 'plain',
                config_factory=lambda m, s: build_config(m, s, args.run or ROOT, recorded=recorded, arms=arms),
                active=active, journals=journal_rows)['generations']
            generations = results['plain']['generations']
            generations.update(extra_generations_started=generations['started']-baseline['started'],
                extra_generation_sec=generations['spent_generation_sec']-baseline['spent_generation_sec'])
        measured = observed_metrics(sessions, active)
        reasons = []
        if measured['unmatched_audio_turns']:
            reasons.append('unmatched_audio_turn')
        if any(t.audio_start is None and t.voiced_at is not None for s in sessions for t in s.turns):
            reasons.append('missing_playback_latency_witness')
        if any(e.kind == 'yt_chat' and e.data.get('chat_line') is None for s in sessions for e in s.events if e.type == 'enqueue'):
            reasons.append('missing_chat_ingress_witness')
        if any(e.type == 'chat' and e.data.get('history_recorded') and not e.data.get('queue_witness')
               for s in sessions for e in s.events):
            reasons.append('unwitnessed_chat_merge_or_admission')
        if any(e.data.get('measured_answered') and not any(t.id == e.turn_id and t.audio_start is not None for t in s.turns)
               for s in sessions for e in s.events if e.type == 'owner'):
            reasons.append('missing_labelled_audio_witness')
        if any(build_config('before', s, recorded=recorded).get('alerts_yield_after_sentence') for s in sessions):
            reasons.append('sentence_boundary_yield_not_recorded')
        calibration = calibrate(results['before'], measured, reasons=reasons)
        sources = ('scripts/livepath/replay_timeline.py', 'scripts/livepath/replay_slots.py',
            'scripts/livepath/replay_policy.py', 'scripts/livepath/replay_report.py', 'scripts/livepath/mabar.py',
            'arti_bridge.py', 'arti_voice_queue.py', 'arti_runtime/mic_priority.py',
            'arti_runtime/chat_rotation.py', 'arti_runtime/chat_media.py', 'arti_runtime/ssn_chat.py',
            'arti_wake.py', 'arti_runtime/attention_bridge.py', 'arti_runtime/attention_desk.py')
        identities = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources}
        input_ids = [dict(id=f'input-{i}', sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for i, p in enumerate(inputs)]
        reports = {m: dict(schema='arti.voice_timeline_replay/2', arm=m, seed=args.seed,
            evidence='UNIT_TESTED', metrics=result, calibration=calibration,
            source_sha256=identities, inputs=input_ids, active=active,
            labels_used=bool(args.labels), generations_override=bool(distribution and m != 'before'),
            limitations=['recorded_asr_and_model_outcomes', 'recorded_media_reaction_readiness', 'desk_deterministic_fallback',
                        'redacted_game_payload_freshness', 'recorded_merge_coverage_may_be_incomplete',
                        'no_device_or_backend_contention_measurement']) for m, result in results.items()}
        summary = table(reports)
        target.mkdir(parents=True, exist_ok=True)
        for mode, report in reports.items():
            (target / f'{mode}.json').write_text(json.dumps(report, indent=2, ensure_ascii=True)+'\n', encoding='utf-8')
        (target / 'summary.md').write_text(summary+'\n', encoding='utf-8')
        print(summary)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Never echo data from configs, labels, provider text or journal payloads.
        parser.error('invalid or unsupported replay input (' + type(exc).__name__ + ')')


if __name__ == '__main__':
    raise SystemExit(main())
