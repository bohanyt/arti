"""WEB-1B: executor Groq terstruktur. HTTP palsu; tanpa jaringan/kunci asli."""

from __future__ import annotations

import ast
import asyncio
import builtins
import os
import socket as socketmod
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_brain_contract import FlagCancellation, ToolError  # noqa: E402
from arti_web_lookup import (  # noqa: E402
    GROQ_CHAT_URL,
    GROQ_SEARCH_ONLY_TOOLS,
    GroqSearchHttpError,
    execute_groq_compound_search,
    groq_agentic_request_body,
    lookup,
    lookup_block,
    parse_groq_agentic_response,
)

pytestmark = pytest.mark.timeout(30)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(address) -> bool:
    return (
        isinstance(address, tuple)
        and len(address) >= 1
        and isinstance(address[0], str)
        and address[0] in _LOOPBACK_HOSTS
    )


class _GuardedSocket(socketmod.socket):
    def connect(self, address):
        if not _is_loopback(address):
            pytest.fail(f"WEB-1B test attempted real network connect {address!r}")
        return super().connect(address)

    def connect_ex(self, address):
        if not _is_loopback(address):
            pytest.fail(f"WEB-1B test attempted real network connect_ex {address!r}")
        return super().connect_ex(address)


@pytest.fixture(autouse=True)
def _web1b_side_channel_guards(monkeypatch):
    def _fail_proc(*_a, **_k):
        pytest.fail("WEB-1B test attempted subprocess/provider launch")

    monkeypatch.setattr(socketmod, "socket", _GuardedSocket)
    monkeypatch.setattr(
        socketmod,
        "create_connection",
        lambda *_a, **_k: pytest.fail("WEB-1B test attempted socket.create_connection"),
    )
    monkeypatch.setattr(
        socketmod,
        "gethostbyname",
        lambda *_a, **_k: pytest.fail("WEB-1B test attempted DNS resolution"),
    )
    monkeypatch.setattr(subprocess, "Popen", _fail_proc)
    monkeypatch.setattr(subprocess, "run", _fail_proc)
    monkeypatch.setattr(subprocess, "call", _fail_proc)
    monkeypatch.setattr(os, "system", lambda *_a, **_k: pytest.fail("os.system"))
    real_open = builtins.open

    def _guarded_open(file, mode="r", *args, **kwargs):
        path = os.fspath(file).replace("\\", "/")
        if path.endswith("config_local.json"):
            pytest.fail("WEB-1B must not read config_local.json")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _guarded_open)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _ok_body(*, content="Versi 1.2.3 dari situs resmi.", title="Situs Resmi", url="https://example.com/v"):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "executed_tools": [
                        {
                            "name": "web_search",
                            "search_results": {
                                "results": [
                                    {
                                        "title": title,
                                        "url": url,
                                        "content": content,
                                        "score": 0.9,
                                    }
                                ]
                            },
                        }
                    ],
                }
            }
        ]
    }


def test_agentic_payload_is_search_only_compound_mini():
    body = groq_agentic_request_body("versi stabil sekarang", {})
    assert body["model"] == "groq/compound-mini"
    assert body["compound_custom"]["tools"]["enabled_tools"] == list(GROQ_SEARCH_ONLY_TOOLS)
    assert "tavily" not in str(body).lower()
    assert body["model"] != "groq/compound"


def test_parse_ok_requires_search_evidence_and_keeps_sources():
    result = parse_groq_agentic_response(_ok_body(), model="groq/compound-mini")
    assert result.status == "ok"
    assert result.search_performed is True
    assert result.provider == "groq_compound"
    assert result.sources[0].url == "https://example.com/v"
    assert "1.2.3" in result.content


def test_fluent_text_without_executed_tools_is_unconfirmed():
    body = {"choices": [{"message": {"content": "Menurutku versi terbaru 9.9.9."}}]}
    result = parse_groq_agentic_response(body, model="groq/compound-mini")
    assert result.status == "unavailable"
    assert result.search_performed is False
    assert result.error is not None
    assert result.error.code == "search_unconfirmed"
    assert result.content == ""


def test_unexpected_compound_tool_is_rejected():
    body = {
        "choices": [
            {
                "message": {
                    "content": "hasil",
                    "executed_tools": [{"name": "code_interpreter", "output": "1+1=2"}],
                }
            }
        ]
    }
    result = parse_groq_agentic_response(body, model="groq/compound-mini")
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "unexpected_tool"
    assert result.search_performed is False


def test_empty_search_results_are_no_results():
    body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "executed_tools": [{"name": "web_search", "search_results": {"results": []}}],
                }
            }
        ]
    }
    result = parse_groq_agentic_response(body, model="groq/compound-mini")
    assert result.status == "no_results"
    assert result.search_performed is True
    assert result.content == ""


def test_malformed_body_is_error():
    result = parse_groq_agentic_response({"choices": []}, model="groq/compound-mini")
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == "malformed_response"


def test_http_status_matrix():
    async def _run(code: int):
        async def boom(*_a, **_k):
            raise GroqSearchHttpError(code)

        return await execute_groq_compound_search(
            "q",
            deadline=1_020.0,
            cancel=FlagCancellation(),
            config={"groq_api_key": "gsk_testkey_not_real_0000"},
            clock=FakeClock(),
            http_post=boom,
        )

    auth = asyncio.run(_run(401))
    assert auth.status == "error" and auth.error.code == "auth"
    limited = asyncio.run(_run(429))
    assert limited.error.code == "rate_limited"
    upstream = asyncio.run(_run(503))
    assert upstream.error.code == "upstream"


def test_executor_records_search_only_request_and_does_not_hit_network():
    captured: list[dict] = []

    async def fake_post(url, headers, json_body, timeout, *, cancel, clock):
        captured.append(
            {"url": url, "headers": dict(headers), "json": json_body, "timeout": timeout}
        )
        return _ok_body()

    result = asyncio.run(
        execute_groq_compound_search(
            "versi stabil sekarang yang mana",
            deadline=1_020.0,
            cancel=FlagCancellation(),
            config={"groq_api_key": "gsk_testkey_not_real_0000"},
            clock=FakeClock(),
            http_post=fake_post,
        )
    )
    assert result.status == "ok"
    assert captured[0]["url"] == GROQ_CHAT_URL
    assert captured[0]["json"]["compound_custom"]["tools"]["enabled_tools"] == ["web_search"]
    assert "gsk_testkey" in captured[0]["headers"]["Authorization"]


def test_cancel_before_http_does_not_start_post():
    calls = []

    async def fake_post(*_a, **_k):
        calls.append(1)
        raise AssertionError("should not run")

    cancel = FlagCancellation()
    cancel.cancel()
    result = asyncio.run(
        execute_groq_compound_search(
            "q",
            deadline=1_020.0,
            cancel=cancel,
            config={"groq_api_key": "gsk_testkey_not_real_0000"},
            clock=FakeClock(),
            http_post=fake_post,
        )
    )
    assert result.status == "timeout"
    assert result.error.code == "cancelled"
    assert calls == []


def test_insufficient_deadline_skips_http():
    calls = []

    async def fake_post(*_a, **_k):
        calls.append(1)
        raise AssertionError("should not run")

    clock = FakeClock(1_000.0)
    result = asyncio.run(
        execute_groq_compound_search(
            "q",
            deadline=1_000.4,
            cancel=FlagCancellation(),
            config={"groq_api_key": "gsk_testkey_not_real_0000"},
            clock=clock,
            http_post=fake_post,
        )
    )
    assert result.status == "timeout"
    assert calls == []


def test_cancel_during_hanging_http_stops_waiting():
    async def hanging(url, headers, json_body, timeout, *, cancel, clock):
        while not cancel.is_cancelled():
            await asyncio.sleep(0)
            if hasattr(clock, "advance"):
                clock.advance(0.05)
        raise asyncio.CancelledError()

    cancel = FlagCancellation()

    async def scenario():
        task = asyncio.create_task(
            execute_groq_compound_search(
                "q",
                deadline=1_020.0,
                cancel=cancel,
                config={"groq_api_key": "gsk_testkey_not_real_0000"},
                clock=FakeClock(),
                http_post=hanging,
            )
        )
        await asyncio.sleep(0)
        cancel.cancel()
        return await task

    result = asyncio.run(scenario())
    assert result.status == "timeout"
    assert result.error.code == "cancelled"


def test_credentialed_source_url_is_dropped():
    body = _ok_body(url="https://user:pass@example.com/secret")
    result = parse_groq_agentic_response(body, model="groq/compound-mini")
    assert result.status == "ok"
    assert result.sources[0].url is None


def test_legacy_lookup_wrappers_unchanged(monkeypatch):
    import arti_web_lookup as wl

    monkeypatch.setitem(wl._PROVIDERS, "groq_compound", lambda q, c: "hasil lama")
    text, src = lookup("berita hari ini", {"web_lookup_enabled": True})
    assert text == "hasil lama"
    assert src == "groq_compound"
    assert "[INFO INTERNET" in lookup_block("berita hari ini", {"web_lookup_enabled": True})


def test_legacy_groq_caller_does_not_send_compound_custom():
    src = (ROOT / "arti_web_lookup.py").read_text(encoding="utf-8")
    fn_start = src.index("def _call_groq_compound")
    fn_end = src.index("\n\ndef _call_cursor")
    legacy = src[fn_start:fn_end]
    assert "compound_custom" not in legacy
    assert "groq_agentic_request_body" not in legacy


def test_no_tavily_backend_code():
    for filename in ("arti_web_lookup.py", "arti_web_backends.py", "arti_tool_router.py"):
        tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "tavily" not in imported
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "TavilyWebSearchBackend" not in text
        assert "class Tavily" not in text
