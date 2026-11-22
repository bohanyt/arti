"""WEB-1A: state machine giliran logis otak — fake adapter/router, tanpa jaringan.

Runner ini:
- memparse keputusan lewat codec ketat (adapter boleh melempar DecisionCodecError);
- mereservasi tepat satu tool call SEBELUM I/O;
- me-resume BrainLease yang sama persis sekali;
- menolak tool kedua, keluaran cacat, dan mismatch sesi/generasi;
- membatalkan tanpa ucapan final;
- tidak menulis ToolCall/ToolObservation ke history/TTS.

Tidak mengimpor bridge, config_local, atau SDK provider.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from arti_brain_contract import (
    WEB_SEARCH,
    BrainAdapter,
    BrainDecision,
    BrainLease,
    DecisionCodecError,
    Speak,
    ToolCall,
    ToolObservation,
    ToolRouter,
    TurnBudget,
    TurnInput,
)

FALLBACK_MALFORMED_INITIAL = (
    "Aku belum bisa menyelesaikan jawabannya sekarang. Coba lagi sebentar, ya."
)
FALLBACK_CHECK_INCOMPLETE = (
    "Aku belum bisa menyelesaikan pengecekan itu sekarang. Coba lagi sebentar, ya."
)
RUNTIME_FALLBACK = "runtime_fallback"
INITIAL_CATALOG: tuple[str, ...] = (WEB_SEARCH,)


class ToolAllowanceExhausted(RuntimeError):
    """Allowance tool sudah dihabiskan sebelum I/O, atau call_id tidak dikenal."""


class BrainTransportError(RuntimeError):
    """Adapter gagal di transport/protokol. Bukan Speak palsu dari model."""


class BrainLeaseMismatch(RuntimeError):
    """Provider/model/session/generation tidak cocok dengan lease yang direservasi."""


@dataclass(frozen=True)
class LogicalTurnResult:
    outcome: Literal["final_speak", "runtime_fallback", "cancelled"]
    speak: Speak | None
    attribution: str
    requester: BrainLease | None
    turn_id: str
    call_id: str | None
    search_count: int
    history: tuple[str, ...]
    tts: tuple[str, ...]
    terminal_reason: str
    observations: tuple[ToolObservation, ...] = ()


class OneCallLedger:
    """Ledger satu-panggilan: consume allowance sebelum I/O, idempotent per call_id."""

    def __init__(self, max_calls: int = 1) -> None:
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        self.max_calls = max_calls
        self.consumed = 0
        self._seq = 0
        self._reserved: set[str] = set()
        self._results: dict[str, ToolObservation] = {}
        self._inflight: dict[str, asyncio.Task[ToolObservation]] = {}
        self._lock = asyncio.Lock()
        self.factory_starts = 0

    def remaining(self) -> int:
        return max(0, self.max_calls - self.consumed)

    def reserve(self, call_id: str | None = None) -> str:
        if call_id is not None and call_id in self._reserved:
            return call_id
        if self.consumed >= self.max_calls:
            raise ToolAllowanceExhausted("logical tool allowance exhausted")
        self.consumed += 1
        self._seq += 1
        cid = call_id or f"call-{self._seq}"
        self._reserved.add(cid)
        return cid

    async def deliver(
        self,
        call_id: str,
        factory: Callable[[], Awaitable[ToolObservation] | ToolObservation],
    ) -> ToolObservation:
        async def _run() -> ToolObservation:
            try:
                self.factory_starts += 1
                result = factory()
                if asyncio.iscoroutine(result):
                    result = await result
                self._results[call_id] = result  # type: ignore[assignment]
                return result  # type: ignore[return-value]
            finally:
                self._inflight.pop(call_id, None)

        async with self._lock:
            cached = self._results.get(call_id)
            if cached is not None:
                return cached
            inflight = self._inflight.get(call_id)
            if inflight is not None:
                task = inflight
            else:
                if call_id not in self._reserved:
                    raise ToolAllowanceExhausted(f"call_id not reserved: {call_id}")
                task = asyncio.create_task(_run())
                self._inflight[call_id] = task
        return await asyncio.shield(task)


def _cancelled_result(
    turn: TurnInput,
    lease: BrainLease | None,
    *,
    call_id: str | None = None,
    search_count: int = 0,
    observations: tuple[ToolObservation, ...] = (),
) -> LogicalTurnResult:
    return LogicalTurnResult(
        outcome="cancelled",
        speak=None,
        attribution="",
        requester=lease,
        turn_id=turn.turn_id,
        call_id=call_id,
        search_count=search_count,
        history=(),
        tts=(),
        terminal_reason="cancelled",
        observations=observations,
    )


def _spoken_result(
    speak: Speak,
    lease: BrainLease | None,
    turn: TurnInput,
    *,
    fallback: bool,
    reason: str,
    call_id: str | None,
    search_count: int,
    observations: tuple[ToolObservation, ...] = (),
) -> LogicalTurnResult:
    attribution = RUNTIME_FALLBACK if fallback else (
        lease.provider if lease is not None else RUNTIME_FALLBACK
    )
    return LogicalTurnResult(
        outcome="runtime_fallback" if fallback else "final_speak",
        speak=speak,
        attribution=attribution,
        requester=lease,
        turn_id=turn.turn_id,
        call_id=call_id,
        search_count=search_count,
        history=(speak.text,),
        tts=(speak.text,),
        terminal_reason=reason,
        observations=observations,
    )


def _timed_out(clock: Callable[[], float], budget: TurnBudget) -> bool:
    return clock() >= budget.deadline


async def run_logical_turn(
    turn: TurnInput,
    adapter: BrainAdapter,
    router: ToolRouter,
    budget: TurnBudget,
    *,
    clock: Callable[[], float] | None = None,
    catalog: tuple[str, ...] = INITIAL_CATALOG,
    ledger: OneCallLedger | None = None,
) -> LogicalTurnResult:
    """Jalankan satu giliran logis: decide -> (satu search) -> resume Speak."""
    now = clock or time.monotonic
    work = ledger if ledger is not None else OneCallLedger(max_calls=1)
    # WEB-1 budget monotonik: allowance efektif TIDAK PERNAH menaikkan jatah
    # yang dikirim pemanggil, dan tidak pernah melewati plafon satu search
    # per giliran logis. Nilai negatif fail-closed ke nol; ledger luar dengan
    # max_calls > 1 tetap tidak bisa mengangkat plafon giliran ini.
    allowance = min(1, max(0, budget.tool_calls_remaining), work.remaining())
    # Katalog yang bisa dieksekusi hanya diiklankan kalau allowance > 0.
    # Ini batas runtime, bukan sekadar janji prompt: lihat penjaga reserve
    # di bawah yang menolak ToolCall walau adapter mengabaikan katalog.
    decide_catalog = catalog if allowance > 0 else ()
    lease: BrainLease | None = None
    released = False
    accepted: ToolCall | None = None
    call_id: str | None = None
    observations: tuple[ToolObservation, ...] = ()
    search_count = 0

    async def _release(*, discard: bool) -> None:
        nonlocal released
        if lease is not None and not released:
            await adapter.release(lease, discard=discard)
            released = True

    def cancelled() -> bool:
        return budget.cancel.is_cancelled()

    try:
        if cancelled():
            return _cancelled_result(turn, None)
        if _timed_out(now, budget):
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                None,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=None,
                search_count=0,
            )

        try:
            lease = await adapter.reserve(turn, budget)
        except (BrainTransportError, BrainLeaseMismatch):
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                None,
                turn,
                fallback=True,
                reason="provider_unavailable",
                call_id=None,
                search_count=0,
            )

        if cancelled():
            await adapter.abort(lease, "cancelled")
            await _release(discard=True)
            return _cancelled_result(turn, lease)
        if _timed_out(now, budget):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                lease,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=None,
                search_count=0,
            )

        decide_budget = replace(budget, tool_calls_remaining=allowance)
        try:
            decision: BrainDecision = await adapter.decide(
                lease, turn, decide_catalog, decide_budget
            )
        except DecisionCodecError:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                lease,
                turn,
                fallback=True,
                reason="malformed_decision",
                call_id=None,
                search_count=0,
            )
        except (BrainTransportError, BrainLeaseMismatch):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                lease,
                turn,
                fallback=True,
                reason="provider_error",
                call_id=None,
                search_count=0,
            )

        if cancelled():
            await adapter.abort(lease, "cancelled")
            await _release(discard=True)
            return _cancelled_result(turn, lease)
        if _timed_out(now, budget):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                lease,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=None,
                search_count=0,
            )

        if isinstance(decision, Speak):
            await _release(discard=False)
            return _spoken_result(
                decision,
                lease,
                turn,
                fallback=False,
                reason="initial_speak",
                call_id=None,
                search_count=0,
            )

        if not isinstance(decision, ToolCall) or decision.name != WEB_SEARCH:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_MALFORMED_INITIAL),
                lease,
                turn,
                fallback=True,
                reason="malformed_decision",
                call_id=None,
                search_count=0,
            )

        # Reservasi SATU call sebelum I/O. Kegagalan tidak mengembalikan jatah.
        try:
            if allowance <= 0:
                raise ToolAllowanceExhausted("effective tool allowance is zero")
            call_id = work.reserve()
        except ToolAllowanceExhausted:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="allowance_exhausted",
                call_id=None,
                search_count=0,
            )
        accepted = decision
        spent_budget = replace(budget, tool_calls_remaining=0)

        if cancelled():
            await adapter.abort(lease, "cancelled")
            await _release(discard=True)
            return _cancelled_result(turn, lease, call_id=call_id)
        if _timed_out(now, budget):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=call_id,
                search_count=0,
            )

        async def _execute() -> ToolObservation:
            return await router.execute(
                accepted,
                turn_id=turn.turn_id,
                call_id=call_id or "",
                requester=lease,
                budget=spent_budget,
            )

        observation = await work.deliver(call_id, _execute)
        observations = (observation,)
        search_count = 1

        if cancelled():
            await adapter.abort(lease, "cancelled")
            await _release(discard=True)
            return _cancelled_result(
                turn,
                lease,
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        if _timed_out(now, budget):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )

        resume_budget = replace(
            budget, tool_calls_remaining=0, search_deadline=None
        )
        try:
            resumed = await adapter.resume(
                lease, turn, accepted, observation, resume_budget
            )
        except DecisionCodecError:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="malformed_resume",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        except BrainLeaseMismatch:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="resume_mismatch",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        except BrainTransportError:
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="resume_provider_error",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )

        if cancelled():
            await adapter.abort(lease, "cancelled")
            await _release(discard=True)
            return _cancelled_result(
                turn,
                lease,
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        if _timed_out(now, budget):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="budget_exhausted",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )

        if isinstance(resumed, ToolCall):
            await _release(discard=True)
            return _spoken_result(
                Speak(FALLBACK_CHECK_INCOMPLETE),
                lease,
                turn,
                fallback=True,
                reason="second_tool_rejected",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        if isinstance(resumed, Speak):
            await _release(discard=False)
            return _spoken_result(
                resumed,
                lease,
                turn,
                fallback=False,
                reason="resumed_speak",
                call_id=call_id,
                search_count=search_count,
                observations=observations,
            )
        await _release(discard=True)
        return _spoken_result(
            Speak(FALLBACK_CHECK_INCOMPLETE),
            lease,
            turn,
            fallback=True,
            reason="malformed_resume",
            call_id=call_id,
            search_count=search_count,
            observations=observations,
        )
    finally:
        if lease is not None and not released:
            await adapter.release(lease, discard=True)
