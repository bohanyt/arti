"""Kirim URUTAN tombol ke P2, hanya saat window Stardew benar-benar di depan.

Kenapa urutan, bukan satu-satu: mengirim tombol berikutnya menuntut proses
ini dimatikan dan dinyalakan lagi, dan itu MEMUTUS controller P2 di tengah
jalan -- join-nya batal. Jadi sekali jalan, satu rangkaian, dari keadaan awal
yang diketahui.

Gerbang fokus diperiksa ULANG sebelum setiap tombol. Terukur 29 Agu 2026:
Stardew mengabaikan input gamepad saat window-nya tidak di depan, jadi tombol
yang dikirim sambil alt-tab hilang begitu saja -- lebih buruk daripada gagal,
karena kelihatannya terkirim.

Sesudah urutan awal, stdin tetap hidup di proses yang SAMA. Ketik tombol
berikutnya tanpa memutus P2. Itu yang bikin Gate 5 butir gerak/menu bisa
dijalankan tanpa mengulang join.

Pakai:
    pad_urutan.py START:2.0 A:1.0
    (nama tombol : jeda detik SESUDAH tombol itu)

Stdin sesudah urutan:
    A:0.5
    DPAD_DOWN:0.8
    STIK_BAWAH:1.5
    STIK_NETRAL
    quit          -> lepas pad (P2 putus)
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_gamepad import WindowStardew  # noqa: E402

TUNGGU = 600.0
HIDUP = 3600.0
STABIL = 1.0
TAHAN = 0.15
# File perintah: agen/terminal lain menulis satu baris, skrip memakan lalu menghapus.
# Tanpa ini, stdin proses latar tidak bisa dikirimi tombol Gate 5.
BERKAS_PERINTAH = ROOT / "data" / "stardew" / "pad_perintah.txt"

# Nama -> atribut XUSB_BUTTON di vgamepad. Tidak mengimpor vgamepad di sini
# supaya tes parser jalan tanpa ViGEm.
TOMBOL_ENUM = {
    "A": "XUSB_GAMEPAD_A",
    "B": "XUSB_GAMEPAD_B",
    "X": "XUSB_GAMEPAD_X",
    "Y": "XUSB_GAMEPAD_Y",
    "START": "XUSB_GAMEPAD_START",
    "BACK": "XUSB_GAMEPAD_BACK",
    "LB": "XUSB_GAMEPAD_LEFT_SHOULDER",
    "RB": "XUSB_GAMEPAD_RIGHT_SHOULDER",
    "DPAD_UP": "XUSB_GAMEPAD_DPAD_UP",
    "DPAD_DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "DPAD_LEFT": "XUSB_GAMEPAD_DPAD_LEFT",
    "DPAD_RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
    "UP": "XUSB_GAMEPAD_DPAD_UP",
    "DOWN": "XUSB_GAMEPAD_DPAD_DOWN",
    "LEFT": "XUSB_GAMEPAD_DPAD_LEFT",
    "RIGHT": "XUSB_GAMEPAD_DPAD_RIGHT",
}

# Stik kiri Xbox: y+ = atas. Tahan selama jeda, lalu netral.
STIK = {
    "STIK_ATAS": (0.0, 1.0),
    "STIK_BAWAH": (0.0, -1.0),
    "STIK_KIRI": (-1.0, 0.0),
    "STIK_KANAN": (1.0, 0.0),
    "STIK_NETRAL": (0.0, 0.0),
}

PERINTAH_KHUSUS = frozenset({"QUIT", "LEPAS"})


def parse_langkah(tok: str) -> tuple[str, float]:
    """Urai 'NAMA' atau 'NAMA:jeda' jadi (nama_huruf_besar, jeda_detik)."""
    mentah = (tok or "").strip().lstrip("\ufeff")
    if not mentah:
        raise ValueError("langkah kosong")
    nama, _, jeda = mentah.partition(":")
    nama = nama.strip().upper().replace("-", "_")
    if not nama:
        raise ValueError("nama tombol kosong")
    jeda_s = float(jeda) if jeda.strip() else 1.0
    if jeda_s < 0:
        raise ValueError("jeda tidak boleh negatif")
    return nama, jeda_s


def langkah_dikenal(nama: str) -> bool:
    return nama in TOMBOL_ENUM or nama in STIK or nama in PERINTAH_KHUSUS


def parse_urutan(tokens: list[str]) -> list[tuple[str, float]]:
    """Urai argumen CLI. Gagal kalau ada nama yang tidak dikenal."""
    keluar: list[tuple[str, float]] = []
    for tok in tokens:
        nama, jeda = parse_langkah(tok)
        if nama in PERINTAH_KHUSUS:
            raise ValueError(f"{nama} tidak boleh di urutan awal")
        if not langkah_dikenal(nama):
            raise ValueError(f"tombol tidak dikenal: {nama}")
        keluar.append((nama, jeda))
    return keluar


def _tahan_gerakan(nama: str) -> bool:
    """D-pad dan stik ditahan selama jeda; wajah/START cukup tap."""
    return nama in STIK or nama in {
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
        "UP", "DOWN", "LEFT", "RIGHT",
    }


def tekan_langkah(pad, vg, window: WindowStardew, nama: str, jeda: float) -> bool:
    """Kirim satu langkah. False = dibatalkan karena window tidak di depan."""
    if not window.di_depan():
        print(f"  BATAL sebelum {nama}: window Stardew tidak lagi di depan.",
              flush=True)
        return False

    if nama in STIK:
        x, y = STIK[nama]
        print(f"  stik {nama} {jeda:.1f}s", flush=True)
        pad.left_joystick_float(x_value_float=x, y_value_float=y)
        pad.update()
        if nama != "STIK_NETRAL":
            time.sleep(jeda)
            pad.left_joystick_float(x_value_float=0.0, y_value_float=0.0)
            pad.update()
        return True

    tombol = getattr(vg.XUSB_BUTTON, TOMBOL_ENUM[nama])
    print(f"  tekan {nama}", flush=True)
    pad.press_button(button=tombol)
    pad.update()
    time.sleep(jeda if _tahan_gerakan(nama) else TAHAN)
    pad.release_button(button=tombol)
    pad.update()
    if not _tahan_gerakan(nama):
        time.sleep(jeda)
    return True


def _baca_stdin(antrian: queue.Queue[str]) -> None:
    """EOF stdin TIDAK melepas pad — proses latar sering tidak punya stdin."""
    try:
        for baris in sys.stdin:
            antrian.put(baris)
    except Exception:  # noqa: BLE001
        pass


def _baca_berkas_perintah() -> str | None:
    """Ambil isi berkas perintah lalu hapus, supaya tidak diulang."""
    try:
        if not BERKAS_PERINTAH.is_file():
            return None
        teks = BERKAS_PERINTAH.read_text(encoding="utf-8-sig")
        BERKAS_PERINTAH.unlink(missing_ok=True)
        return teks
    except OSError:
        return None


def main() -> int:
    import vgamepad as vg

    try:
        urutan = parse_urutan(sys.argv[1:])
    except ValueError as e:
        print(str(e), flush=True)
        return 2
    if not urutan:
        print("tanpa urutan awal — pad netral, menunggu perintah.", flush=True)
        urutan = [("STIK_NETRAL", 0.05)]

    window = WindowStardew()
    pad = vg.VX360Gamepad()
    pad.reset()
    pad.update()
    print("pad virtual nyala, NETRAL.", flush=True)
    print("urutan: " + " -> ".join(f"{n}(+{j:.1f}s)" for n, j in urutan),
          flush=True)
    print("PINDAH KE WINDOW STARDEW. Tombol dikirim otomatis.", flush=True)

    batas = time.monotonic() + TUNGGU
    fokus_sejak = None
    terakhir = None
    while time.monotonic() < batas:
        judul, exe = window.keterangan()
        kini = f"{exe or '(exe?)'} | {judul or '(tanpa judul)'}"
        if kini != terakhir:
            print(f"  di depan: {kini}", flush=True)
            terakhir = kini
        if window.di_depan():
            if fokus_sejak is None:
                fokus_sejak = time.monotonic()
            elif time.monotonic() - fokus_sejak >= STABIL:
                break
        else:
            fokus_sejak = None
        time.sleep(0.2)
    else:
        print("Stardew tidak pernah di depan. Nol tombol dikirim.", flush=True)
        pad.reset()
        pad.update()
        return 1

    for nama, jeda in urutan:
        if not tekan_langkah(pad, vg, window, nama, jeda):
            break

    print("Urutan awal selesai. Pad TETAP HIDUP — ketik tombol berikutnya di sini.",
          flush=True)
    print("contoh: A:0.5   DPAD_DOWN:0.8   STIK_BAWAH:1.5   START:0.3", flush=True)
    print(f"atau tulis ke {BERKAS_PERINTAH}", flush=True)
    print("quit = lepas pad (P2 putus). Alt-tab bebas selama window game di depan "
          "sebelum tiap tombol.", flush=True)

    BERKAS_PERINTAH.parent.mkdir(parents=True, exist_ok=True)
    BERKAS_PERINTAH.unlink(missing_ok=True)

    antrian: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_baca_stdin, args=(antrian,), daemon=True).start()

    def _jalankan_baris(baris: str) -> bool:
        """True = quit diminta."""
        for tok in baris.replace("\ufeff", " ").split():
            try:
                nama, jeda = parse_langkah(tok)
            except ValueError as e:
                print(f"  abaikan {tok!r}: {e}", flush=True)
                continue
            if nama in PERINTAH_KHUSUS:
                print("quit diterima. Pad dilepas.", flush=True)
                return True
            if not langkah_dikenal(nama):
                print(f"  tombol tidak dikenal: {nama}", flush=True)
                continue
            tekan_langkah(pad, vg, window, nama, jeda)
        return False

    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < HIDUP:
            dari_berkas = _baca_berkas_perintah()
            if dari_berkas and _jalankan_baris(dari_berkas):
                return 0
            try:
                baris = antrian.get(timeout=0.4)
            except queue.Empty:
                pad.update()
                continue
            if _jalankan_baris(baris):
                return 0
            pad.update()
    finally:
        pad.reset()
        pad.update()
        print("pad dilepas.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
