"""BRAIN_OBSERVATION store — kontrak OBS-0, terpisah dari MOTOR_STATE.

Tidak membaca Stardew, tidak mengirim input, tidak menimpa StardewStateStore.
Canonical: docs/stardew/OBS-0-CONTRACT.md
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

LIVE_AUTHORITATIVE = frozenset(
    {
        "LOCAL_AUTHORITATIVE",
        "ACTIVE_SYNCED",
        "TEAM_SYNCED",
    }
)

PROVENANCE_VALUES = frozenset(
    {
        "LOCAL_AUTHORITATIVE",
        "ACTIVE_SYNCED",
        "TEAM_SYNCED",
        "STATIC_DATA",
        "SHADOW_STALE_POSSIBLE",
        "DERIVED",
        "UNKNOWN",
    }
)

OBSERVATION_EVENT = "observation"

KIND_SNAPSHOT = "snapshot"
KIND_DELTA = "delta"
KIND_ERROR = "error"
KIND_MANIFEST = "manifest"
APPLY_KINDS = frozenset({KIND_SNAPSHOT, KIND_DELTA, KIND_ERROR, KIND_MANIFEST})
# Snapshot, manifest, dan error boleh membuka epoch baru dan
# meretire session lama. Delta tidak.
REPLACE_KINDS = frozenset({KIND_SNAPSHOT, KIND_MANIFEST, KIND_ERROR})

KNOWN_PRODUCTS = frozenset({"scene", "ui", "progress", "knowledge"})
KNOWN_DOMAINS = frozenset(
    {
        "scene.current_location",
        "ui.active_menu",
        "ui.context",
        "ui.text",
        "progress.player",
        "progress.inventory",
        "knowledge.catalog",
        "knowledge.world",
    }
)
KNOWN_OBSERVATION_PAIRS = frozenset(
    {
        ("scene", "scene.current_location"),
        ("ui", "ui.active_menu"),
        ("ui", "ui.context"),
        ("ui", "ui.text"),
        ("progress", "progress.player"),
        ("progress", "progress.inventory"),
        ("knowledge", "knowledge.catalog"),
        ("knowledge", "knowledge.world"),
    }
)

# Session ID yang sudah diganti tidak dihidupkan lagi. Bound supaya
# penyimpanan tidak tumbuh tanpa batas; urutan ID opaq tidak di-parse.
MAX_RETIRED_SESSIONS = 32


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _facts_map(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for kunci, isi in value.items():
        if not isinstance(kunci, str) or not kunci:
            return None
        out[kunci] = isi
    return out


def _present(obj: dict, key: str) -> bool:
    """True kalau kunci ada dan nilainya bukan JSON null."""
    return key in obj and obj[key] is not None


def _truncation_sah(obj: object) -> bool:
    if obj is None:
        return True
    if not isinstance(obj, dict):
        return False
    truncated = obj.get("truncated")
    if not isinstance(truncated, bool):
        return False
    observed = obj.get("observed_count")
    sent = obj.get("sent_count")
    if not _plain_int(observed) or observed < 0:
        return False
    if not _plain_int(sent) or sent < 0:
        return False
    if sent > observed:
        return False
    if truncated and sent >= observed:
        return False
    if not truncated and sent != observed:
        return False
    return True


def _error_sah(obj: object) -> bool:
    if obj is None:
        return True
    if not isinstance(obj, dict):
        return False
    if not _nonempty_str(obj.get("reason")):
        return False
    detail = obj.get("detail")
    if detail is not None and not isinstance(detail, str):
        return False
    return True


def _delta_patch_sah(body: dict) -> bool:
    upserts = body.get("upserts")
    if upserts is not None and _facts_map(upserts) is None:
        return False
    removes = body.get("removes")
    if removes is not None:
        if not isinstance(removes, list):
            return False
        if not all(isinstance(item, str) and item for item in removes):
            return False
    return True


def _available_error_sah(kind: str, payload: dict) -> bool:
    """Satu aturan: error = unavailable; snapshot/delta/manifest = available."""
    err = payload.get("error")
    if kind == KIND_ERROR:
        if payload.get("available") is not False:
            return False
        if err is None or not _error_sah(err):
            return False
        if payload.get("body") is not None:
            return False
        if payload.get("truncation") is not None:
            return False
        return True
    if payload.get("available") is not True:
        return False
    if err is not None:
        return False
    return True


def _body_kind_sah(kind: str, payload: dict) -> bool:
    """Grammar body per kind — fail closed. Kombinasi salah ditolak."""
    body = payload.get("body")
    if kind == KIND_ERROR:
        return True
    if kind == KIND_SNAPSHOT:
        if not isinstance(body, dict):
            return False
        if _present(body, "upserts") or _present(body, "removes"):
            return False
        if _present(body, "catalog_id"):
            return False
        facts = _facts_map(body.get("facts"))
        if facts is None:
            return False
        truncation = payload.get("truncation")
        if isinstance(truncation, dict) and truncation["sent_count"] != len(facts):
            return False
        return True
    if kind == KIND_DELTA:
        # Delta kosong sah: body null, {}, atau upserts/removes kosong.
        # Tidak boleh membawa facts (itu snapshot).
        if body is None:
            return True
        if not isinstance(body, dict):
            return False
        if _present(body, "facts") or _present(body, "catalog_id"):
            return False
        return _delta_patch_sah(body)
    if kind == KIND_MANIFEST:
        if payload.get("product") != "knowledge":
            return False
        if payload.get("domain") != "knowledge.catalog":
            return False
        if not isinstance(body, dict):
            return False
        if _present(body, "facts") or _present(body, "upserts") or _present(body, "removes"):
            return False
        if not _nonempty_str(body.get("catalog_id")):
            return False
        for kunci in ("game_version", "content_hash"):
            nilai = body.get(kunci)
            if nilai is not None and not isinstance(nilai, str):
                return False
        entry_count = body.get("entry_count")
        if entry_count is not None and (not _plain_int(entry_count) or entry_count < 0):
            return False
        return True
    return False


def observation_payload_sah(payload: object) -> bool:
    """Validasi payload `observation` — fail closed pada field wajib + grammar kind."""
    if not isinstance(payload, dict):
        return False
    if not _nonempty_str(payload.get("session_id")):
        return False
    if not _nonempty_str(payload.get("product")):
        return False
    if not _nonempty_str(payload.get("domain")):
        return False
    if not _nonempty_str(payload.get("kind")):
        return False
    revision = payload.get("domain_revision")
    if not _plain_int(revision) or revision < 0:
        return False
    if not _nonempty_str(payload.get("provenance")):
        return False
    if not isinstance(payload.get("available"), bool):
        return False
    authoritative = payload.get("authoritative")
    if authoritative is not None and not isinstance(authoritative, bool):
        return False
    if (
        payload.get("provenance") == "SHADOW_STALE_POSSIBLE"
        and authoritative is True
    ):
        return False
    if not _truncation_sah(payload.get("truncation")):
        return False
    kind = payload["kind"]
    if kind in APPLY_KINDS:
        if not _available_error_sah(kind, payload):
            return False
        return _body_kind_sah(kind, payload)
    body = payload.get("body")
    if body is None:
        return True
    return isinstance(body, dict)


def is_live_authoritative(provenance: str, authoritative: bool | None = None) -> bool:
    if provenance == "SHADOW_STALE_POSSIBLE":
        return False
    if authoritative is False:
        return False
    return provenance in LIVE_AUTHORITATIVE


@dataclass(frozen=True)
class ApplyResult:
    accepted: bool = False
    ignored: bool = False
    rejected: bool = False
    reason: str = ""
    requires_resync: bool = False


@dataclass(frozen=True)
class DomainRecord:
    player_id: int
    screen_id: int
    session_id: str
    product: str
    domain: str
    kind: str
    domain_revision: int
    seq: int
    provenance: str
    authoritative: bool
    available: bool
    truncated: bool | None
    observed_count: int | None
    sent_count: int | None
    error_reason: str | None
    facts: Mapping[str, object] = field(default_factory=dict)
    catalog_id: str | None = None
    stale: bool = False
    requires_resync: bool = False
    received_monotonic: float = field(default_factory=time.monotonic, compare=False)

    @property
    def is_complete(self) -> bool:
        if self.stale or self.requires_resync or not self.available:
            return False
        if self.kind == KIND_ERROR:
            return False
        if self.kind == KIND_MANIFEST:
            return False
        if self.truncated is not False:
            return False
        if self.observed_count is None or self.sent_count is None:
            return False
        if self.kind == KIND_SNAPSHOT and self.sent_count != len(self.facts):
            return False
        # observed_count/sent_count di record adalah metadata kelengkapan
        # dari snapshot, bukan jumlah entitas live setelah delta.
        return self.sent_count == self.observed_count


@dataclass(frozen=True)
class _SeqCursor:
    session_id: str
    seq: int


class RichObservationStore:
    """Store deterministik per (player_id, domain). Aman lintas thread."""

    def __init__(self) -> None:
        self._kunci = threading.Lock()
        self._records: dict[tuple[int, str], DomainRecord] = {}
        self._last_seq: dict[tuple[int, str], _SeqCursor] = {}
        self._retired: dict[tuple[int, str], list[str]] = {}

    def apply(self, event: Mapping[str, object]) -> ApplyResult:
        if event.get("ev") != OBSERVATION_EVENT:
            return ApplyResult(ignored=True, reason="not_observation")
        payload = event.get("payload")
        if not observation_payload_sah(payload) or not isinstance(payload, dict):
            return ApplyResult(rejected=True, reason="decode_error")

        kind = payload["kind"]
        if kind not in APPLY_KINDS:
            return ApplyResult(ignored=True, reason="unknown_kind")

        pair = (payload["product"], payload["domain"])
        if pair not in KNOWN_OBSERVATION_PAIRS:
            return ApplyResult(ignored=True, reason="unknown_domain")

        player_id = event["player_id"]
        screen_id = event["screen_id"]
        seq = event["seq"]
        session_id = payload["session_id"]
        domain = payload["domain"]
        key = (player_id, domain)

        with self._kunci:
            if session_id in self._retired.get(key, ()):
                return ApplyResult(rejected=True, reason="session_superseded")

            rec = self._records.get(key)
            seq_key = (player_id, session_id)
            last = self._last_seq.get(seq_key)
            if last is not None and seq <= last.seq:
                return ApplyResult(rejected=True, reason="stale_sequence")

            if rec is not None and rec.session_id != session_id:
                if kind not in REPLACE_KINDS:
                    return ApplyResult(rejected=True, reason="session_mismatch")
                self._retire(key, rec.session_id)

            if kind == KIND_DELTA:
                return self._apply_delta(
                    rec, key, seq_key, payload, player_id, screen_id, seq, session_id
                )

            return self._apply_replace(
                rec, key, seq_key, payload, player_id, screen_id, seq, session_id, kind
            )

    def _retire(self, key: tuple[int, str], session_id: str) -> None:
        retired = self._retired.setdefault(key, [])
        if session_id not in retired:
            retired.append(session_id)
        overflow = len(retired) - MAX_RETIRED_SESSIONS
        if overflow > 0:
            del retired[0:overflow]

    def _apply_delta(
        self,
        rec: DomainRecord | None,
        key: tuple[int, str],
        seq_key: tuple[int, str],
        payload: dict,
        player_id: int,
        screen_id: int,
        seq: int,
        session_id: str,
    ) -> ApplyResult:
        if rec is None:
            return ApplyResult(
                rejected=True, reason="revision_gap", requires_resync=True
            )
        if rec.stale or rec.requires_resync:
            return ApplyResult(
                rejected=True, reason="revision_gap", requires_resync=True
            )
        expected = rec.domain_revision + 1
        revision = payload["domain_revision"]
        if revision != expected:
            self._records[key] = replace(rec, stale=True, requires_resync=True)
            return ApplyResult(
                rejected=True, reason="revision_gap", requires_resync=True
            )

        facts = dict(rec.facts)
        body = payload.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        removes = body.get("removes") or []
        upserts = _facts_map(body.get("upserts")) or {}
        for item in removes:
            facts.pop(item, None)
        facts.update(upserts)
        patch_truncated = False
        trunc = payload.get("truncation")
        if isinstance(trunc, dict) and trunc.get("truncated") is True:
            patch_truncated = True
        next_rec = self._record_from_payload(
            payload,
            player_id=player_id,
            screen_id=screen_id,
            seq=seq,
            facts=facts,
            kind=KIND_DELTA,
            stale=False,
            requires_resync=False,
            previous=rec,
        )
        if patch_truncated:
            next_rec = replace(next_rec, stale=True, requires_resync=True)
        self._records[key] = next_rec
        self._last_seq[seq_key] = _SeqCursor(session_id, seq)
        return ApplyResult(
            accepted=True,
            requires_resync=next_rec.requires_resync,
            reason="truncated" if patch_truncated else "",
        )

    def _apply_replace(
        self,
        rec: DomainRecord | None,
        key: tuple[int, str],
        seq_key: tuple[int, str],
        payload: dict,
        player_id: int,
        screen_id: int,
        seq: int,
        session_id: str,
        kind: str,
    ) -> ApplyResult:
        if (
            rec is not None
            and rec.session_id == session_id
            and payload["domain_revision"] < rec.domain_revision
            and kind != KIND_ERROR
        ):
            return ApplyResult(rejected=True, reason="stale_sequence")

        body = payload.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        if kind == KIND_ERROR:
            facts: dict[str, object] = {}
        elif kind == KIND_MANIFEST:
            facts = {}
        else:
            facts = _facts_map(body.get("facts")) or {}
        next_rec = self._record_from_payload(
            payload,
            player_id=player_id,
            screen_id=screen_id,
            seq=seq,
            facts=facts,
            kind=kind,
            stale=False,
            requires_resync=False,
            previous=None,
        )
        self._records[key] = next_rec
        self._last_seq[seq_key] = _SeqCursor(session_id, seq)
        return ApplyResult(accepted=True)

    @staticmethod
    def _record_from_payload(
        payload: dict,
        *,
        player_id: int,
        screen_id: int,
        seq: int,
        facts: dict[str, object],
        kind: str,
        stale: bool,
        requires_resync: bool,
        previous: DomainRecord | None,
    ) -> DomainRecord:
        truncation = payload.get("truncation")
        truncated = None
        observed = None
        sent = None
        if kind == KIND_DELTA and previous is not None:
            # Kelengkapan domain ditetapkan snapshot; delta tidak menimpa
            # observed/sent dengan hitungan patch.
            truncated = previous.truncated
            observed = previous.observed_count
            sent = previous.sent_count
        elif isinstance(truncation, dict):
            truncated = truncation["truncated"]
            observed = truncation["observed_count"]
            sent = truncation["sent_count"]
        provenance = payload["provenance"]
        authoritative_field = payload.get("authoritative")
        authoritative = is_live_authoritative(provenance, authoritative_field)
        error = payload.get("error")
        error_reason = None
        if isinstance(error, dict):
            error_reason = error.get("reason")
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        catalog_id = body.get("catalog_id") if isinstance(body, dict) else None
        if not isinstance(catalog_id, str):
            catalog_id = None
        return DomainRecord(
            player_id=player_id,
            screen_id=screen_id,
            session_id=payload["session_id"],
            product=payload["product"],
            domain=payload["domain"],
            kind=kind,
            domain_revision=payload["domain_revision"],
            seq=seq,
            provenance=provenance,
            authoritative=authoritative,
            available=payload["available"],
            truncated=truncated,
            observed_count=observed,
            sent_count=sent,
            error_reason=error_reason,
            facts=MappingProxyType(dict(facts)),
            catalog_id=catalog_id,
            stale=stale,
            requires_resync=requires_resync,
        )

    def get(self, player_id: int, domain: str) -> DomainRecord | None:
        with self._kunci:
            return self._records.get((player_id, domain))

    def current_revision(self, player_id: int, domain: str) -> int | None:
        rec = self.get(player_id, domain)
        return None if rec is None else rec.domain_revision

    def is_stale(self, player_id: int, domain: str) -> bool:
        rec = self.get(player_id, domain)
        return rec is None or rec.stale or rec.requires_resync

    def requires_resync(self, player_id: int, domain: str) -> bool:
        rec = self.get(player_id, domain)
        if rec is None:
            return True
        return rec.requires_resync

    def mark_stale(self, player_id: int, domain: str) -> None:
        with self._kunci:
            rec = self._records.get((player_id, domain))
            if rec is None:
                return
            self._records[(player_id, domain)] = replace(
                rec, stale=True, requires_resync=True
            )

    def query(
        self,
        player_id: int,
        domain: str,
        *,
        authoritative: bool = False,
        complete: bool = False,
    ) -> DomainRecord | None:
        rec = self.get(player_id, domain)
        if rec is None:
            return None
        if authoritative or complete:
            if rec.stale or rec.requires_resync or not rec.available:
                return None
        if authoritative and not rec.authoritative:
            return None
        if complete and not rec.is_complete:
            return None
        return rec
