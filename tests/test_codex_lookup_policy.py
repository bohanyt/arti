"""Startup catalog checks use fake inference and loopback HTTP only."""
import copy
from http.client import HTTPConnection
import json
import threading
from types import SimpleNamespace as NS

import pytest

from arti_codex_lookup_policy import (
    LookupToolInspector, LookupToolPolicyError, validate_lookup_tools,
)


WEB = [{"type": "namespace", "name": "web", "tools": [{
    "type": "function", "name": "run", "parameters": {
        "type": "object", "properties": {"search_query": {"type": "array"},
                                            "open": {"type": "array"}}}}]}]
UI = {"type": "function", "name": "request_user_input", "parameters": {}}


def test_only_native_web_run_is_accepted():
    result = validate_lookup_tools(WEB)
    assert result.tool_names == ("web.run",)
    assert result.parameter_names == ("open", "search_query")
    assert len(result.schema_sha256) == 64


@pytest.mark.parametrize("tools", [None, {}, [], WEB + [UI], WEB + WEB,
    [{"type": "function", "name": "web.run", "parameters": {}}],
    [{"type": "namespace", "name": "mcp", "tools": WEB[0]["tools"]}],
    [{"type": "namespace", "name": "web", "tools": WEB[0]["tools"] + [UI]}],
    [{"type": "namespace", "name": "web", "tools": [{"type": "custom", "name": "run"}]}],
    [{"type": "namespace", "name": "web", "tools": [{"type": "function", "name": "run"}]}],
])
def test_extra_missing_or_unknown_tools_fail_closed(tools):
    with pytest.raises(LookupToolPolicyError):
        validate_lookup_tools(copy.deepcopy(tools))


def fake_client(inspector, tools, *, model="gpt-6-luna", send=True):
    starts = []
    def thread_start(**kwargs):
        starts.append(kwargs)
        def run(prompt, **kw):
            if send:
                conn = HTTPConnection("127.0.0.1", inspector.server.server_port, timeout=1)
                try:
                    body = json.dumps({"model": model, "tools": tools,
                                       "input": "synthetic prompt that must not be retained"})
                    conn.request("POST", inspector._path, body, {"Content-Type": "application/json"})
                    response = conn.getresponse()
                    assert response.status == 400
                    response.read()
                finally:
                    conn.close()
            raise RuntimeError("deliberate inspection rejection")
        return NS(run=run)
    return NS(thread_start=thread_start, starts=starts)


def inspect(inspector, client):
    return inspector.inspect(client, approval_mode="deny-all", sandbox="read-only", cwd="scratch",
                             base_instructions="base", developer_instructions="lookup")


def test_startup_check_captures_catalog_and_uses_disposable_thread():
    with LookupToolInspector("gpt-6-luna", timeout_sec=1) as inspector:
        client = fake_client(inspector, WEB)
        result = inspect(inspector, client)
        assert result.tool_names == ("web.run",)
        start, = client.starts
        assert start["model_provider"] == inspector.provider_id
        assert start["ephemeral"] and start["sandbox"] == "read-only"
        assert start["approval_mode"] == "deny-all"
        overrides = inspector.provider_overrides()
        assert any("requires_openai_auth=false" in s for s in overrides)
        assert any("supports_websockets=false" in s for s in overrides)
        assert any("127.0.0.1" in s for s in overrides)
        assert inspector._tools == WEB  # no prompt or headers retained


@pytest.mark.parametrize("tools", [WEB + [UI], [], None])
def test_startup_refuses_an_unsafe_actual_catalog(tools):
    with LookupToolInspector("gpt-6-luna", timeout_sec=1) as inspector:
        with pytest.raises(LookupToolPolicyError):
            inspect(inspector, fake_client(inspector, tools))


def test_no_catalog_is_not_proof_even_if_client_returns():
    with LookupToolInspector("gpt-6-luna", timeout_sec=.1) as inspector:
        with pytest.raises(LookupToolPolicyError, match="no tool catalog"):
            inspect(inspector, fake_client(inspector, WEB, send=False))


def test_catalog_from_a_different_model_is_rejected():
    with LookupToolInspector("gpt-6-luna", timeout_sec=1) as inspector:
        with pytest.raises(LookupToolPolicyError, match="invalid"):
            inspect(inspector, fake_client(inspector, WEB, model="other-model"))


def test_hung_check_has_a_bounded_timeout():
    release = threading.Event()
    finished = threading.Event()
    def run(*args, **kw):
        try:
            release.wait(2)
        finally:
            finished.set()
    client = NS(thread_start=lambda **kw: NS(run=run))
    try:
        with LookupToolInspector("gpt-6-luna", timeout_sec=.05) as inspector:
            with pytest.raises(LookupToolPolicyError, match="no tool catalog"):
                inspect(inspector, client)
    finally:
        release.set()
        assert finished.wait(1)


@pytest.mark.parametrize("field", ["file_path", "command", "script", "upload", "unknown_future_capability"])
def test_new_web_capabilities_fail_startup(field):
    tools = copy.deepcopy(WEB)
    tools[0]["tools"][0]["parameters"]["properties"][field] = {"type": "string"}
    with pytest.raises(LookupToolPolicyError, match="unrecognized capabilities"):
        validate_lookup_tools(tools)


def test_arbitrary_web_arguments_fail_startup():
    tools = copy.deepcopy(WEB)
    tools[0]["tools"][0]["parameters"]["additionalProperties"] = True
    with pytest.raises(LookupToolPolicyError, match="arbitrary capabilities"):
        validate_lookup_tools(tools)
