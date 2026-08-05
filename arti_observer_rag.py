"""Observer RAG — separate SQLite for beats; promote via vault reindex."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import arti_vault_rag as vault_rag

_ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG: dict[str, Any] = {
    "observer_enabled": True,
    "observer_db_path": "data/observer_rag.db",
    "observer_embed_all_beats": True,
}


def _observer_cfg(config: dict | None) -> dict:
    base = {**vault_rag.DEFAULT_CONFIG, **DEFAULT_CONFIG, **(config or {})}
    base["vault_rag_db_path"] = base.get("observer_db_path", "data/observer_rag.db")
    return base


def init_db(config: dict | None = None) -> None:
    vault_rag.init_db(_observer_cfg(config))


def index_stats(config: dict | None = None) -> dict[str, Any]:
    return vault_rag.index_stats(_observer_cfg(config))


def reindex_beats_session(session_id: str, config: dict | None = None) -> dict[str, int] | None:
    """Index *_beats.md into observer_rag.db."""
    cfg = _observer_cfg(config)
    md = _ROOT / "vault" / "sessions" / f"{session_id}_beats.md"
    if not md.is_file():
        return None
    rel = md.relative_to(_ROOT).as_posix()
    cfg = {
        **cfg,
        "vault_rag_index_globs": [rel],
    }
    try:
        return vault_rag.reindex_all(cfg, verbose=False)
    except Exception as e:
        print(f"[Observer RAG] embed skip: {e}")
        return None


def promote_approved_to_vault(session_id: str, config: dict | None = None) -> None:
    """Approved beats md is indexed into live vault_rag on shutdown reindex."""
    md = _ROOT / "vault" / "sessions" / f"{session_id}_beats.md"
    if md.is_file():
        print(f"[Curator] Approved beats ready for vault RAG: {md.name}")


def reindex_session(session_id: str, config: dict | None = None) -> dict[str, int] | None:
    return reindex_beats_session(session_id, config)
