"""WEB-1B: ToolRouter runtime — allowlist, kebijakan query, Groq dulu, telemetry.

Tidak punya persona, tidak memicu regex, tidak memanggil lookup_block.
Tidak mengaktifkan mode agentic di produksi.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from arti_brain_contract import (
    WEB_SEARCH,
    BackendResult,
    BrainLease,
    Cancellation,
    ToolCall,
    ToolError,
    ToolObservation,
    TurnBudget,
    WebSearchBackend,
    make_tool_observation,
)
from arti_web_backends import (
    CursorLookupWebSearchBackend,
    build_web_backend_registry,
    cursor_agentic_gates_proven,
)

SEARCH_STAGE_MAX_S = 12.0
RESUME_RESERVE_S = 8.0
FINALIZATION_RESERVE_S = 0.5
BACKEND_MIN_REMAINING_S = 1.0
CURSOR_FALLBACK_MIN_S = 3.0

_SECRET_RE = re.compile(
    r"(?i)("
    r"-----BEGIN "
    r"|sk-[A-Za-z0-9]{10,}"
    r"|gsk_[A-Za-z0-9]{10,}"
    r"|bearer\s+[A-Za-z0-9._\-]{12,}"
    r"|api[_-]?key\s*[:=]\s*\S+"
    r")"
)
_LOCAL_PATH_RE = re.compile(
    r"(?i)("
    r"[a-z]:\\|"
    r"\\\\|"
    r"/home/|"
    r"/users/|"
    r"vault/|"
    r"config_local\.json|"
    r"arti_soul|"
    r"\.rcon_password"
    r")"
)
_PRIVATE_CONTEXT_RE = re.compile(
    r"(?i)("
    r"screen snapshot|"
    r"\[screen\]|"
    r"nomor hp|"
    r"\bcvc\b|"
    r"account-synthetic|"
    r"screen-owner@"
    r")"
)

_AMBIGUOUS_TIMEOUT_CODES = frozenset({"timeout", "cancelled", "deadline"})


@dataclass
class RouterAttempt:
    backend_id: str
    status: str
    error_code: str | None
    latency_ms: int
    search_performed: bool | None
    source_count: int
    truncated: bool
    fallback_cause: str | None
    started: bool


@dataclass
class RuntimeToolRouter:
    backends: Sequence[WebSearchBackend]
    config: Mapping[str, Any] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    telemetry: Callable[..., None] | None = None

    def __post_init__(self) -> None:
        self._results: dict[str, ToolObservation] = {}
        self._inflight: dict[str, asyncio.Task[ToolObservation]] = {}
        self.attempts: list[RouterAttempt] = []
        self.http_starts: int = 0
        self.denied: list[str] = []
        self._lock = asyncio.Lock()

    async def execute(
        self,
        call: ToolCall,
        *,
        turn_id: str,
        call_id: str,
        requester: BrainLease,
        budget: TurnBudget,
    ) -> ToolObservation:
        async def _run() -> ToolObservation:
            obs = await self._execute_fresh(
                call,
                turn_id=turn_id,
                call_id=call_id,
                requester=requester,
                budget=budget,
            )
            self._results[call_id] = obs
            return obs

        async with self._lock:
            cached = self._results.get(call_id)
            if cached is not None:
                return cached
            inflight = self._inflight.get(call_id)
            if inflight is not None:
                task = inflight
            else:
                task = asyncio.create_task(_run())
                self._inflight[call_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            self._inflight.pop(call_id, None)

    async def _execute_fresh(
        self,
        call: ToolCall,
        *,
        turn_id: str,
        call_id: str,
        requester: BrainLease,
        budget: TurnBudget,
    ) -> ToolObservation:
        query = str(call.args.get("query", "")).strip()
        deny = _deny_reason(call)
        self._record_decision(
            requester=requester,
            turn_id=turn_id,
            call_id=call_id,
            reason=call.reason,
            denied=deny,
            query=query,
        )
        if deny is not None:
            self.denied.append(deny)
            return self._observation(
                status="denied",
                query=query,
                turn_id=turn_id,
                call_id=call_id,
                provider=None,
                error=ToolError(code=deny, message=_deny_message(deny)),
            )

        window = _search_window(budget, self.clock)
        if window < BACKEND_MIN_REMAINING_S or budget.cancel.is_cancelled():
            status = "timeout" if not budget.cancel.is_cancelled() else "timeout"
            code = "cancelled" if budget.cancel.is_cancelled() else "deadline"
            return self._observation(
                status=status,
                query=query,
                turn_id=turn_id,
                call_id=call_id,
                provider=None,
                error=ToolError(code=code, message="Jendela pencarian tidak cukup atau dibatalkan."),
            )

        cursor = self._backend("cursor_lookup")
        last: BackendResult | None = None
        groq_ambiguous = False

        primary = [b for b in self.backends if b.backend_id in ("codex", "groq_compound")]
        for attempt_index, backend in enumerate(primary):
            last, ambiguous = await self._run_backend(
                backend,
                query,
                budget=budget,
                window=_search_window(budget, self.clock),
                attempt_index=attempt_index,
                turn_id=turn_id,
                call_id=call_id,
                requester=requester,
            )
            if backend.backend_id == "groq_compound":
                groq_ambiguous = ambiguous
            if last is not None and last.status == "ok":
                return self._from_backend(last, query, turn_id, call_id)

        cursor_ok = (
            cursor is not None
            and last is not None
            and not groq_ambiguous
            and _cursor_should_run(last, cursor, self.config)
        )
        remaining_after = _search_window(budget, self.clock)
        if cursor_ok and remaining_after >= CURSOR_FALLBACK_MIN_S:
            cursor_result, _ = await self._run_backend(
                cursor,
                query,
                budget=budget,
                window=remaining_after,
                attempt_index=len(primary),
                turn_id=turn_id,
                call_id=call_id,
                requester=requester,
            )
            if cursor_result is not None:
                last = cursor_result
                if cursor_result.status == "ok":
                    return self._from_backend(cursor_result, query, turn_id, call_id)
        elif cursor is not None and not cursor_ok:
            self._record_skip_cursor(
                turn_id=turn_id,
                call_id=call_id,
                requester=requester,
                cause=_cursor_skip_cause(
                    last, groq_ambiguous, cursor, remaining_after, self.config
                ),
            )

        if last is None:
            return self._observation(
                status="unavailable",
                query=query,
                turn_id=turn_id,
                call_id=call_id,
                provider=None,
                error=ToolError(code="no_backend", message="Tidak ada backend pencarian yang dijalankan."),
            )
        return self._from_backend(last, query, turn_id, call_id)

    def _backend(self, backend_id: str) -> WebSearchBackend | None:
        for backend in self.backends:
            if getattr(backend, "backend_id", "") == backend_id:
                return backend
        return None

    async def _run_backend(
        self,
        backend: WebSearchBackend,
        query: str,
        *,
        budget: TurnBudget,
        window: float,
        attempt_index: int,
        turn_id: str,
        call_id: str,
        requester: BrainLease,
    ) -> tuple[BackendResult | None, bool]:
        if budget.cancel.is_cancelled():
            return (
                BackendResult(
                    provider=getattr(backend, "backend_id", "unknown"),
                    status="timeout",
                    content="",
                    sources=(),
                    error=ToolError(code="cancelled", message="Pencarian dibatalkan."),
                    search_performed=False,
                    model=None,
                ),
                False,
            )
        if window < BACKEND_MIN_REMAINING_S:
            return None, False
        deadline = self.clock() + window
        if budget.search_deadline is not None:
            deadline = min(deadline, budget.search_deadline)
        deadline = min(deadline, budget.deadline - RESUME_RESERVE_S)
        started_at = self.clock()
        self.http_starts += 1
        ambiguous = False
        result: BackendResult | None = None
        try:
            result = await backend.search(query, deadline=deadline, cancel=budget.cancel)
        except asyncio.CancelledError:
            ambiguous = True
            result = BackendResult(
                provider=getattr(backend, "backend_id", "unknown"),
                status="timeout",
                content="",
                sources=(),
                error=ToolError(code="cancelled", message="Pencarian dibatalkan."),
                search_performed=False,
                model=None,
            )
        except TimeoutError:
            ambiguous = True
            result = BackendResult(
                provider=getattr(backend, "backend_id", "unknown"),
                status="timeout",
                content="",
                sources=(),
                error=ToolError(code="timeout", message="Backend melebihi batas waktu."),
                search_performed=False,
                model=None,
            )
        except Exception as exc:  # noqa: BLE001
            result = BackendResult(
                provider=getattr(backend, "backend_id", "unknown"),
                status="error",
                content="",
                sources=(),
                error=ToolError(code="transport", message=type(exc).__name__),
                search_performed=False,
                model=None,
            )
        latency_ms = max(0, int((self.clock() - started_at) * 1000))
        if result is not None and result.status == "timeout":
            code = (result.error.code if result.error else "timeout")
            if code in _AMBIGUOUS_TIMEOUT_CODES:
                ambiguous = True
        self._record_attempt(
            backend=backend,
            result=result,
            latency_ms=latency_ms,
            attempt_index=attempt_index,
            turn_id=turn_id,
            call_id=call_id,
            requester=requester,
            window=window,
            ambiguous=ambiguous,
        )
        return result, ambiguous

    def _from_backend(
        self,
        result: BackendResult,
        query: str,
        turn_id: str,
        call_id: str,
    ) -> ToolObservation:
        return self._observation(
            status=result.status,
            query=query,
            turn_id=turn_id,
            call_id=call_id,
            provider=result.provider,
            content=result.content,
            sources=result.sources,
            error=result.error,
            truncated=result.truncated,
        )

    def _observation(
        self,
        *,
        status: str,
        query: str,
        turn_id: str,
        call_id: str,
        provider: str | None,
        content: str = "",
        sources: tuple = (),
        error: ToolError | None = None,
        truncated: bool = False,
    ) -> ToolObservation:
        return make_tool_observation(
            status=status,
            query=query,
            turn_id=turn_id,
            call_id=call_id,
            checked_at=_rfc3339(),
            provider=provider,
            content=content,
            sources=sources,
            error=error,
            truncated=truncated,
        )

    def _record_decision(
        self,
        *,
        requester: BrainLease,
        turn_id: str,
        call_id: str,
        reason: str,
        denied: str | None,
        query: str,
    ) -> None:
        extra = {
            "stage": "decision",
            "call_id": call_id,
            "tool": WEB_SEARCH,
            "requester_provider": requester.provider,
            "requester_model": requester.model,
            "requester_effort": requester.effort,
            "reason": reason[:200],
            "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest()[:16],
            "query_len": len(query),
            "denied": denied,
            "allowance_consumed": 1,
        }
        self._emit(
            subsystem="web_router",
            provider=requester.provider,
            model=requester.model or "",
            latency_ms=0,
            ok=denied is None,
            turn_id=turn_id,
            extra=extra,
        )

    def _record_attempt(
        self,
        *,
        backend: WebSearchBackend,
        result: BackendResult | None,
        latency_ms: int,
        attempt_index: int,
        turn_id: str,
        call_id: str,
        requester: BrainLease,
        window: float,
        ambiguous: bool,
    ) -> None:
        backend_id = getattr(backend, "backend_id", "unknown")
        status = result.status if result is not None else "error"
        error_code = result.error.code if result is not None and result.error else None
        attempt = RouterAttempt(
            backend_id=backend_id,
            status=status,
            error_code=error_code,
            latency_ms=latency_ms,
            search_performed=result.search_performed if result is not None else None,
            source_count=len(result.sources) if result is not None else 0,
            truncated=bool(result.truncated) if result is not None else False,
            fallback_cause="ambiguous_timeout" if ambiguous else None,
            started=True,
        )
        self.attempts.append(attempt)
        extra = {
            "stage": "backend_attempt",
            "call_id": call_id,
            "backend_id": backend_id,
            "attempt_index": attempt_index,
            "status": status,
            "error_code": error_code,
            "search_performed": attempt.search_performed,
            "source_count": attempt.source_count,
            "truncated": attempt.truncated,
            "fallback_cause": attempt.fallback_cause,
            "effective_budget_s": window,
            "requester_provider": requester.provider,
        }
        self._emit(
            subsystem="web_router",
            provider=backend_id,
            model=(result.model if result is not None and result.model else ""),
            latency_ms=latency_ms,
            ok=status == "ok",
            turn_id=turn_id,
            extra=extra,
        )

    def _record_skip_cursor(
        self,
        *,
        turn_id: str,
        call_id: str,
        requester: BrainLease,
        cause: str,
    ) -> None:
        extra = {
            "stage": "backend_skip",
            "call_id": call_id,
            "backend_id": "cursor_lookup",
            "fallback_cause": cause,
            "requester_provider": requester.provider,
        }
        self._emit(
            subsystem="web_router",
            provider="cursor_lookup",
            model="",
            latency_ms=0,
            ok=False,
            turn_id=turn_id,
            extra=extra,
        )

    def _emit(
        self,
        *,
        subsystem: str,
        provider: str,
        model: str,
        latency_ms: int,
        ok: bool,
        turn_id: str,
        extra: dict[str, Any],
    ) -> None:
        fn = self.telemetry
        if fn is None:
            try:
                import arti_api_telemetry as tel  # noqa: PLC0415

                fn = tel.record_call
            except Exception:  # noqa: BLE001
                return
        try:
            fn(
                subsystem=subsystem,
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                ok=ok,
                turn_id=turn_id,
                extra=extra,
                config=dict(self.config),
            )
        except Exception:  # noqa: BLE001
            return


def _rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _deny_reason(call: ToolCall) -> str | None:
    if call.name != WEB_SEARCH:
        return "unknown_tool"
    query = str(call.args.get("query", ""))
    if _SECRET_RE.search(query):
        return "secret_like"
    if _LOCAL_PATH_RE.search(query):
        return "local_path"
    if _PRIVATE_CONTEXT_RE.search(query):
        return "private_context"
    return None


def model_query_deny_reason(query: str, private_contexts=()) -> str | None:
    """Stricter egress gate for text proposed by a model, before any provider."""
    reason = _deny_reason(ToolCall(name=WEB_SEARCH, args={"query": query}, reason="model lookup"))
    if reason:
        return reason
    if not query or len(query) > 80:
        return "query_length"
    if re.search(r"\S+@\S+|\b(?:\d[ .()+-]*){7,}|\b[A-Za-z0-9]{20,}\b|"
                 r"\b(?:sk|gsk|ghp|gho|xox[baprs])[-_]", query, re.IGNORECASE):
        return "private_identifier"
    if not re.fullmatch(r"[A-Za-z0-9 ,.?'-]+", query):
        return "query_characters"
    if re.search(r"(?i)\b[\w.-]+\.(?:txt|json|md|env|key|pem|sqlite|db|log)\b", query):
        return "local_path"
    if re.search(
        r"(?i)\b(token|secret|password|passwd|credential|rahasia|memori|memory|vault|"
        r"viewer|penonton|alamat|telepon|rekening|ktp|passport|paspor|email|e-mail|"
        r"riwayat|diagnosis|diagnosa|alergi|gaji|tanggal lahir|tinggal di)\b", query
    ):
        return "private_context"
    words = re.findall(r"\w+", query.casefold())
    # Reject excerpts of the memory supplied to the voice turn; never send it
    # to the lookup provider to ask whether it is private.
    for context in private_contexts:
        memory = " ".join(re.findall(r"\w+", str(context).casefold()))
        if any(len(part := " ".join(words[i:i + 3])) >= 12 and part in memory
               for i in range(max(0, len(words) - 2))):
            return "memory_excerpt"
    return None


def _deny_message(code: str) -> str:
    return {
        "unknown_tool": "Tool tidak ada di katalog runtime.",
        "secret_like": "Query menyerupai rahasia; pencarian ditolak.",
        "local_path": "Query memuat jalur lokal; pencarian ditolak.",
        "private_context": "Query memuat konteks layar/akun; pencarian ditolak.",
    }.get(code, "Query ditolak kebijakan runtime.")


def _search_window(budget: TurnBudget, clock: Callable[[], float]) -> float:
    now = clock()
    remaining_total = budget.deadline - now
    reserved = RESUME_RESERVE_S + FINALIZATION_RESERVE_S
    window = remaining_total - reserved
    if budget.search_deadline is not None:
        window = min(window, budget.search_deadline - now)
    return min(SEARCH_STAGE_MAX_S, max(0.0, window))


def _cursor_should_run(
    groq_result: BackendResult,
    cursor: WebSearchBackend,
    config: Mapping[str, Any],
) -> bool:
    if groq_result.status == "ok":
        return False
    if groq_result.status == "timeout":
        return False
    if isinstance(cursor, CursorLookupWebSearchBackend) and not cursor.eligible():
        return False
    if not cursor_agentic_gates_proven(config):
        return False
    return groq_result.status in {"no_results", "error", "unavailable"}


def _cursor_skip_cause(
    groq_result: BackendResult | None,
    groq_ambiguous: bool,
    cursor: WebSearchBackend,
    remaining: float,
    config: Mapping[str, Any],
) -> str:
    if groq_ambiguous or (groq_result is not None and groq_result.status == "timeout"):
        return "ambiguous_timeout"
    if remaining < CURSOR_FALLBACK_MIN_S:
        return "insufficient_budget"
    if isinstance(cursor, CursorLookupWebSearchBackend) and not cursor.eligible():
        return "capability_unavailable"
    if not cursor_agentic_gates_proven(config):
        return "capability_unavailable"
    return "not_eligible"


def make_runtime_tool_router(
    config: Mapping[str, Any] | None = None,
    *,
    groq_search_fn: Callable[..., Any] | None = None,
    cursor_search_fn: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    telemetry: Callable[..., None] | None = None,
) -> RuntimeToolRouter:
    cfg = dict(config or {})
    backends = build_web_backend_registry(
        cfg,
        groq_search_fn=groq_search_fn,
        cursor_search_fn=cursor_search_fn,
        clock=clock,
    )
    return RuntimeToolRouter(
        backends=backends,
        config=cfg,
        clock=clock or time.monotonic,
        telemetry=telemetry,
    )
