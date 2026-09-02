"""Arti dubbing v1 — mode cerita: baca dialog game dari layar, suarakan (19 Agu 2026).

Desain lengkap: docs/research/2026-08-19-game-cerita-dubbing.md.
Proses MANDIRI seperti arti_discord (bridge tidak disentuh; integrasi mode
cerita ke bridge = fase berikutnya). Jalur ini TANPA LLM — nol token:

    window game -> OCR (RapidOCR + DirectML, ~200 ms/frame di RTX 4050)
      -> kurasi (subtitle vs UI vs lingkungan, gabung multi-baris)
      -> pelacak (gerbang stabilitas + dedup efek-ketik)
      -> Supertone (venv312, NDJSON) + preset suara per gender (voice_dsp)
      -> speaker/virtual cable

Jalankan:
    ./venv/Scripts/python.exe arti_dubbing.py --window "judul game"
    ./venv/Scripts/python.exe arti_dubbing.py --demo-gambar dump --tanpa-suara

Aturan yang dijaga:
- Privasi v1: OCR HANYA saat window game di depan (foreground). streamer suka
  alt-tab — begitu game tidak fokus, mata dubbing MERAM (tidak membaca
  browser/DM). True background-capture (WGC) = v2.
- Suara dasar Arti yang TERKUNCI tidak disentuh: preset dub = overlay
  config voice_dsp KHUSUS lajur ini (netral/cowok/cewek — keputusan streamer:
  "cowo direndahin, cewe ditinggiin").
- Telat lebih buruk daripada skip: antrian ucap maks 2, yang tua dibuang.
- Tiap baris yang diucap dicatat ke data/dub_log.jsonl — kelak disuapkan
  ke history bridge supaya komentar Arti sadar dia barusan meranin siapa.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

DEFAULTS: dict = {
    "dub_window_hint": "",              # substring judul window game (wajib utk mode live)
    "dub_interval_sec": 0.6,            # irama OCR full-frame (GPU ~0.2s -> 0.6 longgar)
    "dub_stabil_frame": 2,              # teks identik N frame dulu baru diucap (efek ketik)
    "dub_min_kata": 3,
    "dub_antrian_maks": 2,              # telat > skip: lebih dari ini, yang tua dibuang
    "dub_range_factor": 1.2,
    # Semitone ABSOLUT per preset (suara normal Arti = +2.0; dub sengaja beda
    # supaya penonton dengar "mode baca"). Keputusan operator [date removed].
    "dub_preset_semitone": {"netral": 0.0, "cowok": -3.0, "cewek": 5.0},
    # Pemetaan nama tokoh -> gender, diisi manual per game ATAU kelak oleh
    # penulis skenario. Kunci lowercase.
    "dub_nama_gender": {},
    "dub_log_path": "data/dub_log.jsonl",
}


def muat_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        overlay = json.loads((_ROOT / "config_local.json").read_text(encoding="utf-8"))
    except Exception:
        overlay = {}
    for k, v in overlay.items():
        if v is not None:
            cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# Kurasi kotak OCR — murni, target unit test
# ---------------------------------------------------------------------------

def klasifikasi_kotak(box, teks: str, w: int, h: int) -> str:
    """SUBTITLE (kandidat dub) / UI (buang) / LINGKUNGAN (bahan komentar).
    Heuristik tervalidasi spike 19 Agu: 6 screenshot, nol sampah bocor
    ke SUBTITLE (chat Twitch, HP bar, tombol semua tersaring)."""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    cx = sum(xs) / len(xs) / w
    cy = sum(ys) / len(ys) / h
    kata = len(teks.split())
    if cy > 0.78 and 0.2 < cx < 0.8 and kata >= 3:
        return "SUBTITLE"
    if kata <= 2 or cy < 0.12 or cx > 0.85 or cx < 0.12:
        return "UI"
    return "LINGKUNGAN"


def di_zona_subtitle(box, w: int, h: int) -> bool:
    """Zona dinilai SEBELUM jumlah kata — demo 19 Agu: kotak 'Room.'
    (1 kata, pecahan baris kedua) terbuang sebelum sempat digabung,
    subtitle CaseOh kehilangan ekornya. Urutan benar: zona -> gabung ->
    baru nilai layak-ucap."""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    cx = sum(xs) / len(xs) / w
    cy = sum(ys) / len(ys) / h
    return cy > 0.78 and 0.15 < cx < 0.85


def rasio_huruf(t: str) -> float:
    """Dialog didominasi HURUF; sampah zona bawah (stempel '7/27/26',
    ukur tinggi '2016-3'11\"') didominasi angka/simbol — temuan demo 19 Agu
    setelah zona dibuat memungut semua kotak."""
    isi = [c for c in t if not c.isspace()]
    if not isi:
        return 0.0
    return sum(1 for c in isi if c.isalpha()) / len(isi)


def rapikan_teks(t: str) -> str:
    """Jahitan kotak OCR suka menelan spasi ('Terkunci,aku') — TTS
    membacanya aneh. Sisipkan spasi sesudah tanda baca yang nempel huruf."""
    import re

    return re.sub(r"([,.!?;])(?=[A-Za-zÀ-ɏ])", r"\1 ", t).strip()


def gabung_subtitle(kotak_subtitle: list[tuple[list, str]]) -> str:
    """PR #1 spike: subtitle multi-baris pecah per kotak ('Lay Traps,
    Barricade Doors and' kehilangan baris keduanya). Kotak zona subtitle
    diurutkan atas->bawah lalu kiri->kanan, teksnya dijahit jadi satu."""
    if not kotak_subtitle:
        return ""
    urut = sorted(
        kotak_subtitle,
        key=lambda bt: (min(p[1] for p in bt[0]), min(p[0] for p in bt[0])),
    )
    return rapikan_teks(" ".join(t.strip() for _, t in urut if t.strip()))


def deteksi_nama(teks: str) -> tuple[str | None, str]:
    """'Rina: halo' -> ('rina', 'halo'). Tanpa label -> (None, teks utuh)."""
    if ":" in teks[:24]:
        nama, _, sisa = teks.partition(":")
        nama = nama.strip()
        if 0 < len(nama.split()) <= 2 and sisa.strip():
            return nama.lower(), sisa.strip()
    return None, teks


def pilih_preset(nama: str | None, config: dict) -> str:
    if nama:
        gender = (config.get("dub_nama_gender") or {}).get(nama.lower())
        if gender in ("cowok", "cewek"):
            return gender
    return "netral"


def config_preset(preset: str, config: dict) -> dict:
    """Overlay config voice_dsp untuk SATU preset dub. Suara dasar Arti
    (kunci supertonic_* produksi) tidak pernah ditulis — cuma dict baru."""
    semi = (config.get("dub_preset_semitone") or DEFAULTS["dub_preset_semitone"])
    return {
        "supertonic_pitch_semitone": float(semi.get(preset, 0.0)),
        "supertonic_range_factor": float(config.get("dub_range_factor", 1.2)),
    }


# ---------------------------------------------------------------------------
# Pelacak dialog — gerbang stabilitas + dedup efek-ketik (murni)
# ---------------------------------------------------------------------------

def _polos(t: str) -> str:
    return "".join(t.lower().split())


def awalan_dari(a: str, b: str) -> bool:
    """Teks a adalah awalan (kasar) dari b? Toleran salah-baca OCR kecil."""
    a2, b2 = _polos(a), _polos(b)
    if not a2 or not b2 or len(a2) > len(b2):
        return False
    potong = a2[: max(8, int(len(a2) * 0.8))]
    return b2.startswith(potong)


@dataclass
class PelacakDialog:
    """Satu teks subtitle per frame masuk -> keluar kalimat siap ucap.

    Efek ketik (bukti spike: 'menyantap ini! In' -> kalimat penuh 3 dtk
    kemudian) ditangani gerbang stabilitas: teks harus IDENTIK
    `stabil_frame` frame berturut sebelum diucap; teks yang masih tumbuh
    (frame baru = perpanjangan frame lama) mereset hitungan.
    """

    stabil_frame: int = 2
    _kandidat: str = ""
    _hitung: int = 0
    _terucap: deque = field(default_factory=lambda: deque(maxlen=8))

    def masuk(self, teks: str) -> str | None:
        teks = (teks or "").strip()
        if not teks:
            self._kandidat, self._hitung = "", 0
            return None

        if _polos(teks) == _polos(self._kandidat):
            self._hitung += 1
        elif awalan_dari(self._kandidat, teks):
            # Masih diketik — kandidat tumbuh, hitung ulang.
            self._kandidat, self._hitung = teks, 1
        else:
            self._kandidat, self._hitung = teks, 1

        if self._hitung < self.stabil_frame:
            return None
        for lama in self._terucap:
            if _polos(teks) == _polos(lama) or awalan_dari(teks, lama):
                return None  # sudah pernah (atau versi pendeknya) diucap
        for lama in self._terucap:
            if awalan_dari(lama, teks):
                # Kalimat LANJUTAN dari yang sudah terucap (efek ketik yang
                # sempat berhenti lalu lanjut) — ucapkan EKORNYA saja,
                # jangan ulang dari awal.
                kata_lama = len(lama.split())
                ekor = " ".join(teks.split()[kata_lama:]).strip()
                self._terucap.append(teks)
                return ekor or None
        self._terucap.append(teks)
        return teks


# ---------------------------------------------------------------------------
# Window game (ctypes, tanpa pywin32)
# ---------------------------------------------------------------------------

class WindowGame:
    def __init__(self, hint: str):
        self.hint = hint.lower()
        self.hwnd = 0
        self._u32 = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001 — DPI awareness gagal bukan fatal
            pass

    def _judul(self, hwnd) -> str:
        n = self._u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        self._u32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def cari(self) -> bool:
        cocok = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum(hwnd, _):
            if self._u32.IsWindowVisible(hwnd):
                judul = self._judul(hwnd)
                if judul and self.hint in judul.lower():
                    cocok.append((hwnd, judul))
            return True

        self._u32.EnumWindows(_enum, 0)
        if cocok:
            self.hwnd = cocok[0][0]
            print(f"[Dub] Window ketemu: \"{cocok[0][1]}\"")
        return bool(cocok)

    def di_depan(self) -> bool:
        """Privasi v1: mata dubbing cuma melek saat game yang fokus."""
        return bool(self.hwnd) and self._u32.GetForegroundWindow() == self.hwnd

    def rect(self) -> tuple[int, int, int, int] | None:
        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                        ("r", ctypes.c_long), ("b", ctypes.c_long)]

        r = RECT()
        if not self._u32.GetWindowRect(self.hwnd, ctypes.byref(r)):
            return None
        if r.r - r.l < 100 or r.b - r.t < 100:
            return None
        return (r.l, r.t, r.r - r.l, r.b - r.t)


# ---------------------------------------------------------------------------
# Suara — Supertone standalone (pola build_reflex_cache) + preset dub
# ---------------------------------------------------------------------------

class SuaraDub:
    def __init__(self, config: dict, tanpa_suara: bool = False):
        self.cfg = config
        self.tanpa_suara = tanpa_suara
        self.proc = None
        self._rid = 0
        self.antrian: deque = deque()

    def _venv312(self) -> str:
        p = _ROOT / "venv312" / "Scripts" / "python.exe"
        if not p.exists():
            raise FileNotFoundError(f"venv312 tidak ada di {p}")
        return str(p)

    def nyalakan(self) -> None:
        if self.tanpa_suara or self.proc:
            return
        env = dict(os.environ)
        env["SUPERTONE_USE_CUDA"] = "1" if self.cfg.get("supertonic_use_cuda") else "0"
        self.proc = subprocess.Popen(
            [self._venv312(), str(_ROOT / "supertone_engine.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            cwd=str(_ROOT), env=env,
        )
        print("[Dub] Supertone engine dinyalakan (venv312)")

    def _synthesize(self, teks: str) -> str | None:
        self._rid += 1
        rid = f"dub-{self._rid}"
        self.proc.stdin.write(json.dumps({
            "v": 1, "id": rid, "type": "synthesize", "text": teks,
            "voice": self.cfg.get("supertonic_voice", "F1"),
            "speed": float(self.cfg.get("supertonic_speed", 1.1)),
            "lang": self.cfg.get("supertonic_lang", "id"),
            "total_steps": int(self.cfg.get("supertonic_total_steps", 8)),
            "preprocess_numbers": True,
        }) + "\n")
        self.proc.stdin.flush()
        batas = time.time() + 60
        while time.time() < batas:
            line = self.proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                return msg.get("wav_path") if msg.get("ok") else None
        return None

    def ucap(self, teks: str, preset: str) -> None:
        maks = int(self.cfg.get("dub_antrian_maks", 2))
        self.antrian.append((teks, preset))
        while len(self.antrian) > maks:
            buang = self.antrian.popleft()
            print(f"[Dub] SKIP (telat > skip): \"{buang[0][:60]}\"")
        while self.antrian:
            t, p = self.antrian.popleft()
            if self.tanpa_suara:
                print(f"[Dub/{p}] {t}")
                continue
            wav = self._synthesize(t)
            if not wav:
                print(f"[Dub] sintesis gagal: \"{t[:60]}\"")
                continue
            self._mainkan(wav, p)

    def _mainkan(self, wav_path: str, preset: str) -> None:
        import numpy as np  # noqa: F401 — dipakai poles_suara
        import sounddevice as sd
        import soundfile as sf

        import arti_voice_dsp

        data, sr = sf.read(wav_path, dtype="float32")
        cfg_preset = config_preset(preset, self.cfg)
        if arti_voice_dsp.aktif(cfg_preset):
            data = arti_voice_dsp.poles_suara(data, sr, cfg_preset)
        dev = self._cari_cable()
        sd.play(data, sr, device=dev)
        sd.wait()

    def _cari_cable(self):
        import sounddevice as sd

        nama = str(self.cfg.get("virtual_cable_name", "CABLE Input")).lower()
        try:
            for i, d in enumerate(sd.query_devices()):
                if nama in d["name"].lower() and d["max_output_channels"] > 0:
                    return i
        except Exception:  # noqa: BLE001
            pass
        return None  # default output

    def matikan(self) -> None:
        if self.proc:
            try:
                self.proc.stdin.write(json.dumps({"v": 1, "type": "shutdown"}) + "\n")
                self.proc.stdin.flush()
            except Exception:  # noqa: BLE001
                pass
            self.proc.terminate()
            self.proc = None


def catat_log(config: dict, nama: str | None, teks: str, preset: str) -> None:
    """Jejak untuk kelak disuapkan ke history bridge (dub yang 'involve')."""
    try:
        p = _ROOT / str(config.get("dub_log_path", "data/dub_log.jsonl"))
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "nama": nama, "teks": teks, "preset": preset,
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — log gagal bukan alasan berhenti dub
        pass


# ---------------------------------------------------------------------------
# Loop utama
# ---------------------------------------------------------------------------

def proses_frame(ocr, frame_bgr, w: int, h: int, pelacak: PelacakDialog,
                 config: dict) -> tuple[str, str, str | None] | None:
    """Satu frame -> (teks_siap_ucap, preset, nama) atau None."""
    res, _ = ocr(frame_bgr)
    kotak_sub = []
    for box, teks, skor in (res or []):
        if skor < 0.5:
            continue
        # Zona dulu, jumlah kata belakangan — pecahan baris ('Room.')
        # ikut terbawa dan dinilai SETELAH digabung. Kotak nyaris-tanpa-huruf
        # (stempel tanggal, angka HP) dibuang SEBELUM digabung.
        if di_zona_subtitle(box, w, h) and rasio_huruf(teks) >= 0.35:
            kotak_sub.append((box, teks))
    mentah = gabung_subtitle(kotak_sub)
    if rasio_huruf(mentah) < 0.5:
        mentah = ""
    siap = pelacak.masuk(mentah)
    if not siap:
        return None
    if len(siap.split()) < int(config.get("dub_min_kata", 3)):
        return None
    nama, isi = deteksi_nama(siap)
    return isi, pilih_preset(nama, config), nama


def main() -> int:
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # Proses mandiri = tanpa Tee bridge — pasang sendiri (audit [date removed]:
    # "otomatis ke disk ga?"), supaya error OCR/engine tidak menguap.
    import arti_debug_log

    arti_debug_log.pasang("dubbing")
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="", help="substring judul window game")
    ap.add_argument("--demo-gambar", default="", help="folder gambar (uji tanpa game)")
    ap.add_argument("--tanpa-suara", action="store_true")
    ap.add_argument("--interval", type=float, default=0.0)
    args = ap.parse_args()

    config = muat_config()
    interval = args.interval or float(config.get("dub_interval_sec", 0.6))
    pelacak = PelacakDialog(stabil_frame=int(config.get("dub_stabil_frame", 2)))
    suara = SuaraDub(config, tanpa_suara=args.tanpa_suara)

    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)

    if args.demo_gambar:
        # Mode demo: tiap gambar = satu "frame" (diulang biar lolos gerbang
        # stabilitas), tanpa window/game.
        from PIL import Image
        import numpy as np

        suara.nyalakan()
        for p in sorted(Path(args.demo_gambar).glob("*")):
            if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            im = np.array(Image.open(p).convert("RGB"))[:, :, ::-1]
            h, w = im.shape[:2]
            hasil = None
            for _ in range(pelacak.stabil_frame):
                hasil = proses_frame(ocr, im, w, h, pelacak, config)
            print(f"--- {p.name}")
            if hasil:
                isi, preset, nama = hasil
                catat_log(config, nama, isi, preset)
                suara.ucap(isi, preset)
        suara.matikan()
        return 0

    hint = args.window or str(config.get("dub_window_hint") or "")
    if not hint:
        print("[Dub] Kasih --window \"judul game\" atau isi dub_window_hint di config_local.")
        return 2

    import mss
    import numpy as np

    win = WindowGame(hint)
    if not win.cari():
        print(f"[Dub] Window dengan judul mengandung \"{hint}\" tidak ketemu.")
        return 2
    suara.nyalakan()
    print(f"[Dub] Jalan — interval {interval}s, Ctrl+C untuk berhenti.")
    try:
        with mss.mss() as sct:
            while True:
                t0 = time.perf_counter()
                if win.di_depan():
                    r = win.rect()
                    if r:
                        l, t, w, h = r
                        shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
                        frame = np.array(shot)[:, :, :3]
                        hasil = proses_frame(ocr, frame, w, h, pelacak, config)
                        if hasil:
                            isi, preset, nama = hasil
                            catat_log(config, nama, isi, preset)
                            suara.ucap(isi, preset)
                sisa = interval - (time.perf_counter() - t0)
                if sisa > 0:
                    time.sleep(sisa)
    except KeyboardInterrupt:
        print("\n[Dub] Berhenti.")
    finally:
        suara.matikan()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
