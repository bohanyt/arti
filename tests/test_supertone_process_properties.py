"""Property-based tests for SupertoneProcess.request() id correspondence.

Uses Hypothesis to validate the request/response correlation contract of the
bridge-side subprocess lifecycle manager (`SupertoneProcess`) defined in the
Supertone 3 TTS upgrade design (see the "Subprocess Lifecycle Manager —
SupertoneProcess" section and the formal spec for `request()`).

Property 5: Request/response id correspondence
    Validates: Requirements 6.1, 6.6
    The manager assigns request ids beginning at 1 and incrementing by exactly
    1 for each subsequent request, producing a strictly increasing sequence of
    positive integers (Req 6.1). For a submitted sequence of requests the engine
    returns responses whose ids correspond to request ids in first-in-first-out
    order (Req 6.6).

Test approach
    No real subprocess is spawned (we never launch `venv312`/`supertone_engine`).
    Instead a FAKE in-process echo "subprocess" object is installed as
    ``SupertoneProcess.proc``: its ``stdin`` parses each just-written NDJSON
    request line to recover the assigned id and queues an echo response carrying
    that exact id; its ``stdout.readline()`` returns those echo lines. Because
    ``SupertoneProcess.request()`` serializes calls under an ``asyncio.Lock`` and
    runs the blocking write/read on worker threads via ``asyncio.to_thread``, the
    write always completes before the matching read — so a single shared queue is
    sufficient and thread-safe for these strictly sequential round-trips.
"""

from __future__ import annotations

import asyncio
import collections
import json

from hypothesis import given, settings, strategies as st

# Per the task: try importing the needed classes directly. Importing the bridge
# module executes module-level code (it installs a stdout/stderr tee logger and
# opens a session log) but performs no blocking work and does not touch audio
# devices at import time (that happens only when TTSEngine is constructed, which
# these tests never do). If this import ever fails in a constrained environment,
# the failure is surfaced loudly rather than silently skipped so the test still
# runs wherever the import succeeds.
from hermes_vtuber_bridge import SupertoneProcess, PROTOCOL_VERSION


class _FakeEchoSubprocess:
    """In-process stand-in for the Supertone engine subprocess handle.

    Mimics the small slice of ``subprocess.Popen`` that ``SupertoneProcess``
    touches during ``request()``:

    * ``proc.poll()``  -> ``None`` (the process is "alive").
    * ``proc.stdin.write(line)`` / ``proc.stdin.flush()`` — on write it parses
      the request line, records the assigned id, and queues an echo response
      whose ``id`` equals the request id.
    * ``proc.stdout.readline()`` — pops and returns the next queued echo line
      (or ``""`` to signal EOF if none is pending, matching a dead subprocess).
    """

    def __init__(self):
        self._pending: "collections.deque[str]" = collections.deque()
        self.written_ids: list = []
        self.stdin = self._Stdin(self)
        self.stdout = self._Stdout(self)

    def poll(self):
        # ``None`` means the OS process is still running (no exit status).
        return None

    class _Stdin:
        def __init__(self, parent: "_FakeEchoSubprocess"):
            self._parent = parent

        def write(self, line: str) -> None:
            # The bridge writes exactly one compact JSON object per line. Parse
            # it, recover the id the manager just assigned, and queue an echo
            # response carrying that exact id (the simplest correct echo).
            req = json.loads(line)
            rid = req.get("id")
            self._parent.written_ids.append(rid)
            resp = {
                "v": PROTOCOL_VERSION,
                "id": rid,
                "ok": True,
                "wav_path": f"/tmp/supertone_tmp/temp_{rid}.wav",
                "duration": 1.0,
                "engine": "supertonic-3",
            }
            self._parent._pending.append(json.dumps(resp) + "\n")

        def flush(self) -> None:
            pass

    class _Stdout:
        def __init__(self, parent: "_FakeEchoSubprocess"):
            self._parent = parent

        def readline(self) -> str:
            if self._parent._pending:
                return self._parent._pending.popleft()
            return ""  # EOF (would surface as a SupertoneError in the manager)


# Arbitrary request text payloads, including non-ASCII characters and expression
# tags, to confirm id correspondence is independent of the carried payload.
# ``codec="utf-8"`` excludes lone surrogates that cannot round-trip through the
# UTF-8 stdin/stdout pipes.
_request_text = st.text(
    alphabet=st.characters(codec="utf-8"),
    max_size=40,
)


async def _drive_requests(payloads: list[str]) -> None:
    """Submit ``payloads`` through a manager backed by the fake echo subprocess.

    Constructs the ``SupertoneProcess`` inside the running event loop so its
    ``asyncio.Lock`` lives on this loop, marks it ready, installs the fake
    subprocess, then issues one ``request()`` per payload and asserts the
    id-correspondence property after each call and over the whole sequence.
    """
    p = SupertoneProcess()
    p._ready = True
    fake = _FakeEchoSubprocess()
    p.proc = fake

    returned_ids: list = []
    for text in payloads:
        req = {"type": "synthesize", "text": text}
        resp = await p.request(req, timeout=5.0)

        # (a) The response id returned for this call matches the request id the
        #     manager assigned to it (mutated into ``req`` in place). Req 6.6;
        #     a mismatch would have raised a DESYNC SupertoneError instead.
        assert resp["id"] == req["id"], (
            f"response id {resp['id']!r} != assigned request id {req['id']!r}"
        )
        returned_ids.append(resp["id"])

    n = len(payloads)
    expected = list(range(1, n + 1))

    # (b) Ids are strictly increasing positive integers starting at 1 (Req 6.1).
    assert returned_ids == expected, (
        f"ids not 1..{n} strictly increasing: {returned_ids!r}"
    )
    # All ids are positive integers (guards against e.g. 0 or non-int ids).
    assert all(isinstance(i, int) and i >= 1 for i in returned_ids)
    assert all(b - a == 1 for a, b in zip(returned_ids, returned_ids[1:]))

    # (c) Responses correspond FIFO: the order ids were written to the engine
    #     equals the order responses (by id) came back (Req 6.6).
    assert fake.written_ids == expected
    assert returned_ids == fake.written_ids


@settings(max_examples=200, deadline=None)
@given(st.lists(_request_text, min_size=1, max_size=25))
def test_request_response_id_correspondence(payloads):
    """Request ids are strictly increasing from 1 and responses match FIFO.

    **Validates: Requirements 6.1, 6.6**
    """
    asyncio.run(_drive_requests(payloads))
