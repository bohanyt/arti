"""Which farm a vault session note is from (ADR 238; setting ``vault_farm_labels``, default off).

One ARTI memory: recall keeps every note and labels where a session note comes from; nothing is filtered or re-ranked
by farm. A session note carries one tag line near its top, written by its writer (or once by
``scripts/livepath/vault_farm_backfill.py``)::

    <!-- arti-farm {"farm":"Ladang","scope_hashes":["<sha256 of save_id:player_id>"],"v":1} -->

It is an HTML comment: the vault indexer strips comments before chunking and again before a hit is shown
(``arti_vault_rag._filter_technical_sections``), so the tag is never memory text and never spoken. ``<`` and ``>``
inside the JSON are escaped, so the comment always ends at its own ``-->``.

The current farm is the ``scope_hash`` of the experience digest (``<arti_mind_dir>/stardew/experience_digest.json``,
ADR 228) written while this process runs. A missing, unreadable or older digest (an earlier session's farm) means no
current farm, and then no labels at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time

SETTING = "vault_farm_labels"
NAME_KEY = "session_farm_name"
TAG_RE = re.compile(r"^<!-- arti-farm (\{[^\r\n]*\}) -->\r?$", re.M)
HEAD_CHARS = 4096  # the tag sits right under the title line
THIS_FARM = "farm ini"
NO_FARM = "catatan lama, farm tidak tercatat"
# The one rule line under the [VAULT RAG ...] header when labels are on.
RULE = "Ingatan dari farm lain itu cerita di farm itu, bukan di farm ini."
# The farm sentence of her verified self facts (scripts/livepath/mabar.readback_lines; run_scope_hashes reads it too).
SELF_FACT_FARM = re.compile(r"Farm ini bernama (.+?) Farm\.")
NAME_MAX = 40
DIGEST_MAX_BYTES = 64 * 1024
# This process's start, as near as imports get: arti_bridge imports arti_vault_rag and session_transcript at the top,
# and both import this module at the top. A digest written before it is an earlier session's farm.
STARTED = time.time()
_SCOPE = re.compile(r"[0-9a-f]{64}")
_lock = threading.Lock()
_TAGS: dict[str, tuple[tuple[int, int], dict | None]] = {}
_TAGS_MAX = 512
_DIGESTS: dict[str, tuple[tuple[int, int], tuple[str | None, float]]] = {}


def enabled(config) -> bool:
    return (config or {}).get(SETTING, False) is True


def clean_name(name) -> str | None:
    """A farm's display name as one short line ("Ladang"), or None."""
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX or any(ord(ch) < 32 for ch in name):
        return None
    return name


def scope_ok(value) -> bool:
    return isinstance(value, str) and _SCOPE.fullmatch(value) is not None


def tag_line(name, scopes) -> str | None:
    """The tag line for a farm name and its scope hashes; None unless both are known."""
    name = clean_name(name)
    scopes = sorted({s for s in (scopes or ()) if scope_ok(s)})
    if not name or not scopes:
        return None
    body = json.dumps(dict(v=1, farm=name, scope_hashes=scopes), ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"))
    return "<!-- arti-farm " + body.replace("<", "\\u003c").replace(">", "\\u003e") + " -->"


def parse_tag(text) -> dict | None:
    """{"farm": name, "scope_hashes": (hash, ...)} from the tag line in the head of a note, or None."""
    if not isinstance(text, str):
        return None
    m = TAG_RE.search(text.lstrip("\ufeff")[:HEAD_CHARS])  # a title-less note with a BOM has its tag right after it
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (ValueError, RecursionError):  # RecursionError: deeply nested JSON; recall must never raise
        return None
    if not isinstance(data, dict) or data.get("v") != 1:
        return None
    name, scopes = clean_name(data.get("farm")), data.get("scope_hashes")
    if not name or not isinstance(scopes, list) or not scopes or not all(scope_ok(s) for s in scopes):
        return None
    return dict(farm=name, scope_hashes=tuple(scopes))


def add_tag(text: str, line: str) -> str:
    """``text`` with ``line`` right under its title line (first when it has none), in the note's own line ending.

    A note that already has a tag comes back unchanged; nothing else in the text moves."""
    if not line or parse_tag(text) is not None:
        return text
    bom = "﻿" if text.startswith("﻿") else ""
    body = text[len(bom):]
    first, sep, rest = body.partition("\n")
    nl = "\r\n" if first.endswith("\r") else "\n"
    if first.startswith("#"):
        return bom + first + "\n" + line + (nl + rest if sep else "\n")
    return bom + line + nl + body


def read_tag(path) -> dict | None:
    """The tag of the note file at ``path`` (its head only), or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(HEAD_CHARS * 4)
    except OSError:
        return None
    return parse_tag(head.decode("utf-8", errors="replace"))


def note_tag(path) -> dict | None:
    """``read_tag`` through a small cache: one read per note file, again only when the file changes."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key, name = (st.st_mtime_ns, st.st_size), str(path)
    with _lock:
        cached = _TAGS.get(name)
    if cached is not None and cached[0] == key:
        return cached[1]
    tag = read_tag(path)
    with _lock:
        if len(_TAGS) >= _TAGS_MAX:
            _TAGS.clear()
        _TAGS[name] = (key, tag)
    return tag


def digest_file(config) -> Path | None:
    try:
        import arti_mind
        from arti_runtime import experience_digest
        return experience_digest.path(arti_mind.mind_dir(config))
    except Exception:  # noqa: BLE001 - no mind dir = no farm
        return None


def current_scope(config, *, not_before: float | None = None) -> str | None:
    """The current farm: the experience digest's ``scope_hash``, only with the setting on and only from a digest
    written since ``not_before`` (default: this process's start). Never raises."""
    if not enabled(config):
        return None
    file = digest_file(config)
    if file is None:
        return None
    try:
        st = os.stat(file)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    with _lock:
        cached = _DIGESTS.get(str(file))
    if cached is not None and cached[0] == key:
        scope, written = cached[1]
    else:
        scope, written = None, 0.0
        try:
            if st.st_size > DIGEST_MAX_BYTES:
                raise ValueError("oversized")
            from arti_runtime import experience_digest, ipc
            row = json.loads(ipc.read_text(file))
            stamp = row.get("written_ts")
            if (row.get("schema") == experience_digest.SCHEMA and scope_ok(row.get("scope_hash"))
                    and type(stamp) in (int, float)):
                scope, written = row["scope_hash"], float(stamp)
        except (OSError, ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            pass
        with _lock:
            _DIGESTS[str(file)] = (key, (scope, written))
    limit = STARTED if not_before is None else float(not_before)
    return scope if scope and written >= limit else None


def self_facts_farm(path) -> str | None:
    """The farm name in her verified self facts file ("Farm ini bernama X Farm."), or None."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        m = SELF_FACT_FARM.search(" ".join(str(x) for x in (data.get("lines") or ())))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return clean_name(m.group(1)) if m else None


def farm_name(config) -> str | None:
    """The session's farm name: ``session_farm_name`` (mabar sets it), else her self facts file, read now (a new
    farm gets its name only after her farmer is made)."""
    cfg = config or {}
    return clean_name(cfg.get(NAME_KEY)) or self_facts_farm(cfg.get("live7_self_facts_file"))


def tag_note(text: str, config) -> str:
    """The session note writer's step: ``text`` with this session's farm tag, or unchanged (setting off, no farm
    name, or no current digest)."""
    if not enabled(config):
        return text
    try:
        scope = current_scope(config)
        line = tag_line(farm_name(config), [scope] if scope else ())
    except Exception:  # noqa: BLE001 - a note is never lost over its tag
        return text
    return add_tag(text, line) if line else text


class Recall:
    """Where each recalled session note is from, against the current farm."""

    def __init__(self, scope: str, root: Path):
        self.scope, self.root = scope, Path(root)

    def where(self, source_path: str) -> str:
        tag = note_tag(self.root / str(source_path))
        if tag is None:
            return NO_FARM
        if self.scope in tag["scope_hashes"]:
            return THIS_FARM
        return 'farm "%s"' % tag["farm"]  # quoted: a farm named "ini" never reads as this farm


def recall(config, root) -> Recall | None:
    """A Recall when labels are on and the current farm is known, else None (today's output)."""
    scope = current_scope(config)
    return Recall(scope, Path(root)) if scope else None
