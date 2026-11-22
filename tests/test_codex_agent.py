"""Provider Codex/Luna — penyedia kedua (19 Agu 2026).

SDK asli butuh login ChatGPT + binari; di sini semua logika di sekitarnya:
gerbang enabled, timeout keras, daur ulang, dan pagar proyek (default MATI,
haram buat kerja latar, urutan lapis composer -> codex -> Groq).
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import arti_codex_agent as ca
import arti_bridge as bridge

ROOT = Path(__file__).resolve().parent.parent
_SRC_BRIDGE = (ROOT / "arti_bridge.py").read_text(encoding="utf-8")


class _ThreadPalsu:
    def __init__(self, jawab="halo dari luna", tidur=0.0, meledak=False):
        self.jawab, self.tidur, self.meledak = jawab, tidur, meledak
        self.effort_terakhir = None

    def run(self, teks, model=None, effort=None):
        self.effort_terakhir = effort
        if self.tidur:
            time.sleep(self.tidur)
        if self.meledak:
            raise RuntimeError("boom")

        class _R:
            final_response = self.jawab
            usage = None

        return _R()


def _pasang(monkeypatch, th):
    monkeypatch.setattr(ca, "_client", object())
    monkeypatch.setattr(ca, "_thread", th)
    monkeypatch.setattr(ca, "_turns", 0)


def test_default_source_mati_nyala_hanya_dari_config_local():
    """ToS abu-abu — nyala harus keputusan sadar operator. Default SOURCE
    wajib False; CONFIG runtime boleh True kalau config_local bilang
    begitu."""
    assert '"codex_agent_enabled": False,' in _SRC_BRIDGE
    assert bridge.CONFIG["codex_model"] == "gpt-5.6-luna"
    # Timeout = kenop operator juga (pagar yang terlalu ketat mencekik Luna
    # saat prompt panjang). Kunci SOURCE, rentang waras buat runtime.
    assert '"codex_timeout_sec": 8.0,' in _SRC_BRIDGE
    assert 4.0 <= float(bridge.CONFIG["codex_timeout_sec"]) <= 30.0
    # Effort = kenop operator — yang
    # dikunci cuma default SOURCE, bukan nilai runtime.
    assert '"codex_effort": "low",' in _SRC_BRIDGE
    assert bridge.CONFIG["codex_effort"] in ("low", "medium", "high", "xhigh", "max")


def test_prewarm_tidak_menyalakan_server_di_pytest():
    """Suite sempat memanggil Luna BETULAN saat codex nyala di config
    produksi — kuota terbakar diam-diam. prewarm wajib no-op di pytest."""
    assert ca.prewarm({"codex_agent_enabled": True}) is False
    assert not ca.is_warm()


def test_disabled_langsung_none_tanpa_sdk(monkeypatch):
    _pasang(monkeypatch, _ThreadPalsu())
    assert ca.send_turn("sys", "halo", {"codex_agent_enabled": False}) is None


def test_enabled_dan_hangat_menjawab(monkeypatch, capsys):
    th = _ThreadPalsu(jawab="jawaban luna")
    _pasang(monkeypatch, th)
    r = ca.send_turn("sys", "halo", {"codex_agent_enabled": True})
    assert r == "jawaban luna"
    assert "[Codex] gpt-5.6-luna/low" in capsys.readouterr().out
    assert th.effort_terakhir == "low", "effort low wajib eksplisit (hemat + cepat)"


def test_effort_bisa_dinaikkan_via_config(monkeypatch):
    th = _ThreadPalsu()
    _pasang(monkeypatch, th)
    ca.send_turn("sys", "halo",
                 {"codex_agent_enabled": True, "codex_effort": "medium"})
    assert th.effort_terakhir == "medium"


def test_timeout_keras_membuang_thread(monkeypatch):
    """Run yang macet tidak boleh menyandera giliran suara."""
    _pasang(monkeypatch, _ThreadPalsu(tidur=5.0))
    ditutup = []
    monkeypatch.setattr(ca, "_tutup", lambda alasan, **_kw: ditutup.append(alasan))
    t0 = time.perf_counter()
    r = ca.send_turn("sys", "halo",
                     {"codex_agent_enabled": True, "codex_timeout_sec": 0.3})
    assert r is None
    assert time.perf_counter() - t0 < 2.0, "join harus ber-timeout"
    assert ditutup == ["timeout"]


def test_error_membuang_thread_dan_none(monkeypatch):
    _pasang(monkeypatch, _ThreadPalsu(meledak=True))
    ditutup = []
    monkeypatch.setattr(ca, "_tutup", lambda alasan, **_kw: ditutup.append(alasan))
    assert ca.send_turn("sys", "halo", {"codex_agent_enabled": True}) is None
    assert ditutup == ["error"]


def test_daur_ulang_setelah_batas_turn(monkeypatch):
    _pasang(monkeypatch, _ThreadPalsu())
    monkeypatch.setattr(ca, "_turns", 19)  # turn berikutnya = ke-20
    kejadian = []
    monkeypatch.setattr(ca, "_tutup", lambda alasan: kejadian.append(alasan))
    monkeypatch.setattr(ca, "prewarm", lambda cfg: kejadian.append("prewarm"))
    r = ca.send_turn("sys", "halo",
                     {"codex_agent_enabled": True, "codex_thread_max_turns": 20})
    assert r is not None
    assert kejadian == ["mencapai batas turn", "prewarm"]


def test_belum_hangat_none_dan_memicu_pemanasan(monkeypatch):
    monkeypatch.setattr(ca, "_client", None)
    monkeypatch.setattr(ca, "_thread", None)
    dipanggil = []
    monkeypatch.setattr(ca, "prewarm", lambda cfg: dipanggil.append(1))
    assert ca.send_turn("sys", "halo", {"codex_agent_enabled": True}) is None
    assert dipanggil == [1]


def test_none_di_config_tidak_menyalakan(monkeypatch):
    """Aturan 7: kunci None di overlay tidak boleh mengubah default MATI."""
    _pasang(monkeypatch, _ThreadPalsu())
    assert ca.send_turn("sys", "halo", {"codex_agent_enabled": None}) is None


def test_thread_luna_dibuka_di_scratch_kosong_di_luar_repo(monkeypatch):
    """Read-only tetap tidak boleh berarti read-only terhadap repo Arti."""
    captured = {}

    class _ClientPalsu:
        def __init__(self, cfg):
            captured["client_cfg"] = cfg

        def thread_start(self, **kwargs):
            captured["thread_kwargs"] = kwargs
            return object()

    fake_sdk = types.SimpleNamespace(
        ApprovalMode=types.SimpleNamespace(deny_all="deny-all"),
        CodexConfig=lambda **kwargs: captured.setdefault("cfg_kwargs", kwargs),
        Codex=_ClientPalsu,
        Sandbox=types.SimpleNamespace(read_only="read-only"),
    )
    monkeypatch.setitem(sys.modules, "openai_codex", fake_sdk)
    monkeypatch.setattr(ca, "_auto_scratch_dir", None)

    ca._buka_thread({"codex_agent_enabled": True})

    cfg_cwd = Path(captured["cfg_kwargs"]["cwd"]).resolve()
    thread_cwd = Path(captured["thread_kwargs"]["cwd"]).resolve()
    assert cfg_cwd == thread_cwd
    assert cfg_cwd != ROOT and ROOT not in cfg_cwd.parents
    assert cfg_cwd.is_dir() and not any(cfg_cwd.iterdir())
    assert 'sandbox_mode="read-only"' in captured["cfg_kwargs"]["config_overrides"]
    assert captured["thread_kwargs"]["approval_mode"] == "deny-all"
    assert captured["thread_kwargs"]["sandbox"] == "read-only"
    assert "Do not use tools or read files" in captured["thread_kwargs"][
        "developer_instructions"
    ]


def test_scratch_luna_configured_harus_kosong_dan_di_luar_repo(tmp_path):
    dalam_repo = ROOT / "data"
    try:
        ca._resolve_scratch_dir({"codex_scratch_dir": str(dalam_repo)})
    except ValueError as exc:
        assert "di luar repo" in str(exc)
    else:
        raise AssertionError("scratch di dalam repo semestinya ditolak")

    berisi = tmp_path / "berisi"
    berisi.mkdir()
    (berisi / "rahasia.txt").write_text("jangan dibaca", encoding="utf-8")
    try:
        ca._resolve_scratch_dir({"codex_scratch_dir": str(berisi)})
    except ValueError as exc:
        assert "kosong" in str(exc)
    else:
        raise AssertionError("scratch berisi semestinya ditolak")


# --------------------------------------------------------------------------
# Pagar proyek
# --------------------------------------------------------------------------

def test_kerja_latar_tidak_menyentuh_codex_suara(monkeypatch):
    """Dulu (analog aturan #2) kerja latar dilarang total memakai codex.
    Keputusan owner 2026-10-23 ("[recorded remark omitted]"): scouter/
    observer BOLEH lewat provider "codex" — tapi HANYA lewat modul stateless
    arti_codex_role, tidak pernah thread SUARA arti_codex_agent. Vision memakai
    role stateless yang sama, sebagai fallback terakhir (2026-10-24)."""
    import ast  # noqa: PLC0415

    # AST, bukan grep string: modul latar boleh mengimpor dari
    # arti_codex_agent HANYA konstanta DEFAULT_BIN (atau tidak sama sekali) —
    # tidak pernah `import arti_codex_agent` / send_turn / prewarm / _thread.
    for f in ("arti_scouter_client.py", "arti_vision_client.py",
              "arti_observer_client.py", "arti_codex_role.py"):
        tree = ast.parse((ROOT / f).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "arti_codex_agent", (
                        f"{f}: `import arti_codex_agent` (jalur suara) dilarang")
            elif isinstance(node, ast.ImportFrom):
                if node.module == "arti_codex_agent":
                    nama = {a.name for a in node.names}
                    assert nama <= {"DEFAULT_BIN"}, (
                        f"{f}: dari arti_codex_agent hanya DEFAULT_BIN, bukan {nama}")
                if node.module is None or node.level:
                    assert all(a.name != "arti_codex_agent" for a in node.names)
            elif isinstance(node, ast.Call):
                fn = node.func
                if (isinstance(fn, ast.Name) and fn.id in ("__import__", "import_module")
                        or isinstance(fn, ast.Attribute) and fn.attr == "import_module"):
                    args = [a.value for a in node.args if isinstance(a, ast.Constant)]
                    assert "arti_codex_agent" not in args, f"{f}: impor dinamis jalur suara"
    import arti_codex_role as role_client
    import arti_vision_client as vision

    monkeypatch.setattr(role_client, "is_enabled", lambda config: True)
    assert vision._resolve_chain({"vision_provider_chain": ["codex", "groq", "cloudflare"]}) == [
        "groq", "cloudflare", "codex"]
    calls = []
    def call(prompt, config, **kwargs):
        calls.append((prompt, config, kwargs))
        return "scene", 7, "isolated"
    monkeypatch.setattr(role_client, "call", call)
    assert vision._call_codex("look", "synthetic-jpeg", {}) == ("scene", 7)
    assert calls == [("look", {"codex_role": "vision"}, {"image_b64": "synthetic-jpeg"})]
    role = (ROOT / "arti_codex_role.py").read_text(encoding="utf-8")
    assert "ephemeral=True" in role, "panggilan role harus thread ephemeral per call"


def test_urutan_lapis_codex_sebelum_groq_di_kedua_jalur():
    """Composer gagal ATAU sesi dingin -> Luna dulu, baru Groq."""
    import ast

    function = next(node for node in ast.parse(_SRC_BRIDGE).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_cursor_reply_with_fallback")
    for penanda in ("sesi belum hangat — mencoba Luna lalu fallback Groq",
                    "— fallback Groq"):
        matches = [(index, statement) for index, statement in enumerate(function.body)
                   if any(isinstance(node, ast.Constant) and isinstance(node.value, str) and penanda in node.value
                          for node in ast.walk(statement))]
        assert len(matches) == 1, penanda
        index, statement = matches[0]
        # Scope the cold branch and the post-Cursor tail structurally; guarded append statements may grow.
        scope = statement.body if isinstance(statement, ast.If) else function.body[index:]
        calls = []
        for statement in scope:
            for node in ast.walk(statement):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "arti_executors" and node.func.attr == "run"):
                    continue
                target = node.args[1]
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id == "arti_codex_agent" and target.attr == "send_turn"):
                    calls.append((node.lineno, node.col_offset, "codex"))
                elif isinstance(target, ast.Name) and target.id == "groq_chat_completion":
                    calls.append((node.lineno, node.col_offset, "groq"))
        order = [provider for _, _, provider in sorted(calls)]
        assert "codex" in order and "groq" in order, penanda
        assert order.index("codex") < order.index("groq"), f"urutan lapis salah sesudah '{penanda}'"
