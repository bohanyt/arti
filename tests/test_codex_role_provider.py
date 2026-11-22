"""Codex as a stateless background-role provider (arti_codex_role) — fake SDK only."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_codex_agent as voice  # noqa: E402
import arti_codex_role as cr  # noqa: E402
import arti_observer_client as oc  # noqa: E402
import arti_scouter_client as sc  # noqa: E402


def _scout_json() -> str:
    return json.dumps({
        "summary": "Streamer dan chat ngobrol soal panen.", "emotion": "senang",
        "topic": "panen", "important_facts": [], "screen_relevant": False,
        "screen_hint": None, "curious_worthy": False, "curious_hook": None,
    })


class _Result:
    def __init__(self, text):
        self.final_response = text
        self.status = "completed"
        self.items = []
        self.usage = None


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # callable(prompt) -> text | raises | sleeps
        self.thread_starts: list[dict] = []
        self.runs: list[dict] = []
        self.threads: list[object] = []
        self.closed = False

    def thread_start(self, **kw):
        self.thread_starts.append(kw)
        client = self

        class _Thread:
            def run(self_inner, prompt, **kw2):
                client.runs.append({"thread": id(self_inner), "prompt": prompt, **kw2})
                return _Result(client.behaviour(prompt))

        th = _Thread()
        self.threads.append(th)
        return th

    def close(self):
        self.closed = True


@pytest.fixture
def fake(monkeypatch):
    holder: dict = {"behaviour": lambda p: _scout_json(), "clients": []}

    def factory(*, codex_bin, cwd):
        c = FakeClient(lambda p: holder["behaviour"](p))
        holder["clients"].append(c)
        return c

    cr.shutdown()
    cr.reset_breakers()
    monkeypatch.setattr(cr, "_client_factory", factory)
    yield holder
    cr.shutdown()
    cr.reset_breakers()


BASE = {
    "codex_agent_enabled": True,
    "telemetry_enabled": False,
    "nvidia_api_key": "test-nvidia",
    "openrouter_api_key": "test-or",
}


def test_provider_registered():
    assert "codex" in sc._PROVIDERS
    assert "codex" not in sc.PROVIDER_PENSIUN


def test_resolve_chain_skips_codex_when_disabled(fake):
    cfg = {**BASE, "codex_agent_enabled": False,
           "scouter_provider_chain": ["nvidia", "codex"]}
    assert sc._resolve_chain(cfg) == ["nvidia"]
    cfg2 = {**BASE, "scouter_provider_chain": ["nvidia", "codex"]}
    del cfg2["codex_agent_enabled"]  # default = off
    assert "codex" not in sc._resolve_chain(cfg2)


@pytest.mark.parametrize("chain", [
    ["codex", "nvidia", "openrouter"],
    ["nvidia", "openrouter", "codex"],
])
def test_resolve_chain_keeps_codex_last(fake, chain):
    cfg = {**BASE, "scouter_provider_chain": chain}
    assert sc._resolve_chain(cfg) == [n for n in chain if n != "codex"] + ["codex"]


def test_disabled_codex_raises_at_dispatch_gate(fake):
    # observer dispatches via _PROVIDERS without _resolve_chain -> callee gates.
    with pytest.raises(cr.CodexRoleError):
        sc._PROVIDERS["codex"]("p", {**BASE, "codex_agent_enabled": False})
    assert fake["clients"] == []


def test_stateless_call_never_touches_voice_thread(fake, monkeypatch):
    sentinel_client, sentinel_thread = object(), object()
    monkeypatch.setattr(voice, "_client", sentinel_client)
    monkeypatch.setattr(voice, "_thread", sentinel_thread)
    monkeypatch.setattr(voice, "_turns", 5)

    def _boom(*a, **k):
        raise AssertionError("voice path used")

    monkeypatch.setattr(voice, "send_turn", _boom)
    monkeypatch.setattr(voice, "prewarm", _boom)

    t1, _ms, info = cr.call("prompt satu", BASE)
    t2, _ms, _ = cr.call("prompt dua", BASE)
    assert t1 and t2
    assert voice._client is sentinel_client and voice._thread is sentinel_thread
    assert voice._turns == 5
    (client,) = fake["clients"]           # one app-server, reused
    assert len(client.thread_starts) == 2  # but a FRESH thread per call
    assert all(kw["ephemeral"] is True for kw in client.thread_starts)
    assert client.threads[0] is not client.threads[1]
    assert client.runs[0]["thread"] != client.runs[1]["thread"]
    assert client.runs[1]["prompt"] == "prompt dua"  # no history carried over
    assert info["model"] == "gpt-6-luna" and info["effort"] == "low"
    assert client.runs[0]["model"] == "gpt-6-luna" and client.runs[0]["effort"] == "low"
    assert "service_tier" not in client.runs[0]  # no tier by default
    assert "service_tier" not in client.thread_starts[0]


def test_config_keys_override(fake):
    cfg = {**BASE, "codex_role_model": "gpt-x", "codex_role_effort": "medium",
           "codex_role_service_tier": "priority"}
    cr.call("p", cfg)
    run = fake["clients"][0].runs[0]
    assert (run["model"], run["effort"], run["service_tier"]) == ("gpt-x", "medium", "priority")


def test_role_and_timeout_defaults():
    assert cr.role_for({}) == "scout"
    assert cr.role_for({"cursor_role": "observer"}) == "observer"
    assert cr.role_for({"cursor_role": "catchup"}) == "catchup"
    assert cr.role_for({"cursor_role": "observer", "telemetry_subsystem": "diary"}) == "diary"
    assert cr.role_timeout_sec("scout", {}) == 20.0
    assert cr.role_timeout_sec("observer", {}) == 45.0
    assert cr.role_timeout_sec("diary", {}) == 90.0
    assert cr.role_timeout_sec("scout", {"codex_role_timeout_sec": 7}) == 7.0


def test_timeout_falls_through_to_next_provider(fake, monkeypatch):
    release = threading.Event()
    fake["behaviour"] = lambda p: (release.wait(5), _scout_json())[1]
    calls = []

    def fake_nvidia(prompt, config):
        calls.append("nvidia")
        return _scout_json(), 5

    monkeypatch.setitem(sc._PROVIDERS, "nvidia", fake_nvidia)
    monkeypatch.setattr(sc, "_resolve_chain", lambda cfg: ["codex", "nvidia"])
    cfg = {**BASE, "scouter_provider_chain": ["codex", "nvidia"],
           "codex_role_timeout_sec": 0.2}
    try:
        result = sc.run_chain("streamer: halo", cfg)
    finally:
        release.set()
    assert result is not None and result.provider == "nvidia"
    assert calls == ["nvidia"]
    assert cr._client is None  # hung client discarded (closed in a daemon thread)
    deadline = time.monotonic() + 2
    while not fake["clients"][0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake["clients"][0].closed
    assert cr.breaker_state()["scout"]["failures"] == 1


def test_error_falls_through_to_next_provider(fake, monkeypatch):
    def boom(p):
        raise RuntimeError("usage limit")

    fake["behaviour"] = boom
    monkeypatch.setitem(sc._PROVIDERS, "nvidia", lambda p, c: (_scout_json(), 5))
    result = sc.run_chain("streamer: halo", {**BASE, "scouter_provider_chain": ["codex", "nvidia"]})
    assert result.provider == "nvidia"


def test_all_free_providers_fail_ends_on_codex(fake, monkeypatch):
    calls = []

    def failing(name):
        def fn(prompt, config):
            calls.append(name)
            raise RuntimeError(f"{name} HTTP 503")
        return fn

    for name in ("nvidia", "openrouter"):
        monkeypatch.setitem(sc._PROVIDERS, name, failing(name))
    cfg = {**BASE, "scouter_provider_chain": ["nvidia", "openrouter", "codex"]}
    result = sc.run_chain("streamer: halo\nchat: semangat", cfg)
    assert calls == ["nvidia", "openrouter"]
    assert result is not None and result.provider == "codex"
    assert len(fake["clients"][0].runs) == 1


def test_breaker_opens_and_reopens(fake, monkeypatch):
    def boom(p):
        raise RuntimeError("503")

    fake["behaviour"] = boom
    cfg = {**BASE, "codex_role_breaker_cooldown_sec": 600}
    for _ in range(3):
        with pytest.raises(cr.CodexRoleError):
            cr.call("p", cfg)
    assert cr.breaker_state()["scout"]["open"] is True
    assert cr.is_available(cfg) is False
    assert sc._resolve_chain({**cfg, "scouter_provider_chain": ["codex", "nvidia"]}) == ["nvidia"]
    n_clients = len(fake["clients"])
    with pytest.raises(cr.CodexRoleError, match="breaker open"):
        cr.call("p", cfg)
    assert len(fake["clients"]) == n_clients  # no SDK work while open
    # other roles are independent
    assert cr.is_available({**cfg, "cursor_role": "observer"}) is True

    cr._breakers["scout"]["opened_at"] -= 601  # cooldown elapsed
    fake["behaviour"] = lambda p: _scout_json()
    assert cr.is_available(cfg) is True
    text, _ms, _info = cr.call("p", cfg)
    assert text
    assert cr.breaker_state()["scout"] == {"failures": 0, "open": False, "opened_at": 0.0}


def test_observer_client_with_codex_first(fake, monkeypatch):
    monkeypatch.setitem(sc._PROVIDERS, "nvidia", lambda p, c: (_ for _ in ()).throw(AssertionError))
    obs_json = json.dumps({"summary": "Segmen panen.", "topics": ["panen"], "facts": [],
                           "worth_embed": True, "worth_learning": False, "noise_level": "low"})
    fake["behaviour"] = lambda p: obs_json
    data = oc.summarize_segment("streamer: panen dulu", {**BASE, "observer_provider_chain": ["codex", "nvidia"]})
    assert data["provider"] == "codex" and data["summary"] == "Segmen panen."
    assert "observer" in cr.breaker_state()


def test_observer_client_codex_disabled_falls_through(fake, monkeypatch):
    monkeypatch.setitem(sc._PROVIDERS, "nvidia", lambda p, c: ('{"summary": "dari nvidia"}', 5))
    data = oc.summarize_segment(
        "streamer: panen", {**BASE, "codex_agent_enabled": False,
                            "observer_provider_chain": ["codex", "nvidia"]})
    assert data["provider"] == "nvidia"
    assert fake["clients"] == []


def test_observer_client_codex_last(fake, monkeypatch):
    def fail(p, c):
        raise RuntimeError("HTTP 429")

    monkeypatch.setitem(sc._PROVIDERS, "nvidia", fail)
    fake["behaviour"] = lambda p: '{"summary": "dari codex"}'
    data = oc.summarize_segment("streamer: x", {**BASE, "observer_provider_chain": ["nvidia", "codex"]})
    assert data["provider"] == "codex"


def _load_diary():
    script = ROOT / "scripts" / "arti_diary_postlive.py"
    name = "arti_diary_postlive"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_diary_chain_picks_up_codex(fake, monkeypatch):
    D = _load_diary()

    def fail(p, c):
        raise RuntimeError("HTTP 503")

    monkeypatch.setitem(sc._PROVIDERS, "nvidia", fail)
    fake["behaviour"] = lambda p: '{"summary": "diary", "entries": []}'
    raw, provider = D.call_llm_chain("p", {**BASE, "observer_provider_chain": ["nvidia", "codex"]})
    assert provider == "codex" and json.loads(raw)["summary"] == "diary"
    assert "diary" in cr.breaker_state()  # own role: 90 s fence, own breaker
    # disabled -> resolved away, never called
    monkeypatch.setitem(sc._PROVIDERS, "nvidia", lambda p, c: ('{"summary": "n"}', 1))
    _raw, provider = D.call_llm_chain(
        "p", {**BASE, "codex_agent_enabled": False, "observer_provider_chain": ["codex", "nvidia"]})
    assert provider == "nvidia"


def test_real_factory_refuses_under_pytest():
    with pytest.raises(cr.CodexRoleError, match="pytest"):
        cr._real_factory(codex_bin="codex", cwd=".")


# --------------------------------------------------------------------------- #
# Review 8584c33d fixes
# --------------------------------------------------------------------------- #

def _wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_startup_hang_never_orphans_client(monkeypatch):
    """HIGH: factory() hangs past the timeout -> the stale worker must close
    the client it eventually creates instead of publishing it."""
    cr.shutdown()
    cr.reset_breakers()
    release = threading.Event()
    made: list[FakeClient] = []

    def slow_factory(*, codex_bin, cwd):
        c = FakeClient(lambda p: _scout_json())
        made.append(c)
        if len(made) == 1:
            release.wait(5)  # first app-server start hangs
        return c

    monkeypatch.setattr(cr, "_client_factory", slow_factory)
    cfg = {**BASE, "codex_role_timeout_sec": 0.2}
    try:
        with pytest.raises(cr.CodexRoleError, match="timeout"):
            cr.call("p", cfg)
        assert cr._client is None
        # next call starts client B while A is still starting
        text, _ms, _ = cr.call("p", {**BASE, "codex_role_timeout_sec": 2})
        assert text and len(made) == 2
        b = cr._client
        assert b is made[1]
        release.set()  # stale worker A finally gets its client
        assert _wait_for(lambda: made[0].closed), "stale client A orphaned"
        assert cr._client is b and not b.closed  # A never replaced B
    finally:
        release.set()
        cr.shutdown()
        cr.reset_breakers()


def test_startup_hang_then_shutdown_closes_stale_client(monkeypatch):
    cr.shutdown()
    cr.reset_breakers()
    release = threading.Event()
    made: list[FakeClient] = []

    def slow_factory(*, codex_bin, cwd):
        release.wait(5)
        c = FakeClient(lambda p: _scout_json())
        made.append(c)
        return c

    monkeypatch.setattr(cr, "_client_factory", slow_factory)
    try:
        with pytest.raises(cr.CodexRoleError):
            cr.call("p", {**BASE, "codex_role_timeout_sec": 0.2})
        cr.shutdown(timeout=0.5)
        release.set()
        assert _wait_for(lambda: bool(made) and made[0].closed)
        assert cr._client is None
    finally:
        release.set()
        cr.reset_breakers()


class _Item:
    def __init__(self, kind):
        self.type = kind


@pytest.mark.parametrize("kind", ["commandExecution", "fileChange", "mcpToolCall",
                                  "webSearch", "somethingNew", None])
def test_tool_event_fails_the_call(fake, monkeypatch, kind):
    orig = _Result.__init__

    def with_tool(self, text):
        orig(self, text)
        self.items = [_Item("userMessage"), _Item(kind), _Item("agentMessage")]

    monkeypatch.setattr(_Result, "__init__", with_tool)
    with pytest.raises(cr.CodexRoleError, match="unexpected tool event"):
        cr.call("p", BASE)
    assert cr.breaker_state()["scout"]["failures"] == 1
    monkeypatch.setitem(sc._PROVIDERS, "nvidia", lambda p, c: (_scout_json(), 5))
    result = sc.run_chain("streamer: x", {**BASE, "scouter_provider_chain": ["codex", "nvidia"]})
    assert result.provider == "nvidia"


def test_message_only_items_accepted(fake, monkeypatch):
    orig = _Result.__init__

    def ok_items(self, text):
        orig(self, text)
        self.items = [_Item("userMessage"), _Item("reasoning"), _Item("agentMessage")]

    monkeypatch.setattr(_Result, "__init__", ok_items)
    assert cr.call("p", BASE)[0]


def test_overrides_disable_tools_but_not_login():
    o = set(cr.OVERRIDES)
    for must in ('features.shell_tool=false', 'features.unified_exec=false',
                 'features.hooks=false', 'features.plugins=false', 'features.apps=false',
                 'features.unbounded_connection_retries=false',
                 'orchestrator.skills.enabled=false', 'features.skip_host_skill_discovery=true',
                 'web_search="disabled"', 'mcp_servers={}', 'sandbox_mode="read-only"'):
        assert must in o, must
    joined = " ".join(o)
    for banned in ("forced_login_method", "cli_auth_credentials_store", "model_provider"):
        assert banned not in joined
    import arti_stardew_codex as sdc  # noqa: PLC0415
    feature_lines = {x for x in sdc.OVERRIDES if x.startswith(("features.", "orchestrator."))}
    assert feature_lines <= o


def test_session_cap_per_role(fake, capsys):
    cfg = {**BASE, "codex_role_session_cap_scout": 2}
    cr.call("p", cfg)
    cr.call("p", cfg)
    assert cr.session_calls()["scout"] == 2
    assert cr.is_available(cfg) is False
    assert sc._resolve_chain({**cfg, "scouter_provider_chain": ["nvidia", "codex"]}) == ["nvidia"]
    with pytest.raises(cr.CodexRoleError, match="session cap"):
        cr.call("p", cfg)
    assert len(fake["clients"][0].runs) == 2
    out = capsys.readouterr().out
    assert out.count("batas sesi") == 1  # one log line, not one per tick
    # other roles have their own budget
    assert cr.is_available({**cfg, "cursor_role": "observer"}) is True


def test_session_cap_defaults_and_dict_key():
    assert cr.session_cap("scout", {}) == 20
    assert cr.session_cap("observer", {}) == 12
    assert cr.session_cap("catchup", {}) == 6
    assert cr.session_cap("diary", {}) == 2
    assert cr.session_cap("scout", {"codex_role_session_caps": {"scout": 5}}) == 5
    assert cr.session_cap("scout", {"codex_role_session_caps": {"scout": 5},
                                    "codex_role_session_cap_scout": 7}) == 7


def test_session_cap_counts_failed_attempts(fake):
    def boom(p):
        raise RuntimeError("503")

    fake["behaviour"] = boom
    cfg = {**BASE, "codex_role_session_cap_diary": 2, "codex_role": "diary"}
    for _ in range(2):
        with pytest.raises(cr.CodexRoleError, match="gagal"):
            cr.call("p", cfg)
    assert cr.is_available(cfg) is False


def test_half_open_is_single_trial(fake):
    def boom(p):
        raise RuntimeError("503")

    fake["behaviour"] = boom
    cfg = {**BASE, "codex_role_breaker_cooldown_sec": 600}
    for _ in range(3):
        with pytest.raises(cr.CodexRoleError):
            cr.call("p", cfg)
    assert cr.breaker_state()["scout"]["open"]
    cr._breakers["scout"]["opened_at"] -= 601
    with pytest.raises(cr.CodexRoleError, match="gagal"):
        cr.call("p", cfg)  # the one trial fails ...
    assert cr.breaker_state()["scout"]["open"] is True  # ... and re-closes at once
    with pytest.raises(cr.CodexRoleError, match="breaker open"):
        cr.call("p", cfg)


def test_busy_path_does_not_count_or_trip(fake):
    gate = threading.Event()
    entered = threading.Event()

    def slow(p):
        entered.set()
        gate.wait(5)
        return _scout_json()

    fake["behaviour"] = slow
    out: dict = {}

    def first_call():
        out["r"] = cr.call("p", {**BASE, "codex_role_timeout_sec": 5})

    t = threading.Thread(target=first_call)
    t.start()
    try:
        assert entered.wait(2)
        with pytest.raises(cr.CodexRoleError, match="busy"):
            cr.call("p", {**BASE, "cursor_role": "observer", "codex_role_timeout_sec": 0.2})
        assert "observer" not in cr.session_calls()
        assert cr.breaker_state().get("observer", {"failures": 0})["failures"] == 0
    finally:
        gate.set()
        t.join(5)
    assert out["r"][0]
    assert len(fake["clients"]) == 1  # the busy call never started a client


def test_recycle_closes_old_client(fake):
    cfg = {**BASE, "codex_role_client_max_calls": 2}
    cr.call("p", cfg)
    first = fake["clients"][0]
    cr.call("p", cfg)
    assert cr._client is None
    assert _wait_for(lambda: first.closed)
    cr.call("p", cfg)
    assert len(fake["clients"]) == 2 and fake["clients"][1] is cr._client


def test_shutdown_is_synchronous_and_idempotent(fake):
    cr.call("p", BASE)
    client = fake["clients"][0]
    cr.shutdown(timeout=1.0)
    assert client.closed  # closed before shutdown() returned
    assert cr._client is None
    cr.shutdown(timeout=1.0)  # second call is a no-op


def test_atexit_close_is_inline(fake, monkeypatch):
    cr.call("p", BASE)
    client = fake["clients"][0]

    class _NoThreads:
        Lock = threading.Lock

        @staticmethod
        def Thread(*a, **k):  # noqa: N802
            raise RuntimeError("no new threads at interpreter shutdown")

    monkeypatch.setattr(cr, "threading", _NoThreads)
    cr._atexit_close()
    assert client.closed and cr._client is None


# --- bridge exit path -------------------------------------------------------

def test_bridge_shutdown_helper_closes_role_and_voice(fake, monkeypatch):
    import arti_bridge as bridge  # noqa: PLC0415

    cr.call("p", BASE)
    client = fake["clients"][0]
    seen = []
    monkeypatch.setattr(voice, "abort_turn", lambda reason: seen.append(reason))
    bridge._shutdown_codex_clients(timeout=1.0)
    assert client.closed and cr._client is None
    assert seen == ["shutdown"]


def test_bridge_shutdown_helper_skips_unimported(monkeypatch):
    import arti_bridge as bridge  # noqa: PLC0415

    monkeypatch.delitem(sys.modules, "arti_codex_role", raising=False)
    monkeypatch.delitem(sys.modules, "arti_codex_agent", raising=False)
    bridge._shutdown_codex_clients(timeout=0.1)  # must not import or raise
    assert "arti_codex_role" not in sys.modules


class _FakeProc:
    def __init__(self):
        self.killed = False
        self.exited = False

    def poll(self):
        return 0 if (self.killed or self.exited) else None

    def kill(self):
        self.killed = True


def _fake_api(init_gate=None):
    """Stand-in for openai_codex.api: CodexClient.start() spawns a fake proc;
    initialize() can hang on ``init_gate``."""
    class CodexClient:
        def __init__(self, config=None):
            self.config = config
            self._proc = None
            self.closed = False

        def start(self):
            self._proc = _FakeProc()

        def initialize(self):
            if init_gate is not None:
                init_gate.wait(5)
            return "meta"

        def close(self):
            self.closed = True
            if self._proc:
                self._proc.exited = True

    class _Api:
        pass

    api = _Api()
    api.CodexClient = CodexClient
    api.validate_initialize_metadata = lambda m: m
    return api


class _FakeOc:
    class Codex:
        def close(self):
            self._client.close()


def test_spawn_tracks_proc_before_initialize_and_exit_kills_hung_start():
    cr._kill_stray_procs()
    gate = threading.Event()
    box = {}
    t = threading.Thread(target=lambda: box.setdefault(
        "c", cr._spawn_codex(_FakeOc, cfg=None, api=_fake_api(gate))), daemon=True)
    t.start()
    try:
        assert _wait_for(lambda: len(cr._procs) == 1)  # tracked while initialize() hangs
        proc = cr._procs[0]
        cr.shutdown(timeout=0.2)  # exit path while the start is still hanging
        assert proc.killed, "hung app-server start left behind"
        assert cr._procs == []
    finally:
        gate.set()
        t.join(5)


def test_spawn_normal_path_and_close_is_not_killed():
    cr._kill_stray_procs()
    codex = cr._spawn_codex(_FakeOc, cfg="cfg", api=_fake_api())
    assert codex._init == "meta" and codex._client.config == "cfg"
    proc = codex._client._proc
    codex.close()
    assert cr._kill_stray_procs() == 0 and not proc.killed  # exited cleanly


def test_atexit_kills_tracked_live_proc():
    cr._kill_stray_procs()
    p = _FakeProc()
    cr._track_proc(p)
    cr._atexit_close()
    assert p.killed


def test_sdk_codex_init_layout_pinned():
    """_spawn_codex mirrors Codex.__init__; fail loudly if the SDK changes it."""
    oc_mod = pytest.importorskip("openai_codex")
    import inspect  # noqa: PLC0415
    import openai_codex.api as api  # noqa: PLC0415

    src = inspect.getsource(oc_mod.Codex.__init__)
    for step in ("CodexClient(config=config)", "self._client.start()",
                 "validate_initialize_metadata(self._client.initialize())"):
        assert step in src, step
    assert hasattr(api, "CodexClient") and hasattr(api, "validate_initialize_metadata")


def test_voice_atexit_close_registered_and_idempotent(monkeypatch):
    import arti_bridge as bridge  # noqa: PLC0415
    import ast  # noqa: PLC0415

    tree = ast.parse((ROOT / "arti_bridge.py").read_text(encoding="utf-8"))
    regs = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func).endswith("atexit.register")]
    assert "_atexit.register(_atexit_close_codex_voice)" in regs

    class _VoiceClient:
        closes = 0

        def close(self):
            _VoiceClient.closes += 1

    monkeypatch.setattr(voice, "_client", _VoiceClient())
    monkeypatch.setattr(voice, "_thread", object())
    bridge._atexit_close_codex_voice()
    bridge._atexit_close_codex_voice()  # second call: no-op
    assert _VoiceClient.closes == 1
    assert voice._client is None and voice._thread is None


def test_ctrl_break_path_reaches_codex_close():
    """Ctrl+Break -> SIGBREAK -> default_int_handler (KeyboardInterrupt) -> the
    __main__ try/finally, which must call _shutdown_codex_clients() right
    after save_stream_session_log()."""
    import ast  # noqa: PLC0415

    src = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")
    assert "_signal.signal(_signal.SIGBREAK, _signal.default_int_handler)" in src
    tree = ast.parse(src)
    main_if = next(n for n in tree.body if isinstance(n, ast.If)
                   and "__main__" in ast.unparse(n.test))
    finals = [t for t in ast.walk(main_if) if isinstance(t, ast.Try) and t.finalbody]

    def _calls(stmts):
        names = []
        for st in stmts:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                f = st.value.func
                names.append(f.id if isinstance(f, ast.Name) else ast.unparse(f))
        return names

    hit = next(c for c in (_calls(t.finalbody) for t in finals)
               if "save_stream_session_log" in c)
    i = hit.index("save_stream_session_log")
    assert hit[i + 1] == "_shutdown_codex_clients"


def test_vision_role_uses_image_without_tools_or_voice_client(fake):
    import openai_codex as oc
    text, _, info = cr.call("describe public scene", {**BASE, "codex_role":"vision"}, image_b64="synthetic")
    assert text and info["role"]=="vision"
    client=fake["clients"][0]
    inputs=client.runs[0]["prompt"]
    assert isinstance(inputs[0], oc.TextInput)
    assert isinstance(inputs[1], oc.ImageInput)
    assert inputs[1].url=="data:image/jpeg;base64,synthetic"
    assert client.thread_starts[0]["ephemeral"] is True
    assert "Do not use tools" in client.thread_starts[0]["developer_instructions"]
