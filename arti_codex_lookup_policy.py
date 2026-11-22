"""Fail-closed inspection of Codex's actual model-visible lookup tool catalog.

The same app-server builds a disposable thread against an authless loopback
provider. Its first request is captured and rejected before any inference or
tool execution. No lookup query, credentials, or prompt body is retained.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from typing import Any
import uuid


class LookupToolPolicyError(RuntimeError):
    pass


# Audited 0.163 hosted-web inputs. A future local-file/command input, or a
# different tool schema, must fail startup rather than silently broaden access.
WEB_PARAMETER_NAMES = frozenset({
    "click", "finance", "find", "image_query", "open", "response_length",
    "screenshot", "search_query", "sports", "time", "weather",
})


@dataclass(frozen=True)
class LookupToolPolicy:
    tool_names: tuple[str, ...]
    schema_sha256: str
    parameter_names: tuple[str, ...]


def validate_lookup_tools(tools: Any) -> LookupToolPolicy:
    """Accept exactly the native web namespace with its single run function."""
    if not isinstance(tools, list) or len(tools) != 1:
        raise LookupToolPolicyError("lookup requires exactly one web tool namespace")
    namespace = tools[0]
    if not isinstance(namespace, dict) or namespace.get("type") != "namespace" or namespace.get("name") != "web":
        raise LookupToolPolicyError("lookup has a non-web tool namespace")
    functions = namespace.get("tools")
    if not isinstance(functions, list) or len(functions) != 1:
        raise LookupToolPolicyError("lookup has additional or missing web functions")
    function = functions[0]
    if not isinstance(function, dict) or function.get("type") != "function" or function.get("name") != "run":
        raise LookupToolPolicyError("lookup has an unexpected web function")
    parameters = function.get("parameters")
    if not isinstance(parameters, dict) or not isinstance(parameters.get("properties"), dict):
        raise LookupToolPolicyError("lookup web tool schema is missing")
    names = set(parameters["properties"])
    if not {"search_query", "open"} <= names or not names <= WEB_PARAMETER_NAMES:
        raise LookupToolPolicyError("lookup web tool has unrecognized capabilities")
    if parameters.get("additionalProperties") is True:
        raise LookupToolPolicyError("lookup web tool accepts arbitrary capabilities")
    digest = hashlib.sha256(json.dumps(tools, sort_keys=True).encode("utf-8")).hexdigest()
    return LookupToolPolicy(("web.run",), digest, tuple(sorted(parameters["properties"])))


class LookupToolInspector:
    """One bounded, thread-local self-check before publishing a lookup client."""

    def __init__(self, model: str, *, timeout_sec: float = 3.0):
        self.model = model
        self.timeout_sec = max(0.01, float(timeout_sec))
        self.provider_id = "arti-lookup-check-" + uuid.uuid4().hex
        self._path = "/" + uuid.uuid4().hex + "/responses"
        self._ready = threading.Event()
        self._tools: Any = None
        self._error: str | None = None
        self.server = None
        self._server_thread = None

    def __enter__(self):
        owner = self
        class Capture(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.connection.settimeout(1.0)
                try:
                    if self.path != owner._path:
                        self.send_error(404)
                        return
                    if owner._ready.is_set():
                        self.send_error(409)
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4 * 1024 * 1024:
                        raise ValueError("capture size")
                    body = json.loads(self.rfile.read(length))
                    if body.get("model") != owner.model:
                        raise ValueError("capture model mismatch")
                    owner._tools = body.get("tools")
                except Exception:
                    owner._error = "invalid lookup self-check request"
                finally:
                    if self.path == owner._path:
                        owner._ready.set()
                response = b'{"error":{"message":"lookup policy inspection complete","type":"invalid_request_error"}}'
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
        self.server.daemon_threads = True
        self._server_thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=.05),
            daemon=True, name="codex-lookup-policy-http")
        self._server_thread.start()
        return self

    def provider_overrides(self) -> tuple[str, ...]:
        prefix = "model_providers." + self.provider_id + "."
        base_url = f"http://127.0.0.1:{self.server.server_port}{self._path.removesuffix('/responses')}"
        return (
            prefix + 'name="ARTI local lookup tool check"',
            prefix + "base_url=" + json.dumps(base_url),
            prefix + 'wire_api="responses"',
            prefix + "requires_openai_auth=false",
            prefix + "supports_standalone_web_search=true",
            prefix + "supports_websockets=false",
            prefix + "request_max_retries=0",
        )

    def inspect(self, client, *, approval_mode, sandbox, cwd: str,
                base_instructions: str, developer_instructions: str) -> LookupToolPolicy:
        finished = threading.Event()
        def check():
            try:
                thread = client.thread_start(
                    model=self.model, model_provider=self.provider_id,
                    cwd=cwd, ephemeral=True, config={"web_search": "live"},
                    approval_mode=approval_mode, sandbox=sandbox,
                    base_instructions=base_instructions,
                    developer_instructions=developer_instructions)
                thread.run("Lookup tool isolation check. Do not invoke tools.",
                           model=self.model, effort="low")
            except Exception:
                # HTTP 400 is deliberate. Only the captured catalog is proof.
                pass
            finally:
                finished.set()
        worker = threading.Thread(target=check, daemon=True, name="codex-lookup-policy-check")
        worker.start()
        deadline = time.monotonic() + self.timeout_sec
        while not self._ready.wait(.01):
            if finished.is_set() or time.monotonic() >= deadline:
                raise LookupToolPolicyError("lookup startup self-check produced no tool catalog")
        if self._error:
            raise LookupToolPolicyError(self._error)
        policy = validate_lookup_tools(self._tools)
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if worker.is_alive():
            raise LookupToolPolicyError("lookup startup self-check did not finish")
        return policy

    def __exit__(self, *_args):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=1.0)
