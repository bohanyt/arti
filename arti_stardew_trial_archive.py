"""Bounded local provider evidence for validated trials only; never authority.

Ordinary messages/responses are exact. Credential redaction and truncation are
explicit in each record. No transport headers, SDK events or policy/config dump.
"""
from __future__ import annotations

from arti_runtime import wait_ledger as W

import json
from pathlib import Path
import re
import shutil
import threading

from arti_stardew_source_tags import DOMAIN, source_tags, tag_value


MAX_STEPS = 4096
REQUEST_BYTES = 128 * 1024
RESPONSE_BYTES = 32 * 1024
TOTAL_BYTES = 8 * 1024 * 1024
RUN_BYTES = 128 * 1024 * 1024  # below the harness's 256 MiB admission stop
DISK_FLOOR = 15 * 1024**3
_CREDENTIAL = re.compile(r"(?i)(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)")
_SECRET_TEXT = re.compile(
    r"(?i)\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----|"
    r'''\b(?:[a-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)["']?\s*[:=]\s*["']?[^\s,"'}]+'''
)


class TrialProviderArchive:
    def __init__(self, journal_dir, *, secrets=(), tags=None):
        self.directory = Path(journal_dir) / "provider_archive"
        # Live7 WP4: domain/cycle/day_trial on every record (null cycle/day_trial outside Live7).
        base = source_tags()
        self._tags = {key: tag_value(tags.get(key)) if key != "domain" else DOMAIN
                      for key in base} if isinstance(tags, dict) else base
        self._secrets = tuple(s for s in secrets if isinstance(s, str) and s)
        self._lock = threading.Lock()
        self._bytes = 0

    def _budget_allows(self, count, *, totals=None):
        """Count only this session's provider evidence, including packed days/restarts.

        ADR 163: existing lossless storage compresses the closed archive. Its
        index names logical files whose bytes live under run/objects, not missing
        evidence. The harness still owns its disk projection/admission stop.
        """
        parent = self.directory.parent
        while not parent.exists():
            parent = parent.parent
        if shutil.disk_usage(parent).free - count < DISK_FLOOR:
            return False
        if totals is None:
            from arti_runtime.archive_budget import accounting
            with accounting(self.directory) as (own, total, _):
                totals = own, total
        own, total = totals
        self._bytes = max(self._bytes, own)
        return self._bytes + count <= TOTAL_BYTES and total + count <= RUN_BYTES

    def step(self, generation_id, lane, index):
        """Capture one committed physical call, including its eventual late reply."""
        if type(index) is not int or not 1 <= index <= MAX_STEPS:
            return None
        request_denied = False

        @W.span('journal_archive', 119)
        def write(kind, payload):
            nonlocal request_denied
            # Evidence failure must never change provider admission/response/cleanup.
            try:
                if kind not in ("request", "response"):
                    return
                if kind == "response" and request_denied:
                    return
                redacted = False

                def clean(value):
                    nonlocal redacted
                    if isinstance(value, dict):
                        result = {}
                        for key, item in value.items():
                            if _CREDENTIAL.fullmatch(key):
                                result[key] = "[REDACTED]"
                                redacted = True
                            else:
                                result[key] = clean(item)
                        return result
                    if isinstance(value, list):
                        return [clean(v) for v in value]
                    if isinstance(value, str):
                        original = value
                        for secret in self._secrets:
                            value = value.replace(secret, "[REDACTED]")
                        value = _SECRET_TEXT.sub("[REDACTED]", value)
                        redacted |= value != original
                    return value

                content = clean(payload)
                meta = clean({"generation_id": generation_id, "lane": lane})
                encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                cap = REQUEST_BYTES if kind == "request" else RESPONSE_BYTES
                # Reserve metadata/JSON escaping room, keeping a valid JSON record under the cap.
                record = {"schema": "stardew_trial_provider_archive.v1", "request_index": index,
                          **meta, "kind": kind, "redacted": redacted, "truncated": False,
                          **self._tags, "content": content}
                data = json.dumps(record, ensure_ascii=False).encode("utf-8")
                if len(data) > cap:
                    record.update(truncated=True, content_bytes=len(encoded))
                    record.pop("content")
                    # Prefix is of the sanitized JSON representation, never the original secret text.
                    record["content_json_prefix"] = encoded[:cap // 8].decode("utf-8", errors="ignore")
                    data = json.dumps(record, ensure_ascii=False).encode("utf-8")
                from arti_runtime.archive_budget import accounting
                with self._lock, accounting(self.directory) as (own, total, added):
                    # Leave room for this request's bounded reply. At capacity
                    # keep neither half rather than an unpaired response alone.
                    needed = len(data) + (RESPONSE_BYTES if kind == "request" else 0)
                    if len(data) > cap or not self._budget_allows(needed, totals=(own, total)):
                        request_denied |= kind == "request"
                        return
                    self.directory.mkdir(parents=True, exist_ok=True)
                    with (self.directory / f"{index:03d}-{kind}.json").open("xb") as handle:
                        handle.write(data)
                    self._bytes += len(data)
                    added(len(data))
            except Exception:
                pass

        return write
