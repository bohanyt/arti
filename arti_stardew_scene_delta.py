"""OBS-2A: diff fact scene.current_location yang deterministik.

Tidak membaca Stardew, tidak mengirim input, tidak menimpa MOTOR_STATE.
Producer runtime (ModEntry / SMAPI events) bukan bagian OBS-2A.
Canonical: docs/stardew/OBS-2-SCENE-DELTAS.md
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from arti_stardew_scene import SCENE_DOMAIN, SCENE_PRODUCT

# Harus sama dengan SceneDeltaCaps.MaxPatchOps di C#. Tes penjaga
# membaca kedua sisi. Jangan menaikkan MAX_LINE_BYTES receiver.
SCENE_DELTA_MAX_PATCH_OPS = 48


def canonical_json(value: object) -> str:
    """JSON kanonik: kunci objek terurut, tanpa spasi, Unicode utuh."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def facts_equal(left: object, right: object) -> bool:
    return canonical_json(left) == canonical_json(right)


def _copy_facts(source: Mapping[str, object] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    if source is None:
        return out
    for kunci, isi in source.items():
        if not isinstance(kunci, str) or not kunci:
            continue
        out[kunci] = isi
    return out


@dataclass(frozen=True)
class SceneDeltaPatch:
    upserts: Mapping[str, object]
    removes: tuple[str, ...]
    observed_count: int
    sent_count: int
    truncated: bool

    @property
    def is_empty(self) -> bool:
        return not self.upserts and not self.removes


def diff_scene_facts(
    before: Mapping[str, object] | None,
    after: Mapping[str, object] | None,
    *,
    max_patch_ops: int = SCENE_DELTA_MAX_PATCH_OPS,
) -> SceneDeltaPatch:
    """Diff dua peta fact. Keluaran terurut Ordinal; remove dulu saat cap.

    Cap yang terlewati menandai truncated=true. Store OBS-0 lalu
    stale/resync — jangan diam-diam membuang op sambil klaim complete.
    """
    if max_patch_ops < 0:
        raise ValueError("max_patch_ops tidak boleh negatif")
    prev = _copy_facts(before)
    nxt = _copy_facts(after)
    ids = sorted(set(prev) | set(nxt))
    upserts: dict[str, object] = {}
    removes: list[str] = []
    for fact_id in ids:
        had = fact_id in prev
        has = fact_id in nxt
        if not had and has:
            upserts[fact_id] = nxt[fact_id]
        elif had and not has:
            removes.append(fact_id)
        elif had and has and not facts_equal(prev[fact_id], nxt[fact_id]):
            upserts[fact_id] = nxt[fact_id]

    observed = len(upserts) + len(removes)
    if observed == 0:
        return SceneDeltaPatch(
            upserts=MappingProxyType({}),
            removes=(),
            observed_count=0,
            sent_count=0,
            truncated=False,
        )

    remaining = max_patch_ops
    sent_removes: list[str] = []
    for fact_id in removes:
        if remaining <= 0:
            break
        sent_removes.append(fact_id)
        remaining -= 1
    sent_upserts: dict[str, object] = {}
    for fact_id in upserts:
        if remaining <= 0:
            break
        sent_upserts[fact_id] = upserts[fact_id]
        remaining -= 1
    sent = len(sent_upserts) + len(sent_removes)
    return SceneDeltaPatch(
        upserts=MappingProxyType(sent_upserts),
        removes=tuple(sent_removes),
        observed_count=observed,
        sent_count=sent,
        truncated=sent < observed,
    )


def apply_scene_delta(
    facts: Mapping[str, object] | None,
    patch: SceneDeltaPatch,
) -> dict[str, object]:
    """Remove dulu, baru upsert — sama dengan RichObservationStore."""
    nxt = _copy_facts(facts)
    for fact_id in patch.removes:
        nxt.pop(fact_id, None)
    nxt.update(dict(patch.upserts))
    return nxt


def apply_scene_patch(
    facts: Mapping[str, object] | None,
    *,
    upserts: Mapping[str, object] | None = None,
    removes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, object]:
    return apply_scene_delta(
        facts,
        SceneDeltaPatch(
            upserts=MappingProxyType(dict(upserts or {})),
            removes=tuple(removes or ()),
            observed_count=len(upserts or {}) + len(removes or ()),
            sent_count=len(upserts or {}) + len(removes or ()),
            truncated=False,
        ),
    )


def build_delta_payload(
    *,
    session_id: str,
    previous_revision: int,
    provenance: str,
    authoritative: bool,
    patch: SceneDeltaPatch,
) -> dict[str, object]:
    """Payload kind=delta, domain_revision = R+1. Tidak membuka session."""
    truncation: dict[str, object] | None
    if patch.truncated:
        truncation = {
            "truncated": True,
            "observed_count": patch.observed_count,
            "sent_count": patch.sent_count,
        }
    elif not patch.is_empty:
        truncation = {
            "truncated": False,
            "observed_count": patch.observed_count,
            "sent_count": patch.sent_count,
        }
    else:
        truncation = None
    return {
        "session_id": session_id,
        "product": SCENE_PRODUCT,
        "domain": SCENE_DOMAIN,
        "kind": "delta",
        "domain_revision": previous_revision + 1,
        "provenance": provenance,
        "authoritative": authoritative,
        "available": True,
        "error": None,
        "truncation": truncation,
        "body": {
            "upserts": dict(patch.upserts),
            "removes": list(patch.removes),
        },
    }
