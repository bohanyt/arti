"""Internet fast lookup — pertanyaan yang butuh info recent dicek ke web dulu.

Fitur C rencana v0.7. Terukur di spike 2026-08-02:
  groq/compound-mini : 6,8 dtk, hasil web asli + sumber (provider UTAMA, gratis)
  cursor grok-4.5/low: 17,6 dtk dengan web search (FALLBACK)
  groq/compound besar: 413 request_too_large di free tier — jangan dipakai

Pemicu SENGAJA konservatif (latensi & kuota): hanya permintaan eksplisit
("[recorded remark omitted]") atau topik yang melekat-waktu (berita/harga/skor/...).
"[recorded remark omitted]" BUKAN pertanyaan web.

Hasil disuntik ke system prompt sebagai blok [INFO INTERNET] — dibatasi
`web_lookup_max_chars`, gagal apa pun = jawab biasa tanpa blok (jangan bisu).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from arti_brain_contract import (
    BackendResult,
    Cancellation,
    Source,
    ToolError,
    make_source,
)

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_SEARCH_ONLY_TOOLS = ("web_search",)
_FORBIDDEN_COMPOUND_TOOLS = frozenset(
    {
        "visit_website",
        "code_interpreter",
        "wolfram_alpha",
        "code_execution",
        "browser_search",
        "browser",
    }
)
_WEB_SEARCH_NAMES = frozenset({"web_search", "search"})

# Permintaan eksplisit untuk mencari
_ASK_SEARCH_RE = re.compile(
    r"(cek (di )?(internet|web|google)|googling|search(in| dulu)?\b|"
    r"cari (di )?(internet|web|google)|tolong cari)",
    re.IGNORECASE,
)
# Topik yang melekat-waktu — menjawab tanpa web hampir pasti halusinasi
_TIMEBOUND_RE = re.compile(
    r"\b(berita|harga|kurs|saham|skor|klasemen|jadwal|trending|viral|"
    r"rilis terbaru|baru rilis|update terbaru|patch terbaru|versi terbaru|"
    r"pemilu|pilpres|cuaca)\b",
    re.IGNORECASE,
)


def needs_web_lookup(text: str, config: dict | None = None) -> bool:
    """Perlu cek web? Pure — target unit test."""
    cfg = config or {}
    if not cfg.get("web_lookup_enabled", False):
        return False
    t = (text or "").strip()
    if len(t) < 8:
        return False
    return bool(_ASK_SEARCH_RE.search(t) or _TIMEBOUND_RE.search(t))


def _lookup_instruction(query: str) -> str:
    return (
        "Cari di web lalu jawab RINGKAS (maks 2 kalimat, Bahasa Indonesia) "
        "+ sebut nama sumbernya singkat. Kalau tidak ketemu, bilang tidak ketemu. "
        f"Pertanyaan: {query}"
    )


def _call_groq_compound(query: str, config: dict) -> str:
    import os  # noqa: PLC0415

    import requests  # noqa: PLC0415

    key = (config.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY kosong")
    model = str(config.get("web_lookup_groq_model", "groq/compound-mini"))
    t0 = time.monotonic()
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": _lookup_instruction(query)}],
            "max_tokens": int(config.get("web_lookup_max_tokens", 220)),
        },
        timeout=float(config.get("web_lookup_timeout_sec", 10.0)),
    )
    r.raise_for_status()
    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("jawaban kosong")
    _telemetry(config, "groq_compound", model, int((time.monotonic() - t0) * 1000))
    return text


def _call_cursor(query: str, config: dict) -> str:
    import arti_cursor_agent as ca  # noqa: PLC0415 — lazy: modul jalan tanpa SDK
    from arti_tool_router import CURSOR_FALLBACK_MIN_S

    deadline = config.get("_web_lookup_deadline")
    cancel = config.get("_web_lookup_cancel")
    if cancel is not None and cancel.is_cancelled():
        raise RuntimeError("cursor lookup cancelled")
    if deadline is not None and deadline - time.monotonic() < CURSOR_FALLBACK_MIN_S:
        raise TimeoutError("cursor lookup insufficient budget")
    r = ca.send_task("lookup", "", _lookup_instruction(query), config,
                     cancel=cancel, deadline=deadline)
    if not r.ok or not (r.text or "").strip():
        raise RuntimeError(f"cursor lookup gagal: {r.reason}")
    _telemetry(config, "cursor", r.model or "grok-4.5/low", r.latency_ms)
    return r.text.strip()


def call_codex_search(query: str, config: dict) -> BackendResult:
    """One fresh search-only role thread; failure raises for chain fallback."""
    import arti_codex_role as cr

    cfg = {**config, "codex_role": "lookup", "telemetry_subsystem": "web_lookup"}
    prompt = (
        "Cari di web sekarang. Jawab ringkas dalam Bahasa Indonesia, maks 2 kalimat. "
        "Untuk harga, sebut tanggal data dan satuannya. Kembalikan HANYA JSON: "
        '{"answer":"jawaban", "sources":[{"title":"nama sumber","url":"https://..."}]}. '
        "Gunakan 1-3 URL sumber yang benar-benar ditemukan, jangan mengarang. "
        f"Tanggal lokal: {time.strftime('%Y-%m-%d')}. Pertanyaan: {query[:500]}"
    )
    text, _ms, info = cr.call(prompt, cfg)
    body = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise RuntimeError("codex lookup empty answer")
    sources = []
    for entry in body.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        source = make_source(title=entry.get("title") or "Sumber", url=entry.get("url"))
        if source.url:
            sources.append(source)
    if not sources:
        raise RuntimeError("codex lookup missing sources")
    return BackendResult(provider="codex", status="ok", content=answer.strip(),
                         sources=tuple(sources[:3]), error=None,
                         search_performed=info["search_performed"], model=info["model"])


def _call_codex(query: str, config: dict) -> str:
    result = call_codex_search(query, config)
    names = ", ".join(source.title for source in result.sources)
    return f"{result.content} (Sumber: {names})"


async def execute_codex_search(query: str, *, deadline: float, cancel: Cancellation,
                               config: Mapping[str, Any] | None = None,
                               clock: Callable[[], float] | None = None) -> BackendResult:
    now = clock or time.monotonic
    cfg = dict(config or {})
    model = str(cfg.get("web_lookup_codex_model", "gpt-6-luna"))
    def failed(status, code):
        return BackendResult(provider="codex", status=status, content="", sources=(),
                             error=ToolError(code=code, message=code),
                             search_performed=False, model=model)
    if cancel.is_cancelled() or deadline <= now():
        return failed("timeout", "cancelled" if cancel.is_cancelled() else "deadline")
    # A local cancellation token also stops the bounded role worker on task cancellation.
    from arti_brain_contract import FlagCancellation
    stop = FlagCancellation()
    cfg.update(_web_lookup_deadline=time.monotonic() + deadline - now(),
               _web_lookup_cancel=stop)
    task = asyncio.create_task(asyncio.to_thread(call_codex_search, query, cfg))
    try:
        while not task.done():
            if cancel.is_cancelled() or now() >= deadline:
                stop.cancel()
                return failed("timeout", "cancelled" if cancel.is_cancelled() else "deadline")
            await asyncio.wait({task}, timeout=.05)
        return task.result()
    except asyncio.CancelledError:
        stop.cancel()
        raise
    except Exception as exc:
        code = "timeout" if "timeout" in str(exc) else "provider_error"
        return failed("timeout" if code == "timeout" else "error", code)
    finally:
        if not task.done():
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def _telemetry(config: dict, provider: str, model: str, ms: int) -> None:
    try:
        import arti_api_telemetry as tel  # noqa: PLC0415

        tel.record_call(subsystem="web_lookup", provider=provider, model=model,
                        latency_ms=ms, ok=True, config=config)
    except Exception:  # noqa: BLE001
        pass


_PROVIDERS = {
    "codex": _call_codex,
    "groq_compound": _call_groq_compound,
    "cursor": _call_cursor,
}


def lookup(query: str, config: dict) -> tuple[str, str] | tuple[None, str]:
    """(hasil, provider) atau (None, alasan). BLOCKING — bungkus to_thread."""
    chain = list(config.get("web_lookup_provider_chain") or ["groq_compound", "cursor"])
    config = dict(config)
    deadline = config.setdefault("_web_lookup_deadline", time.monotonic() + float(
        config.get("web_lookup_turn_budget_sec", 12.0)))
    import arti_provider_health as _ph  # noqa: PLC0415

    _boleh = _ph.usable([n for n in chain if n in _PROVIDERS],
                        lambda n: _ph.chain_key(n, config, lane="web"))

    last_err = ""
    for name in chain:
        cancel = config.get("_web_lookup_cancel")
        if (cancel is not None and cancel.is_cancelled()) or time.monotonic() >= deadline:
            return None, "cancelled or deadline"
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        hkey = _ph.chain_key(name, config, lane="web")
        if name not in _boleh:
            continue
        try:
            print(f"[WebLookup] Trying {name}...")
            attempt_config = {**config, "web_lookup_timeout_sec": min(
                float(config.get("web_lookup_timeout_sec", 10.0)),
                max(0.01, deadline - time.monotonic()))}
            text = fn(query, attempt_config)
            _ph.record_success(hkey)
            cap = int(config.get("web_lookup_max_chars", 500))
            if len(text) > cap:
                text = text[:cap].rsplit(" ", 1)[0] + "…"
            return text, name
        except Exception as e:  # noqa: BLE001
            last_err = f"{name}: {type(e).__name__}: {e}"
            print(f"[WebLookup] {last_err}"[:160])
            _ph.record_failure(hkey, _ph.classify(e))
    return None, last_err or "chain kosong"


def lookup_block(query: str, config: dict) -> str:
    """Blok siap suntik ke system prompt; string kosong kalau gagal."""
    text, src = lookup(query, config)
    if not text:
        return ""
    print(f"[WebLookup] OK via {src} ({len(text)} char)")
    return (
        "\n\n[INFO INTERNET — dicek barusan:]\n"
        f"{text}\n"
        "(Pakai kalau relevan; sebut santai mis. 'barusan aku intip sekilas'. "
        "JANGAN menyebut nama mesin/model/API.)"
    )


async def lookup_block_async(query: str, config: dict, *,
                             abort: Callable[[], bool] | None = None) -> str:
    """Bounded lookup whose worker sees cancellation before another provider."""
    from arti_brain_contract import FlagCancellation
    stop = FlagCancellation()
    budget = max(0.0, float(config.get("web_lookup_turn_budget_sec", 12.0)))
    deadline = time.monotonic() + budget
    cfg = {**config, "_web_lookup_cancel": stop, "_web_lookup_deadline": deadline}
    if abort and abort():
        raise asyncio.CancelledError()
    task = asyncio.create_task(asyncio.to_thread(lookup_block, query, cfg))
    try:
        while not task.done():
            if abort and abort():
                raise asyncio.CancelledError()
            if time.monotonic() >= deadline:
                return ""
            await asyncio.wait({task}, timeout=.05)
        if abort and abort():
            raise asyncio.CancelledError()
        return task.result()
    finally:
        stop.cancel()
        if not task.done():
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


# --- WEB-1B executor (agentic). Router tidak boleh memanggil lookup_block. ---


def groq_agentic_request_body(query: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Payload Groq Compound Mini: HANYA web_search, bukan compound besar."""
    cfg = dict(config or {})
    model = str(cfg.get("web_lookup_groq_model", "groq/compound-mini"))
    return {
        "model": model,
        "messages": [{"role": "user", "content": _lookup_instruction(query)}],
        "max_tokens": int(cfg.get("web_lookup_max_tokens", 220)),
        "compound_custom": {"tools": {"enabled_tools": list(GROQ_SEARCH_ONLY_TOOLS)}},
    }


def _tool_name(entry: Mapping[str, Any]) -> str:
    raw = (
        entry.get("name")
        or entry.get("tool_name")
        or entry.get("type")
        or ""
    )
    if isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    nested = entry.get("function")
    if isinstance(nested, Mapping):
        inner = nested.get("name")
        if isinstance(inner, str):
            return inner.strip().lower()
    return ""


def _parse_search_results(raw: object) -> list[dict[str, Any]]:
    payload: object = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
    if isinstance(payload, Mapping):
        inner = payload.get("results", payload.get("organic_results", ()))
        if isinstance(inner, list):
            payload = inner
        else:
            return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, Mapping):
            out.append(dict(item))
    return out


def _sources_from_results(results: list[dict[str, Any]]) -> tuple[Source, ...]:
    sources: list[Source] = []
    for item in results:
        title = item.get("title") or item.get("name") or ""
        url = item.get("url") or item.get("link")
        if not isinstance(title, str) or not title.strip():
            if isinstance(url, str) and url.strip():
                title = url.strip()
            else:
                continue
        url_text = url if isinstance(url, str) else None
        try:
            sources.append(make_source(title=title, url=url_text))
        except Exception:  # noqa: BLE001
            continue
    return tuple(sources)


def _snippets_from_results(results: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in results:
        snippet = item.get("content") or item.get("snippet") or item.get("text") or ""
        if isinstance(snippet, str) and snippet.strip():
            parts.append(snippet.strip())
    return "\n".join(parts)


def parse_groq_agentic_response(
    body: Mapping[str, Any],
    *,
    model: str,
) -> BackendResult:
    """Ubah JSON Groq jadi BackendResult. Tanpa bukti search -> bukan ok."""
    if not isinstance(body, Mapping):
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(code="malformed_response", message="Respons Groq bukan objek."),
            search_performed=False,
            model=model,
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(code="malformed_response", message="Respons Groq tanpa choices."),
            search_performed=False,
            model=model,
        )
    first = choices[0]
    if not isinstance(first, Mapping):
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(code="malformed_response", message="Choice Groq cacat."),
            search_performed=False,
            model=model,
        )
    message = first.get("message")
    if not isinstance(message, Mapping):
        message = {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    executed = message.get("executed_tools")
    if executed is None:
        executed = body.get("executed_tools")
    if not isinstance(executed, list):
        executed = []

    unexpected: list[str] = []
    search_entries: list[Mapping[str, Any]] = []
    for entry in executed:
        if not isinstance(entry, Mapping):
            continue
        name = _tool_name(entry)
        if name in _FORBIDDEN_COMPOUND_TOOLS:
            unexpected.append(name)
        elif name in _WEB_SEARCH_NAMES or entry.get("search_results") is not None:
            search_entries.append(entry)
        elif name:
            unexpected.append(name)

    if unexpected:
        shown = ", ".join(sorted(set(unexpected)))[:80]
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(
                code="unexpected_tool",
                message=f"Groq menjalankan tool terlarang: {shown}",
            ),
            search_performed=False,
            model=model,
        )

    results: list[dict[str, Any]] = []
    for entry in search_entries:
        results.extend(_parse_search_results(entry.get("search_results")))

    search_performed = bool(search_entries)
    if not search_performed:
        return BackendResult(
            provider="groq_compound",
            status="unavailable",
            content="",
            sources=(),
            error=ToolError(
                code="search_unconfirmed",
                message="Tidak ada bukti Groq menjalankan web_search.",
            ),
            search_performed=False,
            model=model,
        )

    sources = _sources_from_results(results)
    snippets = _snippets_from_results(results)
    body_text = (snippets or (content or "")).strip()
    if not body_text and not sources and not results:
        return BackendResult(
            provider="groq_compound",
            status="no_results",
            content="",
            sources=(),
            error=ToolError(code="no_results", message="Pencarian tidak menemukan hasil."),
            search_performed=True,
            model=model,
        )
    return BackendResult(
        provider="groq_compound",
        status="ok",
        content=body_text,
        sources=sources,
        error=None,
        search_performed=True,
        model=model,
        truncated=False,
    )


def _classify_http_error(status_code: int) -> ToolError:
    if status_code in (401, 403):
        return ToolError(code="auth", message="Otentikasi Groq ditolak.")
    if status_code == 429:
        return ToolError(code="rate_limited", message="Groq membatasi laju permintaan.")
    if 500 <= status_code <= 599:
        return ToolError(code="upstream", message=f"Groq HTTP {status_code}.")
    return ToolError(code="http_error", message=f"Groq HTTP {status_code}.")


class GroqSearchHttpError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


async def default_groq_http_post(
    url: str,
    headers: Mapping[str, str],
    json_body: Mapping[str, Any],
    timeout: float,
    *,
    cancel: Cancellation,
    clock: Callable[[], float],
) -> Mapping[str, Any]:
    """HTTP Groq dengan batas waktu. Pembatalan membuang hasil terlambat."""
    import requests  # noqa: PLC0415

    if cancel.is_cancelled():
        raise asyncio.CancelledError()
    remaining = timeout
    if remaining <= 0:
        raise TimeoutError("deadline")

    def _post() -> requests.Response:
        return requests.post(
            url,
            headers=dict(headers),
            json=dict(json_body),
            timeout=remaining,
        )

    task = asyncio.create_task(asyncio.to_thread(_post))
    poll = 0.05
    try:
        while not task.done():
            if cancel.is_cancelled():
                task.cancel()
                raise asyncio.CancelledError()
            try:
                response = await asyncio.wait_for(asyncio.shield(task), timeout=poll)
                break
            except TimeoutError:
                remaining -= poll
                if remaining <= 0:
                    task.cancel()
                    raise TimeoutError("deadline") from None
        else:
            response = task.result()
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        raise
    if response.status_code >= 400:
        raise GroqSearchHttpError(response.status_code)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("malformed_json") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("malformed_json")
    return payload


async def execute_groq_compound_search(
    query: str,
    *,
    deadline: float,
    cancel: Cancellation,
    config: Mapping[str, Any] | None = None,
    clock: Callable[[], float] | None = None,
    http_post: Callable[..., Any] | None = None,
) -> BackendResult:
    """Executor Groq terstruktur. Tidak mengubah lookup() warisan."""
    import os  # noqa: PLC0415

    now = clock or time.monotonic
    cfg = dict(config or {})
    model = str(cfg.get("web_lookup_groq_model", "groq/compound-mini"))
    if cancel.is_cancelled():
        return BackendResult(
            provider="groq_compound",
            status="timeout",
            content="",
            sources=(),
            error=ToolError(code="cancelled", message="Pencarian dibatalkan."),
            search_performed=False,
            model=model,
        )
    remaining = deadline - now()
    if remaining < 1.0:
        return BackendResult(
            provider="groq_compound",
            status="timeout",
            content="",
            sources=(),
            error=ToolError(code="deadline", message="Sisa waktu pencarian tidak cukup."),
            search_performed=False,
            model=model,
        )
    key = (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        return BackendResult(
            provider="groq_compound",
            status="unavailable",
            content="",
            sources=(),
            error=ToolError(code="missing_key", message="GROQ_API_KEY kosong."),
            search_performed=False,
            model=model,
        )
    configured = float(cfg.get("web_lookup_timeout_sec", 10.0))
    timeout = min(configured, remaining)
    payload = groq_agentic_request_body(query, cfg)
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    post = http_post or default_groq_http_post
    try:
        body = await post(
            GROQ_CHAT_URL,
            headers,
            payload,
            timeout,
            cancel=cancel,
            clock=now,
        )
    except asyncio.CancelledError:
        return BackendResult(
            provider="groq_compound",
            status="timeout",
            content="",
            sources=(),
            error=ToolError(code="cancelled", message="Pencarian dibatalkan."),
            search_performed=False,
            model=model,
        )
    except TimeoutError:
        return BackendResult(
            provider="groq_compound",
            status="timeout",
            content="",
            sources=(),
            error=ToolError(code="timeout", message="Batas waktu Groq habis."),
            search_performed=False,
            model=model,
        )
    except GroqSearchHttpError as exc:
        err = _classify_http_error(exc.status_code)
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=err,
            search_performed=False,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(code="transport", message=type(exc).__name__),
            search_performed=False,
            model=model,
        )
    if not isinstance(body, Mapping):
        return BackendResult(
            provider="groq_compound",
            status="error",
            content="",
            sources=(),
            error=ToolError(code="malformed_response", message="Respons Groq bukan objek."),
            search_performed=False,
            model=model,
        )
    return parse_groq_agentic_response(body, model=model)


def backend_result_to_legacy_text(result: BackendResult, config: Mapping[str, Any] | None = None) -> str:
    """Hanya untuk wrapper warisan. Router agentic tidak memakai ini."""
    if result.status != "ok" or not result.content:
        return ""
    cap = int((config or {}).get("web_lookup_max_chars", 500))
    text = result.content
    if len(text) > cap:
        text = text[:cap].rsplit(" ", 1)[0] + "…"
    return text
