"""WEB-1B: registri backend pencarian eksplisit.

Groq Compound Mini adalah executor utama. Cursor hanya masuk rantai agentic
kalau gerbang search-only + pembatalan sudah terbukti; kalau tidak, hasilnya
`unavailable` / `capability_unavailable` tanpa I/O. Tidak ada klien Tavily.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arti_brain_contract import (
    BackendResult,
    Cancellation,
    ToolError,
    WebSearchBackend,
)

CursorSearchFn = Callable[..., Awaitable[BackendResult]]
GroqSearchFn = Callable[..., Awaitable[BackendResult]]

CURSOR_CAPABILITY_UNAVAILABLE = ToolError(
    code="capability_unavailable",
    message="Cursor lookup belum terbukti search-only dan bisa dibatalkan.",
)


def cursor_agentic_gates_proven(config: Mapping[str, Any] | None) -> bool:
    """Gerbang eksplisit. Default False — role_allows_tools('lookup') bukan bukti."""
    cfg = config or {}
    return bool(cfg.get("web_lookup_cursor_search_only_proven")) and bool(
        cfg.get("web_lookup_cursor_cancel_proven")
    )


class GroqCompoundWebSearchBackend:
    backend_id = "groq_compound"

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        search_fn: GroqSearchFn | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = dict(config or {})
        self._search_fn = search_fn
        self._clock = clock

    async def search(
        self,
        query: str,
        *,
        deadline: float,
        cancel: Cancellation,
    ) -> BackendResult:
        fn = self._search_fn
        if fn is None:
            from arti_web_lookup import execute_groq_compound_search

            fn = execute_groq_compound_search
        kwargs: dict[str, Any] = {
            "deadline": deadline,
            "cancel": cancel,
            "config": self._config,
        }
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return await fn(query, **kwargs)


class CursorLookupWebSearchBackend:
    """Wrapper cadangan. Tanpa bukti gerbang, tidak memanggil send_task/SDK."""

    backend_id = "cursor_lookup"

    def __init__(
        self,
        *,
        config: Mapping[str, Any] | None = None,
        search_fn: CursorSearchFn | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = dict(config or {})
        self._search_fn = search_fn
        self._clock = clock

    def eligible(self) -> bool:
        return cursor_agentic_gates_proven(self._config) and self._search_fn is not None

    async def search(
        self,
        query: str,
        *,
        deadline: float,
        cancel: Cancellation,
    ) -> BackendResult:
        if not self.eligible():
            return BackendResult(
                provider=self.backend_id,
                status="unavailable",
                content="",
                sources=(),
                error=CURSOR_CAPABILITY_UNAVAILABLE,
                search_performed=False,
                model=None,
            )
        kwargs: dict[str, Any] = {
            "deadline": deadline,
            "cancel": cancel,
            "config": self._config,
        }
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return await self._search_fn(query, **kwargs)  # type: ignore[misc]


class CodexLookupWebSearchBackend(GroqCompoundWebSearchBackend):
    """Search-only ephemeral Codex role, isolated from the voice client."""
    backend_id = "codex"

    async def search(self, query: str, *, deadline: float, cancel: Cancellation) -> BackendResult:
        from arti_web_lookup import execute_codex_search
        fn = self._search_fn or execute_codex_search
        kwargs = {"config": self._config, "deadline": deadline, "cancel": cancel}
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return await fn(query, **kwargs)


def build_web_backend_registry(
    config: Mapping[str, Any] | None = None,
    *,
    groq_search_fn: GroqSearchFn | None = None,
    cursor_search_fn: CursorSearchFn | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[WebSearchBackend, ...]:
    """Respect the lookup config order; legacy default remains Groq then Cursor."""
    cfg = dict(config or {})
    groq = GroqCompoundWebSearchBackend(
        config=cfg, search_fn=groq_search_fn, clock=clock
    )
    cursor = CursorLookupWebSearchBackend(
        config=cfg, search_fn=cursor_search_fn, clock=clock
    )
    available = {"codex": CodexLookupWebSearchBackend(config=cfg, clock=clock),
                 "groq_compound": groq, "cursor": cursor, "cursor_lookup": cursor}
    return tuple(available[name] for name in cfg.get("web_lookup_provider_chain", ["groq_compound", "cursor"])
                 if name in available)
