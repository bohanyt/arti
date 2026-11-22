"""arti_gamepad.py — lapis TANGAN + MOTOR Stardew (Fase D).

Tiga lapis rencana `docs/plans/2026-08-25-arti-mabar-stardew.md`:

    SMAPI mod ──state──► Arti (otak) ──niat──► MOTOR ──► vgamepad ──► Player 2
       MATA                  jarang        deterministik      TANGAN

Berkas ini adalah dua lapis kanan. **Nol LLM di sini, selamanya.** Itu bukan
gaya, itu aturan wajib #2: loop game yang memanggil composer/Luna adalah
pemakai penyedia kedua saat siaran hidup, dan kegagalan itu sudah
dibayar dua kali dalam satu malam (11 Agu) — giliran suara cuma punya 12
detik, panggilan composer 15-27 detik, 3x timeout maka breaker tutup dan
SELURUH siaran dijawab model cadangan terkecil. Tes penjaga
`test_motor_tidak_mengimpor_llm` mengunci ini.

## Empat gerbang yang HARUS dilewati sebelum satu tombol pun terkirim

1. `state_layak_motor()` (sudah ada di `arti_stardew.py`, ditulis lebih dulu
   dengan sengaja) — farmer target, bukan pemain utama, state segar, dan
   pemain memang bisa bergerak.
2. **Window fokus** — tombol hanya dikirim saat window Stardew yang di depan.
   Tanpa ini Arti menekan tombol ke Discord, browser, atau editor kode.
3. **Tombol mati darurat** — MENGUNCI. Sekali jatuh, motor tetap mati sampai
   pemiliknya sendiri membangunkan. Tidak ada pemulihan otomatis: kalau
   Bohan menekan rem saat siaran, dia tidak mau tangannya hidup lagi
   sepuluh detik kemudian tanpa dia minta.
4. **Deadman** — utas terpisah yang melepas semua tombol kalau motor berhenti
   melapor. Bukan paranoia: gerbang di dalam loop tidak menolong kalau utas
   motornya yang menggantung, dan tombol yang tertahan berarti Arti lari ke
   tembok sampai ada yang sadar.

## Aturan #3 (invariant kejujuran) diterjemahkan ke sini

`HasilAksi.terverifikasi` menandai apakah keberhasilan dibaca ULANG dari
game atau cuma "tombolnya sudah dikirim". Yang `terverifikasi=False`
**tidak boleh** dilaporkan Arti ke penonton sebagai kejadian. Mod sekarang
mengirim ringkasan inventaris, jadi `ambil()` baru berhasil terverifikasi
kalau `items_total` benar-benar bertambah.
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

from arti_stardew import StardewState, StardewStateStore
from arti_stardew_ui import UiNavigator, UiSnapshot, bangun_ui_snapshot

# --- tata nama tombol -------------------------------------------------------
# Nama internal dipakai apa adanya oleh PadPalsu, jadi tes membaca teks yang
# manusiawi ("A", "kiri") dan tidak pernah menyentuh vgamepad.

TOMBOL_A = "A"          # pakai alat / konfirmasi / bicara NPC
TOMBOL_B = "B"          # batal / tutup menu
TOMBOL_X = "X"          # inventaris
TOMBOL_Y = "Y"          # ganti alat
TOMBOL_START = "START"  # join local co-op; sesudah join membuka GameMenu

# Layar "[recorded remark omitted]" milik Stardew. Dibaca dari dunia
# sebagai `active_menu`, jadi join tidak perlu menebak apa pun.
MENU_JOIN_LOKAL = "LocalCoopJoinMenu"

ATAS, KANAN, BAWAH, KIRI = "atas", "kanan", "bawah", "kiri"

# Stardew `Farmer.FacingDirection`: 0=atas 1=kanan 2=bawah 3=kiri.
FACING = {ATAS: 0, KANAN: 1, BAWAH: 2, KIRI: 3}

# Sumbu tile: x membesar ke KANAN, y membesar ke BAWAH.
_DELTA = {ATAS: (0, -1), KANAN: (1, 0), BAWAH: (0, 1), KIRI: (-1, 0)}

_STIK = {ATAS: (0.0, 1.0), BAWAH: (0.0, -1.0), KIRI: (-1.0, 0.0), KANAN: (1.0, 0.0)}


def _kode_gerbang(alasan: str) -> str:
    teks = alasan.casefold()
    if "rem darurat" in teks:
        return "EMERGENCY_STOP"
    if "fokus" in teks:
        return "FOCUS_REQUIRED"
    if "basi" in teks:
        return "STATE_STALE"
    if "target farmer belum dikunci" in teks:
        return "TARGET_NOT_FOUND"
    return "BLOCKED"


@dataclass(frozen=True)
class HasilAksi:
    """Hasil satu aksi motor.

    `terverifikasi` adalah kolom yang paling penting dibaca: True berarti
    keberhasilan dibaca ulang dari state game; False berarti tombolnya
    terkirim tapi akibatnya tidak terlihat di state. Jangan pernah
    menyuarakan yang False sebagai fakta (aturan #3).
    """

    aksi: str
    berhasil: bool
    terverifikasi: bool
    alasan: str = ""
    tile_awal: tuple[int, int] | None = None
    tile_akhir: tuple[int, int] | None = None
    energi_awal: float | None = None
    energi_akhir: float | None = None
    detik: float = 0.0
    langkah: int = 0
    kode: str = ""

    @property
    def delta_energi(self) -> float | None:
        if self.energi_awal is None or self.energi_akhir is None:
            return None
        return self.energi_akhir - self.energi_awal

    def __str__(self) -> str:
        tanda = "OK " if self.berhasil else "GAGAL"
        cap = "" if self.terverifikasi else " (TAK TERVERIFIKASI)"
        kode = f" [{self.kode}]" if self.kode else ""
        ekor = f" — {self.alasan}" if self.alasan else ""
        return f"[{tanda}] {self.aksi}{cap}{kode} {self.detik:.2f}s{ekor}"


# ---------------------------------------------------------------------------
# TANGAN — backend pad
# ---------------------------------------------------------------------------

class PadBackend(Protocol):
    def tekan(self, tombol: str) -> None: ...
    def lepas(self, tombol: str) -> None: ...
    def stik(self, x: float, y: float) -> None: ...
    def reset(self) -> None: ...
    def ada_input(self) -> bool: ...
    def tutup(self) -> None: ...


class _PadDasar:
    """Pembukuan bersama: apa yang sedang ditahan.

    Dipisah supaya `reset()` benar-benar otoritatif — deadman memakai
    `ada_input()` untuk tahu apakah masih ada yang perlu dilepas, dan itu
    tidak boleh menebak.
    """

    def __init__(self) -> None:
        self._kunci = threading.Lock()
        self._ditahan: set[str] = set()
        self._stik: tuple[float, float] = (0.0, 0.0)

    def ada_input(self) -> bool:
        with self._kunci:
            return bool(self._ditahan) or self._stik != (0.0, 0.0)

    @property
    def ditahan(self) -> frozenset[str]:
        with self._kunci:
            return frozenset(self._ditahan)

    @property
    def stik_sekarang(self) -> tuple[float, float]:
        with self._kunci:
            return self._stik


class PadPalsu(_PadDasar):
    """Pad untuk tes: mencatat, tidak mengirim apa pun ke OS."""

    def __init__(self, *, jam: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._jam = jam
        self.jejak: list[tuple[float, str, object]] = []
        self.tertutup = False

    def _catat(self, apa: str, nilai: object) -> None:
        self.jejak.append((self._jam(), apa, nilai))

    def tekan(self, tombol: str) -> None:
        with self._kunci:
            self._ditahan.add(tombol)
        self._catat("tekan", tombol)

    def lepas(self, tombol: str) -> None:
        with self._kunci:
            self._ditahan.discard(tombol)
        self._catat("lepas", tombol)

    def stik(self, x: float, y: float) -> None:
        with self._kunci:
            self._stik = (x, y)
        self._catat("stik", (x, y))

    def reset(self) -> None:
        with self._kunci:
            self._ditahan.clear()
            self._stik = (0.0, 0.0)
        self._catat("reset", None)

    def tutup(self) -> None:
        self.reset()
        self.tertutup = True

    # --- bantuan tes ---
    def tombol_ditekan(self) -> list[str]:
        return [n for _, a, n in self.jejak if a == "tekan"]  # type: ignore[misc]

    def arah_terkirim(self) -> list[tuple[float, float]]:
        return [n for _, a, n in self.jejak if a == "stik" and n != (0.0, 0.0)]  # type: ignore[misc]


class PadVirtual(_PadDasar):
    """Xbox 360 virtual lewat `vgamepad` (ViGEm).

    Sengaja mengimpor vgamepad DI DALAM `__init__`, bukan di puncak modul:
    modul ini harus bisa diimpor di mesin tanpa driver ViGEm supaya tes
    dan analisis statis tetap jalan.
    """

    def __init__(self) -> None:
        super().__init__()
        import vgamepad as vg

        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._peta = {
            TOMBOL_A: vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            TOMBOL_B: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            TOMBOL_X: vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            TOMBOL_Y: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            TOMBOL_START: vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        }

    def tekan(self, tombol: str) -> None:
        self._pad.press_button(button=self._peta[tombol])
        self._pad.update()
        with self._kunci:
            self._ditahan.add(tombol)

    def lepas(self, tombol: str) -> None:
        self._pad.release_button(button=self._peta[tombol])
        self._pad.update()
        with self._kunci:
            self._ditahan.discard(tombol)

    def stik(self, x: float, y: float) -> None:
        self._pad.left_joystick_float(x_value_float=x, y_value_float=y)
        self._pad.update()
        with self._kunci:
            self._stik = (x, y)

    def reset(self) -> None:
        self._pad.reset()
        self._pad.update()
        with self._kunci:
            self._ditahan.clear()
            self._stik = (0.0, 0.0)

    def tutup(self) -> None:
        try:
            self.reset()
        except Exception:  # noqa: BLE001 — menutup pad tidak boleh melempar
            pass


# ---------------------------------------------------------------------------
# Gerbang window fokus
# ---------------------------------------------------------------------------

class WindowStardew:
    """Gerbang fokus. Mengenali game lewat NAMA PROSES, bukan judul window.

    Versi pertama mencocokkan judul dengan "stardew", dan itu salah — dibayar
    sebelum sempat hidup, ketemu saat membaca biner SMAPI:

    - Console SMAPI berjudul `SMAPI 4.5.2 - running Stardew Valley 1.6.15 ...`
      (string `running Stardew Valley` ada di StardewModdingAPI.dll). Judulnya
      MENGANDUNG "Stardew", jadi gerbangnya lolos untuk window yang salah.
    - Window Explorer yang sedang membuka folder game berjudul persis
      `Stardew Valley`. Lolos juga.

    Keduanya berarti motor menganggap dirinya boleh menekan tombol padahal
    yang di depan bukan gamenya. Nama proses tidak punya ambiguitas itu.

    Judul tetap dibaca — untuk dilaporkan saat gerbangnya menolak, supaya
    salah-deteksi kelihatan dan bukan cuma "ditolak" tanpa sebab. Kalau nama
    proses tidak bisa dibaca (hak akses), gerbang jatuh ke pencocokan judul
    yang KETAT plus penolakan eksplisit terhadap console SMAPI.

    `cari_depan` boleh disuntik di tes — mengembalikan `(judul, nama_exe)`.
    Tidak ada tes yang boleh bergantung pada window sungguhan.
    """

    # Di bawah SMAPI, window GAME dimiliki StardewModdingAPI.exe -- SMAPI-lah
    # proses host-nya. Diukur langsung 29 Agu 2026 terhadap game yang hidup:
    #
    #   window game    : StardewModdingAPI.exe
    #                    "Stardew Valley 1.6.15 - running SMAPI 4.5.2 with 3 mods"
    #   console SMAPI  : "SMAPI 4.5.2 - running Stardew Valley 1.6.15 with 3 mods"
    #
    # Perhatikan: KEDUA judul memuat "Stardew Valley" DAN "SMAPI". Yang
    # membedakan cuma URUTANNYA. Dan keduanya bisa dimiliki proses yang sama,
    # jadi nama proses saja juga tidak cukup. Gerbang ini menuntut dua-duanya:
    # proses yang dikenal, DAN judul yang DIAWALI "Stardew Valley".
    EXE_GAME = ("stardew valley.exe", "stardewmoddingapi.exe")

    def __init__(
        self,
        petunjuk: str = "stardew valley",
        *,
        exe: str | tuple[str, ...] | None = None,
        cari_depan: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        self.petunjuk = petunjuk.lower()
        if exe is None:
            daftar: tuple[str, ...] = self.EXE_GAME
        elif isinstance(exe, str):
            daftar = (exe,)
        else:
            daftar = tuple(exe)
        self.exe = tuple(e.lower() for e in daftar)
        self._cari_depan = cari_depan or self._window_depan

    @staticmethod
    def _window_depan() -> tuple[str, str]:
        """(judul, nama_exe) window terdepan. ctypes, tanpa pywin32."""
        try:
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            hwnd = u32.GetForegroundWindow()
            if not hwnd:
                return "", ""
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            judul = buf.value or ""

            pid = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return judul, ""
            # QUERY_LIMITED_INFORMATION cukup dan tidak menuntut hak tinggi.
            handle = k32.OpenProcess(0x1000, False, pid.value)
            if not handle:
                return judul, ""
            try:
                besar = ctypes.c_ulong(32768)
                nama = ctypes.create_unicode_buffer(besar.value)
                if k32.QueryFullProcessImageNameW(
                    handle, 0, nama, ctypes.byref(besar)
                ):
                    return judul, (nama.value or "").replace("/", chr(92)).rsplit(
                        chr(92), 1
                    )[-1]
            finally:
                k32.CloseHandle(handle)
            return judul, ""
        except Exception:  # noqa: BLE001 — di non-Windows anggap TIDAK fokus
            return "", ""

    def keterangan(self) -> tuple[str, str]:
        """Apa yang benar-benar di depan sekarang. Untuk diagnosa, bukan gerbang."""
        judul, exe = self._cari_depan()
        return (judul or "").strip(), (exe or "").strip()

    def di_depan(self) -> bool:
        judul, exe = self.keterangan()

        # 1. Judul harus DIAWALI "Stardew Valley". Ini yang memisahkan window
        #    game dari console SMAPI -- keduanya memuat kedua kata, cuma
        #    urutannya yang beda.
        if not judul.lower().lstrip().startswith(self.petunjuk):
            return False

        # 2. Dan prosesnya harus dikenal. Tanpa ini, window Explorer yang
        #    membuka folder game (judulnya persis "Stardew Valley") ikut lolos.
        if not exe:
            # FAIL CLOSED. Nama proses tak terbaca = kita tidak tahu siapa yang
            # di depan, dan "tidak tahu" bukan alasan mengirim tombol.
            return False
        return exe.lower() in self.exe

# ---------------------------------------------------------------------------
# Benturan hotkey VTS — pemeriksaan HANYA-BACA
# ---------------------------------------------------------------------------

# Nama tombol controller sebagaimana VTS menuliskannya. VTS menormalkan input
# gamepad dan menamainya ala PlayStation walau pad-nya Xbox, jadi benturan
# terjadi apa pun jenis padnya — mengganti merek pad tidak menolong.
_TRIGGER_CONTROLLER = "controller"

_MODEL_VTS_BAWAAN = (
    r"C:\Program Files (x86)\Steam\steamapps\common\VTube Studio"
    r"\VTube Studio_Data\StreamingAssets\Live2DModels\MyModel_vts\MyModel.vtube.json"
)


def benturan_vts(path: str | None = None) -> list[tuple[str, str]]:
    """Daftar hotkey VTS yang masih terikat ke tombol CONTROLLER.

    Kenapa ini ada: 6 dari 8 hotkey model Arti pernah terikat ke tombol yang
    persis dipakai Stardew (A/B/X/Y/DpadUp/DpadDown). Kalau dibiarkan, tiap
    ayunan cangkul — ribuan kali per sesi — memicu IdleMotion dan animasi
    Arti kejang sepanjang siaran.

    Per 29 Agu 2026 keenamnya sudah dikosongkan, jadi fungsi ini normalnya
    mengembalikan daftar kosong. Ia tetap dipanggil tiap `aktifkan()` karena
    VTS menulis ulang berkas ini sendiri dan binding bisa kembali tanpa ada
    yang sadar.

    HANYA MEMBACA. Jangan pernah menulis `<model>.vtube.json` dari kode: VTS
    memegang konfigurasi model di memori dan menimpanya saat menyimpan atau
    keluar, jadi suntingan berkas hilang diam-diam atau merusak model.
    Perubahan binding dilakukan Bohan lewat UI VTS.
    """
    berkas = path or _MODEL_VTS_BAWAAN
    try:
        with open(berkas, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Tidak ketemu / tidak terbaca bukan alasan memblokir motor. Yang
        # memblokir adalah benturan yang TERBUKTI ada.
        return []
    hasil: list[tuple[str, str]] = []
    for hk in data.get("Hotkeys") or []:
        nama = str(hk.get("Name") or "")
        pemicu = hk.get("Triggers") or {}
        for kunci in ("Trigger1", "Trigger2", "Trigger3"):
            nilai = str(pemicu.get(kunci) or "")
            if nilai.lower().startswith(_TRIGGER_CONTROLLER):
                hasil.append((nama, nilai))
    return hasil


# ---------------------------------------------------------------------------
# Gerbang tangan
# ---------------------------------------------------------------------------

class GerbangTangan:
    """Satu-satunya tempat yang boleh memutuskan "boleh menekan tombol".

    Urutan periksa sengaja dari yang paling murah dan paling kritis:
    rem darurat, saklar aktif, fokus window, baru state game.
    """

    def __init__(
        self,
        store: StardewStateStore,
        *,
        window: WindowStardew | None = None,
        max_age: float = 0.5,
    ) -> None:
        self._store = store
        self._window = window or WindowStardew()
        self._max_age = max_age
        self._kunci = threading.Lock()
        self._aktif = False
        self._darurat = False
        self._alasan_darurat = ""

    # --- saklar ---
    @property
    def aktif(self) -> bool:
        with self._kunci:
            return self._aktif and not self._darurat

    @property
    def darurat(self) -> bool:
        with self._kunci:
            return self._darurat

    @property
    def alasan_darurat(self) -> str:
        with self._kunci:
            return self._alasan_darurat

    def aktifkan(self) -> None:
        with self._kunci:
            if self._darurat:
                raise RuntimeError(
                    "rem darurat masih aktif — panggil bangunkan() dulu, "
                    f"alasan: {self._alasan_darurat}"
                )
            self._aktif = True

    def matikan(self) -> None:
        with self._kunci:
            self._aktif = False

    def rem_darurat(self, alasan: str = "rem darurat ditekan") -> None:
        """MENGUNCI. Tidak ada pemulihan otomatis — itu memang maunya."""
        with self._kunci:
            self._darurat = True
            self._alasan_darurat = alasan
            self._aktif = False

    def bangunkan(self) -> None:
        """Lepas kuncian rem. Hanya pemilik yang boleh memanggil ini."""
        with self._kunci:
            self._darurat = False
            self._alasan_darurat = ""

    # --- izin ---
    def izin_input(self) -> str:
        """Alasan input ditolak tanpa menuntut pemain sedang bebas bergerak."""
        with self._kunci:
            if self._darurat:
                return f"rem darurat aktif ({self._alasan_darurat})"
            if not self._aktif:
                return "motor tidak aktif"
        if not self._window.di_depan():
            return "window Stardew tidak fokus"
        return ""

    def izin(self, player_id: int) -> tuple[StardewState | None, str]:
        alasan = self.izin_input()
        if alasan:
            return None, alasan
        return self._store.state_layak_motor(player_id, max_age=self._max_age)

    def izin_menu(self, player_id: int) -> tuple[StardewState | None, str]:
        """Gerbang menu: semua pagar input kecuali status bebas bergerak."""
        alasan = self.izin_input()
        if alasan:
            return None, alasan
        return self._store.state_layak_input(player_id, max_age=self._max_age)


# ---------------------------------------------------------------------------
# MOTOR
# ---------------------------------------------------------------------------

@dataclass
class SetelanMotor:
    """Angka-angka yang boleh disetel. Bawaannya konservatif.

    `deadman_detik` sengaja lebih longgar dari `max_age` state (0,5 dtk):
    gerbang state sudah menolak snapshot basi tiap langkah, jadi deadman
    cuma mengurus kasus utas motornya sendiri yang menggantung.
    """

    pulsa_detik: float = 0.05          # satu iris tahan-tombol
    tap_detik: float = 0.06            # tap pendek: berputar tanpa melangkah
    macet_detik: float = 1.2           # tile tidak berubah selama ini = macet
    batas_jalan_detik: float = 20.0    # pagar keras satu perintah jalan
    deadman_detik: float = 1.0
    timeout_state: float = 0.5         # menunggu snapshot berikutnya


class MotorStardew:
    """Motor deterministik. Nol LLM.

    Semua penekanan tombol lewat SATU corong (`_periksa`), dan tiap primitif
    melepas semuanya di `finally`. Urutan wajibnya: **baca segar -> putuskan
    -> tekan -> tidur**. Kalau ada jalur kedua yang menekan tombol tanpa lewat
    sini, itu bug — bukan optimasi.
    """

    def __init__(
        self,
        store: StardewStateStore,
        pad: PadBackend,
        gerbang: GerbangTangan,
        *,
        setelan: SetelanMotor | None = None,
        jam: Callable[[], float] = time.monotonic,
        tidur: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._pad = pad
        self._gerbang = gerbang
        self.setelan = setelan or SetelanMotor()
        self._jam = jam
        self._tidur = tidur
        self._detak = jam()
        self._deadman: threading.Thread | None = None
        self._berhenti = threading.Event()
        self._deadman_menyala = 0
        self._ui_navigator = UiNavigator()

    # --- siklus hidup --------------------------------------------------
    def aktifkan(self, *, abaikan_benturan_vts: bool = False) -> list[tuple[str, str]]:
        """Nyalakan motor. Balikan = daftar benturan VTS yang ditemukan.

        Kalau ada benturan dan `abaikan_benturan_vts` False, motor TIDAK
        menyala dan melempar. Bukan cerewet: animasi Arti rapuh dan
        kerusakannya terlihat penonton sepanjang siaran.
        """
        tabrakan = benturan_vts()
        if tabrakan and not abaikan_benturan_vts:
            daftar = ", ".join(f"{n}={t}" for n, t in tabrakan)
            raise RuntimeError(
                "hotkey VTS masih terikat tombol controller — motor menolak "
                f"menyala supaya animasi Arti tidak kejang: {daftar}. "
                "Lepas Trigger-nya lewat UI VTS (JANGAN edit <model>.vtube.json)."
            )
        self._gerbang.aktifkan()
        self._mulai_deadman()
        return tabrakan

    def matikan(self) -> None:
        self._gerbang.matikan()
        self._berhenti.set()
        if self._deadman is not None:
            self._deadman.join(timeout=2.0)
            self._deadman = None
        self._pad.reset()

    def rem_darurat(self, alasan: str = "rem darurat ditekan") -> None:
        """Melumpuhkan tangan SEKETIKA, lalu mengunci."""
        self._gerbang.rem_darurat(alasan)
        self._pad.reset()

    def pasang_hotkey_darurat(self, kombinasi: str = "ctrl+alt+s") -> bool:
        """Daftarkan hotkey global rem darurat.

        Tidak dipanggil otomatis saat import — mendaftarkan hotkey global
        adalah efek samping yang harus diminta pemanggil. Bawaan
        `ctrl+alt+s` dipilih karena bebas: PTT memakai `ctrl+alt+a`, vision
        memakai tombol mouse, dan VTS memakai `LeftControl+M`.
        """
        try:
            import keyboard

            keyboard.add_hotkey(
                kombinasi, lambda: self.rem_darurat(f"hotkey {kombinasi}")
            )
            return True
        except Exception as e:  # noqa: BLE001 — gagal pasang hotkey bukan fatal
            print(f"[Motor] Hotkey darurat '{kombinasi}' gagal dipasang: {e}")
            return False

    def _mulai_deadman(self) -> None:
        if self._deadman is not None:
            return
        self._berhenti.clear()

        def loop() -> None:
            while not self._berhenti.wait(self.setelan.pulsa_detik):
                if not self._pad.ada_input():
                    continue
                if self._jam() - self._detak > self.setelan.deadman_detik:
                    self._deadman_menyala += 1
                    self._pad.reset()

        self._deadman = threading.Thread(
            target=loop, name="stardew-deadman", daemon=True
        )
        self._deadman.start()

    @property
    def deadman_menyala(self) -> int:
        """Berapa kali deadman harus turun tangan. Bukan nol = ada yang salah."""
        return self._deadman_menyala

    def __enter__(self) -> "MotorStardew":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.matikan()
        self._pad.tutup()

    # --- corong tunggal ------------------------------------------------
    def _target(self) -> int | None:
        return self._store.target

    def _periksa(self, player_id: int) -> tuple[StardewState | None, str]:
        """Corong TUNGGAL: periksa gerbang dan perbarui detak. TIDAK tidur.

        Sengaja tidak menggabungkan tidur di sini, dan itu dibayar mahal saat
        dibangun: versi pertama memeriksa gerbang, tidur, lalu mengembalikan
        snapshot PRA-tidur. Akibatnya motor menilai posisinya dari keadaan
        sebelum langkahnya sendiri — dia melewati tujuan, mengoreksi, lalu
        MENGAKU sampai di tile yang sudah dia tinggalkan. Itu bukan sekadar
        off-by-one: itu laporan "berhasil" yang salah, dan pelanggaran
        aturan #3 langsung dari lapis paling bawah.

        Aturannya sekarang: baca segar -> putuskan -> tekan -> tidur. Snapshot
        yang dipakai menilai SELALU lebih baru daripada tombol terakhir.

        Detak diperbarui HANYA kalau gerbang lolos. Jadi begitu gerbang mulai
        menolak, deadman ikut menghitung mundur — tidak perlu jalur pelepasan
        kedua yang bisa lupa dipanggil.
        """
        snap, alasan = self._gerbang.izin(player_id)
        if snap is None:
            return None, alasan
        self._detak = self._jam()
        return snap, ""

    def _lepas_semua(self) -> None:
        self._pad.reset()

    def _snapshot_berikutnya(
        self, player_id: int, sesudah_revisi: int
    ) -> StardewState | None:
        """Tunggu satu snapshot yang lebih baru, memakai jam motor sendiri.

        SENGAJA tidak memakai `StardewStateStore.wait_next`. Metode itu
        menunggu di `threading.Condition` dengan detik jam dinding, sementara
        motor memajukan waktu lewat `tidur` yang disuntik. Mencampur keduanya
        membuat motor punya DUA jalur waktu: di tes berjam palsu, `wait_next`
        tidak pernah kedaluwarsa dan suite menggantung selamanya. Satu jalur
        waktu, satu corong gerbang.
        """
        batas = self._jam() + self.setelan.timeout_state
        while True:
            snap = self._store.latest(player_id)
            if snap is not None and snap.revisi > sesudah_revisi:
                return snap
            if self._jam() >= batas:
                return snap
            self._tidur(self.setelan.pulsa_detik)

    # --- primitif ------------------------------------------------------
    def hadap(self, arah: str) -> HasilAksi:
        """Menghadap satu arah tanpa melangkah. TERVERIFIKASI lewat `facing`."""
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi("hadap", False, False, "target farmer belum dikunci")
        if arah not in _DELTA:
            return HasilAksi("hadap", False, False, f"arah tidak dikenal: {arah}")

        try:
            snap, alasan = self._periksa(pid)
            if snap is None:
                return HasilAksi("hadap", False, False, alasan)
            if snap.facing == FACING[arah]:
                # Jalur yang paling sering dipakai: sesudah jalan_ke, farmer
                # sudah menghadap arah langkah terakhirnya. Tidak menekan apa
                # pun berarti tidak ada risiko ikut melangkah.
                return HasilAksi(
                    "hadap", True, True, "sudah menghadap ke sana",
                    detik=self._jam() - mulai,
                )
            self._pad.stik(*_STIK[arah])
            batas = self._jam() + self.setelan.tap_detik
            while self._jam() < batas:
                self._tidur(self.setelan.pulsa_detik)
                snap, alasan = self._periksa(pid)
                if snap is None:
                    return HasilAksi("hadap", False, False, alasan,
                                     detik=self._jam() - mulai)
            self._lepas_semua()
            akhir = self._snapshot_berikutnya(pid, snap.revisi) or snap
            ok = akhir.facing == FACING[arah]
            return HasilAksi(
                "hadap", ok, True,
                "" if ok else f"facing tetap {akhir.facing}",
                detik=self._jam() - mulai,
            )
        finally:
            self._lepas_semua()

    def jalan_ke(self, x: int, y: int) -> HasilAksi:
        """Jalan ke tile (x, y). TERVERIFIKASI lewat pembacaan ulang tile.

        Ini jalan LURUS per sumbu dengan deteksi macet, **bukan pathfinding**
        — state tidak memuat peta tabrakan, jadi motor tidak tahu ada pagar
        atau pohon sampai dia berhenti bergerak. Kalau macet di kedua sumbu,
        hasilnya GAGAL dengan alasan "terhalang", bukan diam-diam berhenti.
        Jangan menyuruhnya menyeberangi rintangan; itu tugas lapis berikutnya.
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi("jalan_ke", False, False, "target farmer belum dikunci")

        tile_awal: tuple[int, int] | None = None
        langkah = 0
        batas_keras = mulai + self.setelan.batas_jalan_detik
        sumbu_dicoba: set[str] = set()
        macet_sejak = mulai
        tile_terakhir: tuple[int, int] | None = None

        try:
            while True:
                snap, alasan = self._periksa(pid)
                if snap is None:
                    return HasilAksi(
                        "jalan_ke", False, True, alasan,
                        tile_awal=tile_awal, tile_akhir=tile_terakhir,
                        detik=self._jam() - mulai, langkah=langkah,
                    )
                if tile_awal is None:
                    tile_awal = snap.tile
                if snap.tile != tile_terakhir:
                    tile_terakhir = snap.tile
                    macet_sejak = self._jam()
                    sumbu_dicoba.clear()
                    langkah += 1

                dx = x - snap.tile[0]
                dy = y - snap.tile[1]
                if dx == 0 and dy == 0:
                    self._lepas_semua()
                    return HasilAksi(
                        "jalan_ke", True, True,
                        tile_awal=tile_awal, tile_akhir=snap.tile,
                        detik=self._jam() - mulai, langkah=langkah,
                    )

                if self._jam() > batas_keras:
                    return HasilAksi(
                        "jalan_ke", False, True,
                        f"lewat batas {self.setelan.batas_jalan_detik:.0f}s",
                        tile_awal=tile_awal, tile_akhir=snap.tile,
                        detik=self._jam() - mulai, langkah=langkah,
                    )

                # Sumbu x dulu, lalu y — kecuali sumbu itu sudah terbukti macet.
                urutan = []
                if dx:
                    urutan.append((KANAN if dx > 0 else KIRI, "x"))
                if dy:
                    urutan.append((BAWAH if dy > 0 else ATAS, "y"))
                pilihan = next((p for p in urutan if p[1] not in sumbu_dicoba), None)
                if pilihan is None:
                    self._lepas_semua()
                    return HasilAksi(
                        "jalan_ke", False, True, "terhalang di semua sumbu",
                        tile_awal=tile_awal, tile_akhir=snap.tile,
                        detik=self._jam() - mulai, langkah=langkah,
                    )

                arah, sumbu = pilihan
                if self._jam() - macet_sejak > self.setelan.macet_detik:
                    sumbu_dicoba.add(sumbu)
                    macet_sejak = self._jam()
                    self._lepas_semua()
                    # WAJIB tidur juga di cabang ini. Sesudah `_periksa`
                    # berhenti tidur sendiri, satu-satunya yang memajukan
                    # waktu adalah `_tidur` — dan cabang macet yang langsung
                    # `continue` membuat loop berputar tanpa waktu berjalan.
                    # Dengan jam sungguhan itu 100% CPU; dengan jam palsu di
                    # tes, gantung selamanya.
                    self._tidur(self.setelan.pulsa_detik)
                    continue
                self._pad.stik(*_STIK[arah])
                self._tidur(self.setelan.pulsa_detik)
        finally:
            self._lepas_semua()

    def pakai_alat(
        self, *, tahan_detik: float | None = None,
        izinkan_bukan_alat: bool = False,
    ) -> HasilAksi:
        """Tekan tombol pakai-alat. SEBAGIAN terverifikasi.

        Yang benar-benar terbaca dari state cuma perubahan energi. Ayunan
        yang meleset ke tile kosong tetap memakan energi, dan sebagian aksi
        tidak memakai energi sama sekali — jadi delta energi adalah PETUNJUK,
        bukan bukti bahwa tanahnya tercangkul. Karena itu `terverifikasi`
        False: jangan menyuarakan "berhasil mencangkul" dari hasil ini.
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi("pakai_alat", False, False, "target farmer belum dikunci")
        tahan = self.setelan.tap_detik if tahan_detik is None else tahan_detik

        try:
            snap, alasan = self._periksa(pid)
            if snap is None:
                return HasilAksi("pakai_alat", False, False, alasan)

            # Pagar aksi yang tidak bisa dibatalkan. Tombol pakai-alat di
            # Stardew KONTEKSTUAL: sambil memegang barang bukan-alat, tombol
            # yang sama bisa memberi hadiah ke NPC atau memasukkan barang ke
            # shipping bin — langsung terjual, tidak bisa dibatalkan, dan
            # motor tidak melihat NPC maupun bin karena keduanya bukan
            # MOTOR_STATE. Jadi bawaannya menolak; pemanggil yang memang
            # butuh (mis. ambil()) harus memintanya eksplisit.
            #
            # Gerbang ini HANYA menyala kalau mod mengirim inventaris. Dengan
            # DLL lama kita tidak tahu apa yang dipegang, dan menolak semuanya
            # bukan keamanan — cuma bikin motor tidak berguna.
            dipegang = snap.dipegang
            if (
                snap.punya_inventaris
                and dipegang is not None
                and not dipegang.alat
                and not izinkan_bukan_alat
            ):
                return HasilAksi(
                    "pakai_alat", False, False,
                    f"memegang bukan-alat ({dipegang.nama}); "
                    "pakai izinkan_bukan_alat=True kalau memang disengaja",
                    detik=self._jam() - mulai,
                )

            energi_awal = snap.energy
            self._pad.tekan(TOMBOL_A)
            batas = self._jam() + tahan
            while self._jam() < batas:
                self._tidur(self.setelan.pulsa_detik)
                snap, alasan = self._periksa(pid)
                if snap is None:
                    return HasilAksi(
                        "pakai_alat", False, False, alasan,
                        energi_awal=energi_awal, detik=self._jam() - mulai,
                    )
            self._lepas_semua()
            akhir = self._snapshot_berikutnya(pid, snap.revisi) or snap
            return HasilAksi(
                "pakai_alat", True, False,
                "tombol terkirim; akibat di dunia tidak terbaca dari state",
                energi_awal=energi_awal, energi_akhir=akhir.energy,
                detik=self._jam() - mulai,
            )
        finally:
            self._lepas_semua()

    def ambil(self, x: int, y: int) -> HasilAksi:
        """Datangi tile tetangga, hadap ke (x, y), lalu tekan ambil.

        TERVERIFIKASI hanya bila ringkasan inventaris mod menunjukkan
        `items_total` bertambah. Kalau DLL lama tidak mengirim inventaris,
        tombol tetap boleh terkirim tetapi hasil wajib ditandai tidak
        terverifikasi dan tidak boleh diumumkan sebagai fakta (aturan #3).
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi("ambil", False, False, "target farmer belum dikunci")

        snap = self._store.latest(pid)
        if snap is None:
            return HasilAksi("ambil", False, False, "belum pernah ada state")

        # Berdiri di tile TETANGGA lalu menghadap ke target — bukan
        # menginjak tile targetnya sendiri. Dipilih tetangga yang paling
        # dekat dari posisi sekarang.
        kandidat = sorted(
            ((x - dx, y - dy, arah) for arah, (dx, dy) in _DELTA.items()),
            key=lambda t: abs(t[0] - snap.tile[0]) + abs(t[1] - snap.tile[1]),
        )
        bx, by, arah = kandidat[0]

        if snap.tile != (bx, by):
            hasil_jalan = self.jalan_ke(bx, by)
            if not hasil_jalan.berhasil:
                return HasilAksi(
                    "ambil", False, False,
                    f"tidak sampai ke tetangga ({bx},{by}): {hasil_jalan.alasan}",
                    tile_awal=hasil_jalan.tile_awal,
                    tile_akhir=hasil_jalan.tile_akhir,
                    detik=self._jam() - mulai,
                )

        hasil_hadap = self.hadap(arah)
        if not hasil_hadap.berhasil:
            return HasilAksi(
                "ambil", False, False, f"gagal menghadap: {hasil_hadap.alasan}",
                detik=self._jam() - mulai,
            )

        # Berputar memakai dorongan arah yang sama dengan berjalan. Kalau tap
        # sependek `tap_detik` ternyata TETAP memindahkan satu tile di game
        # sungguhan, kita berdiri di tempat yang salah dan aksi berikutnya
        # mengenai tile lain. Belum terukur live — jadi diperiksa, bukan
        # diasumsikan.
        sesudah = self._store.latest(pid)
        if sesudah is not None and sesudah.tile != (bx, by):
            return HasilAksi(
                "ambil", False, False,
                f"posisi bergeser saat menghadap: {(bx, by)} -> {sesudah.tile}",
                tile_akhir=sesudah.tile, detik=self._jam() - mulai,
            )

        sebelum = self._store.latest(pid)
        total_awal = sebelum.items_total if sebelum is not None else None

        hasil_tekan = self.pakai_alat(izinkan_bukan_alat=True)
        if not hasil_tekan.berhasil:
            return HasilAksi(
                "ambil", False, False, hasil_tekan.alasan,
                tile_akhir=(bx, by), detik=self._jam() - mulai,
            )

        akhir = self._snapshot_berikutnya(
            pid, sebelum.revisi if sebelum is not None else -1
        )
        total_akhir = akhir.items_total if akhir is not None else None

        if total_awal is None or total_akhir is None:
            # DLL lama: isi tas tidak terkirim, jadi tidak ada cara jujur
            # untuk tahu barangnya masuk. Tetap TIDAK TERVERIFIKASI.
            return HasilAksi(
                "ambil", True, False,
                "aksi terkirim; mod belum mengirim inventaris, "
                "jadi masuknya barang TIDAK terbukti",
                tile_akhir=(bx, by),
                energi_awal=hasil_tekan.energi_awal,
                energi_akhir=hasil_tekan.energi_akhir,
                detik=self._jam() - mulai,
            )

        naik = total_akhir - total_awal
        return HasilAksi(
            "ambil", naik > 0, True,
            "" if naik > 0 else "isi tas tidak bertambah",
            tile_akhir=(bx, by),
            energi_awal=hasil_tekan.energi_awal,
            energi_akhir=hasil_tekan.energi_akhir,
            detik=self._jam() - mulai,
        )

    def status(self) -> HasilAksi:
        """Potret target terbaru. Hanya membaca; tidak melewati gerbang input."""
        pid = self._target()
        if pid is None:
            return HasilAksi("status", False, False, "target farmer belum dikunci")
        snap = self._store.latest(pid)
        if snap is None:
            return HasilAksi("status", False, False, "belum pernah ada state")
        return HasilAksi(
            "status", True, True,
            f"screen={snap.screen_id} lokasi={snap.location} tile={snap.tile} "
            f"facing={snap.facing} menu={snap.active_menu!r} "
            f"snap={snap.menu_snap_id} label={snap.menu_snap_label!r} "
            f"close={snap.menu_close_id} free={snap.is_player_free} "
            f"move={snap.can_player_move} items_total={snap.items_total}",
        )

    def ui_snapshot(self) -> UiSnapshot | None:
        """Semantic read model terbaru; nol input dan tidak mencuri fokus."""
        pid = self._target()
        if pid is None:
            return None
        state = self._store.latest(pid)
        return bangun_ui_snapshot(state) if state is not None else None

    def ui_open(
        self, *, expected_ui_revision: int | None = None
    ) -> HasilAksi:
        """Buka GameMenu farmhand dengan START dan buktikan dari state baru.

        Ini hanya sah dari dunia bebas. Kalau menu sudah aktif, state basi,
        revision berubah, window kehilangan fokus, atau rem aktif, START tidak
        pernah dikirim. Keyboard Escape tidak dipakai karena itu milik host,
        bukan farmhand controller target.
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi(
                "ui_open", False, False, "target farmer belum dikunci",
                kode="TARGET_NOT_FOUND",
            )

        try:
            state, alasan = self._periksa(pid)
            if state is None:
                return HasilAksi(
                    "ui_open", False, False, alasan,
                    detik=self._jam() - mulai, kode=_kode_gerbang(alasan),
                )
            if state.active_menu is not None:
                return HasilAksi(
                    "ui_open", False, True,
                    f"menu sudah aktif: {state.active_menu}",
                    detik=self._jam() - mulai, kode="BLOCKED",
                )
            if (
                expected_ui_revision is not None
                and state.ui_revision != expected_ui_revision
            ):
                return HasilAksi(
                    "ui_open", False, True,
                    f"UI berubah: expected={expected_ui_revision} "
                    f"actual={state.ui_revision}",
                    detik=self._jam() - mulai, kode="UI_CHANGED",
                )

            revisi_awal = state.revisi
            self._pad.tekan(TOMBOL_START)
            self._tidur(self.setelan.tap_detik)
            self._lepas_semua()

            akhir: StardewState | None = None
            batas = self._jam() + self.setelan.timeout_state
            while True:
                terbaru = self._store.latest(pid)
                if terbaru is not None and terbaru.revisi > revisi_awal:
                    akhir = terbaru
                    if akhir.active_menu == "GameMenu":
                        return HasilAksi(
                            "ui_open", True, True, "GameMenu terbuka",
                            detik=self._jam() - mulai,
                        )
                    if akhir.active_menu is not None:
                        return HasilAksi(
                            "ui_open", False, True,
                            f"menu tak diharapkan sesudah START: "
                            f"{akhir.active_menu}",
                            detik=self._jam() - mulai, kode="BLOCKED",
                        )
                if self._jam() >= batas:
                    break
                self._tidur(self.setelan.pulsa_detik)

            if akhir is None:
                return HasilAksi(
                    "ui_open", False, False,
                    "tidak ada state baru sesudah START",
                    detik=self._jam() - mulai, kode="ACTION_UNVERIFIED",
                )
            return HasilAksi(
                "ui_open", False, True, "UI tetap tertutup sesudah START",
                detik=self._jam() - mulai, kode="NO_OBSERVABLE_CHANGE",
            )
        finally:
            self._lepas_semua()

    def ui_focus(
        self,
        target: str,
        *,
        expected_ui_revision: int | None = None,
        max_transitions: int = 32,
    ) -> HasilAksi:
        """Fokus semantic target via graph/hint/eksplorasi navigation-only."""
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi(
                "ui_focus", False, False, "target farmer belum dikunci",
                kode="TARGET_NOT_FOUND",
            )
        state, alasan = self._gerbang.izin_menu(pid)
        if state is None:
            return HasilAksi(
                "ui_focus", False, False, alasan,
                detik=self._jam() - mulai, kode=_kode_gerbang(alasan),
            )
        if state.active_menu is None:
            return HasilAksi(
                "ui_focus", False, True, "tidak ada menu aktif",
                detik=self._jam() - mulai, kode="NO_ACTIVE_MENU",
            )
        if (
            expected_ui_revision is not None
            and state.ui_revision != expected_ui_revision
        ):
            return HasilAksi(
                "ui_focus", False, True,
                f"UI berubah: expected={expected_ui_revision} actual={state.ui_revision}",
                detik=self._jam() - mulai, kode="UI_CHANGED",
            )
        if not state.menu_components:
            return HasilAksi(
                "ui_focus", False, True, "menu tanpa structural components",
                detik=self._jam() - mulai, kode="UNSUPPORTED_MENU",
            )

        result = self._ui_navigator.focus(
            state,
            target,
            latest=lambda: self._store.latest(pid),
            move=lambda arah, revision: self.navigasi_menu(
                arah, expected_ui_revision=revision
            ),
            max_transitions=max_transitions,
        )
        return HasilAksi(
            "ui_focus", result.ok, result.verified, result.message,
            detik=self._jam() - mulai, langkah=result.transitions,
            kode=result.code,
        )

    def ui_activate(
        self,
        target: str,
        *,
        expected_ui_revision: int | None = None,
        max_transitions: int = 32,
    ) -> HasilAksi:
        """Fokus semantic target, pastikan tetap unik/fokus, lalu tekan A."""
        mulai = self._jam()
        fokus = self.ui_focus(
            target,
            expected_ui_revision=expected_ui_revision,
            max_transitions=max_transitions,
        )
        if not fokus.berhasil:
            return HasilAksi(
                "ui_activate", False, fokus.terverifikasi, fokus.alasan,
                detik=self._jam() - mulai, langkah=fokus.langkah, kode=fokus.kode,
            )
        pid = self._target()
        state, alasan = self._gerbang.izin_menu(pid) if pid is not None else (None, "target farmer belum dikunci")
        if state is None:
            return HasilAksi(
                "ui_activate", False, False, alasan,
                detik=self._jam() - mulai, langkah=fokus.langkah,
                kode=_kode_gerbang(alasan),
            )
        matches = self._ui_navigator.resolve_target(state, target)
        fokus_konsisten = (
            len(matches) == 1
            and state.menu_focus_key == target
            and state.menu_snap_id == matches[0].native_id
        )
        if not fokus_konsisten:
            kode = "TARGET_AMBIGUOUS" if len(matches) > 1 else "UI_CHANGED"
            return HasilAksi(
                "ui_activate", False, True, "target berubah sebelum konfirmasi",
                detik=self._jam() - mulai, langkah=fokus.langkah, kode=kode,
            )
        aktif = self.konfirmasi_menu(expected_ui_revision=state.ui_revision)
        return HasilAksi(
            "ui_activate", aktif.berhasil, aktif.terverifikasi, aktif.alasan,
            detik=self._jam() - mulai, langkah=fokus.langkah, kode=aktif.kode,
        )

    def ui_back(self, *, expected_ui_revision: int | None = None) -> HasilAksi:
        result = self.tutup_menu(expected_ui_revision=expected_ui_revision)
        return HasilAksi(
            "ui_back", result.berhasil, result.terverifikasi, result.alasan,
            detik=result.detik, langkah=result.langkah, kode=result.kode,
        )

    def dialogue_choose(
        self,
        choice: int,
        *,
        expected_ui_revision: int | None = None,
    ) -> HasilAksi:
        if isinstance(choice, bool) or not isinstance(choice, int) or choice < 0:
            return HasilAksi(
                "dialogue_choose", False, True,
                f"choice tidak sah: {choice!r}", kode="TARGET_NOT_FOUND",
            )
        pid = self._target()
        state, alasan = self._gerbang.izin_menu(pid) if pid is not None else (None, "target farmer belum dikunci")
        if state is None:
            return HasilAksi(
                "dialogue_choose", False, False, alasan,
                kode=_kode_gerbang(alasan),
            )
        if state.menu_type != "DialogueBox":
            return HasilAksi(
                "dialogue_choose", False, True,
                f"menu bukan DialogueBox: {state.menu_type}",
                kode="UNSUPPORTED_MENU",
            )
        result = self.ui_activate(
            f"dialogue.choice.{choice}",
            expected_ui_revision=expected_ui_revision,
        )
        return HasilAksi(
            "dialogue_choose", result.berhasil, result.terverifikasi,
            result.alasan, detik=result.detik, langkah=result.langkah,
            kode=result.kode,
        )

    def navigasi_menu(
        self, arah: str, *, expected_ui_revision: int | None = None
    ) -> HasilAksi:
        """Geser kursor controller tepat satu langkah dan baca ulang snap."""
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi(
                "navigasi_menu", False, False, "target farmer belum dikunci"
            )
        if arah not in _STIK:
            return HasilAksi(
                "navigasi_menu", False, False, f"arah tidak dikenal: {arah}"
            )

        def _hasil(ok: bool, alasan: str, kode: str = "") -> HasilAksi:
            return HasilAksi(
                "navigasi_menu", ok, True, alasan,
                detik=self._jam() - mulai, kode=kode,
            )

        try:
            snap, alasan = self._gerbang.izin_menu(pid)
            if snap is None:
                return HasilAksi(
                    "navigasi_menu", False, False, alasan,
                    kode=_kode_gerbang(alasan),
                )
            if snap.active_menu is None:
                return _hasil(False, "tidak ada menu aktif", "NO_ACTIVE_MENU")
            if (
                expected_ui_revision is not None
                and snap.ui_revision != expected_ui_revision
            ):
                return _hasil(
                    False,
                    f"UI berubah: expected={expected_ui_revision} "
                    f"actual={snap.ui_revision}",
                    "UI_CHANGED",
                )
            if not snap.punya_kursor_menu:
                return _hasil(
                    False, "kursor menu tidak terlihat; navigasi buta ditolak",
                    "NO_FOCUS",
                )
            sebelum = snap.menu_snap_id
            self._detak = self._jam()
            self._pad.stik(*_STIK[arah])
            self._tidur(self.setelan.tap_detik)
            self._pad.stik(0.0, 0.0)
            akhir = self._snapshot_berikutnya(pid, snap.revisi)
            if akhir is None or akhir.menu_snap_id == sebelum:
                return _hasil(
                    False, f"snap tetap {sebelum}", "NO_OBSERVABLE_CHANGE"
                )
            return _hasil(
                True,
                f"snap {sebelum} -> {akhir.menu_snap_id} "
                f"label={akhir.menu_snap_label!r}",
            )
        finally:
            self._lepas_semua()

    def konfirmasi_menu(
        self, *, expected_ui_revision: int | None = None
    ) -> HasilAksi:
        """Tekan A pada pilihan controller yang TERLIHAT, lalu baca ulang menu.

        Ini bukan alias `pakai_alat`: saat menu terbuka, state memang menandai
        pemain tidak bebas. Gerbangnya adalah kursor lock-on yang dilaporkan
        mod + rem + fokus. Tanpa `snap_id`, A buta ditolak.
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi(
                "konfirmasi_menu", False, False, "target farmer belum dikunci"
            )

        def _hasil(ok: bool, alasan: str = "", kode: str = "") -> HasilAksi:
            return HasilAksi(
                "konfirmasi_menu", ok, True, alasan,
                detik=self._jam() - mulai, kode=kode,
            )

        try:
            snap, alasan = self._gerbang.izin_menu(pid)
            if snap is None:
                return HasilAksi(
                    "konfirmasi_menu", False, False, alasan,
                    kode=_kode_gerbang(alasan),
                )
            if snap.active_menu is None:
                return _hasil(False, "tidak ada menu aktif", "NO_ACTIVE_MENU")
            if (
                expected_ui_revision is not None
                and snap.ui_revision != expected_ui_revision
            ):
                return _hasil(
                    False,
                    f"UI berubah: expected={expected_ui_revision} "
                    f"actual={snap.ui_revision}",
                    "UI_CHANGED",
                )
            if not snap.punya_kursor_menu:
                return _hasil(False, "kursor menu tidak terlihat; A buta ditolak", "NO_FOCUS")
            self._detak = self._jam()
            self._pad.tekan(TOMBOL_A)
            self._tidur(self.setelan.tap_detik)
            self._pad.lepas(TOMBOL_A)
            akhir = self._snapshot_berikutnya(pid, snap.revisi)
            if akhir is None:
                return HasilAksi(
                    "konfirmasi_menu", False, False,
                    "state sesudah A tidak datang", detik=self._jam() - mulai,
                    kode="ACTION_UNVERIFIED",
                )
            berubah = (
                akhir.ui_revision != snap.ui_revision
                if akhir.ui_revision is not None and snap.ui_revision is not None
                else akhir.ui_fingerprint != snap.ui_fingerprint
            )
            return _hasil(
                berubah,
                "" if berubah else f"UI tetap {snap.active_menu} sesudah A",
                "" if berubah else "NO_OBSERVABLE_CHANGE",
            )
        finally:
            self._lepas_semua()

    # Arah lock-on dicoba berurutan: silang ada di KANAN ATAS, jadi naik dan
    # ke kanan lebih dulu. Bawah/kiri tetap ada supaya kursor bisa keluar dari
    # sudut mati tanpa perlu tahu tata letak tiap menu.
    ARAH_MENU = ((0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0))

    def tutup_menu(
        self,
        *,
        batas_langkah: int = 24,
        expected_ui_revision: int | None = None,
    ) -> HasilAksi:
        """Tutup menu dengan MENAVIGASI kursor lock-on ke tombol silang.

        Versi pertama cuma men-tap B tiga kali. Itu gagal total di game hidup
        29 Agu 2026: menu `GameMenu` milik P2 tidak bergeming walau B ditahan
        0,20 dan 0,35 detik, begitu pula START. Sebabnya bukan tombolnya tidak
        sampai — stik terbukti sampai (kursor terlihat berpindah antar tombol
        UI) — melainkan UI Stardew memakai controller lock-on: kursor digeser
        dengan STIK antar komponen, lalu komponen yang terpilih diaktifkan.

        Karena itu primitif ini butuh umpan balik posisi kursor
        (`menu_snap_id` dari mod). Tanpa laporan itu satu-satunya cara adalah
        menekan tombol secara buta, dan itu ditolak: lihat `punya_kursor_menu`.

        Tetap SENGAJA melewati `state_layak_motor` — selama menu terbuka
        `is_player_free` False, jadi kalau primitif ini tunduk pada gerbang
        state, motor tidak akan pernah bisa keluar dari menu.
        """
        mulai = self._jam()
        pid = self._target()
        if pid is None:
            return HasilAksi("tutup_menu", False, False, "target farmer belum dikunci")

        def _hasil(ok: bool, alasan: str = "") -> HasilAksi:
            return HasilAksi("tutup_menu", ok, True, alasan, detik=self._jam() - mulai)

        try:
            snap, alasan_input = self._gerbang.izin_menu(pid)
            if snap is None:
                return HasilAksi(
                    "tutup_menu", False, False, alasan_input,
                    kode=_kode_gerbang(alasan_input),
                )
            if snap.active_menu is None:
                return _hasil(True)
            if (
                expected_ui_revision is not None
                and snap.ui_revision != expected_ui_revision
            ):
                return HasilAksi(
                    "tutup_menu", False, True,
                    f"UI berubah: expected={expected_ui_revision} "
                    f"actual={snap.ui_revision}",
                    detik=self._jam() - mulai, kode="UI_CHANGED",
                )
            if not snap.punya_kursor_menu:
                return _hasil(
                    False,
                    "mod tidak melaporkan kursor menu (DLL lama?); "
                    "navigasi buta ditolak",
                )
            # B dulu, stik kemudian. Silang GameMenu ada di BINGKAI
            # (`upperRightCloseButton.myID` = 9175502), kursor lock-on ada di
            # HALAMAN (slot 0, tab Inventory 12340, ...). Stick tidak akan
            # pernah membuat snap_id == close_id — terbukti live 29 Agu 20:18,
            # 24 langkah, menu tetap terbuka. B hanya dicoba SESUDAH kursor
            # terlihat: itu bukti pad tertaut. Tap B buta tanpa kursor adalah
            # jebakan yang sudah dibayar sesi yang sama.
            self._detak = self._jam()
            self._pad.tekan(TOMBOL_B)
            self._tidur(self.setelan.tap_detik)
            self._pad.lepas(TOMBOL_B)
            sesudah_b = self._snapshot_berikutnya(pid, snap.revisi)
            if sesudah_b is not None and sesudah_b.active_menu is None:
                return _hasil(True)
            if sesudah_b is not None:
                snap = sesudah_b

            if snap.menu_close_id is None:
                return _hasil(False, f"menu {snap.active_menu} tanpa tombol tutup")

            i_arah = 0
            mandek = 0
            for _ in range(max(1, batas_langkah)):
                kini, alasan_input = self._gerbang.izin_menu(pid)
                if kini is None:
                    return HasilAksi(
                        "tutup_menu", False, False, alasan_input,
                        kode=_kode_gerbang(alasan_input),
                    )
                if kini.active_menu is None:
                    return _hasil(True)

                if kini.kursor_di_tombol_tutup:
                    self._detak = self._jam()
                    self._pad.tekan(TOMBOL_A)
                    self._tidur(self.setelan.tap_detik)
                    self._pad.lepas(TOMBOL_A)
                    sesudah = self._snapshot_berikutnya(pid, kini.revisi)
                    if sesudah is not None and sesudah.active_menu is None:
                        return _hasil(True)
                    continue

                sebelum = kini.menu_snap_id
                x, y = self.ARAH_MENU[i_arah % len(self.ARAH_MENU)]
                self._detak = self._jam()
                self._pad.stik(x, y)
                self._tidur(self.setelan.tap_detik)
                self._pad.stik(0.0, 0.0)
                lanjut = self._snapshot_berikutnya(pid, kini.revisi)

                if lanjut is None or lanjut.menu_snap_id == sebelum:
                    # Arah ini buntu. Ganti arah, dan menyerah kalau SEMUA
                    # arah sudah dicoba dua putaran tanpa kursor bergerak.
                    i_arah += 1
                    mandek += 1
                    if mandek >= len(self.ARAH_MENU) * 2:
                        return _hasil(False, f"kursor mandek di komponen {sebelum}")
                else:
                    mandek = 0

            akhir = self._store.latest(pid)
            ok = akhir is not None and akhir.active_menu is None
            return _hasil(
                ok,
                "" if ok else f"menu masih terbuka: "
                f"{akhir.active_menu if akhir else '?'} "
                f"sesudah {batas_langkah} langkah",
            )
        finally:
            self._lepas_semua()


def buat_motor(
    store: StardewStateStore,
    *,
    palsu: bool = False,
    setelan: SetelanMotor | None = None,
) -> MotorStardew:
    """Rakit motor lengkap. `palsu=True` memakai pad yang tidak mengirim apa pun.

    Motor yang dikembalikan **belum aktif** — pemanggil wajib memanggil
    `aktifkan()`. Itu disengaja: tidak ada jalur yang menyalakan tangan
    sebagai efek samping.
    """
    pad: PadBackend = PadPalsu() if palsu else PadVirtual()
    gerbang = GerbangTangan(store)
    return MotorStardew(store, pad, gerbang, setelan=setelan)


# ---------------------------------------------------------------------------
# CLI probe — untuk bukti live Fase D
# ---------------------------------------------------------------------------

_BANTUAN_AKSI = """\
Aksi ditulis berurutan sebagai argumen posisi:

  jalan:X,Y      jalan ke tile (terverifikasi)
  hadap:ARAH     atas|kanan|bawah|kiri (terverifikasi)
  pakai          tekan tombol pakai-alat (TIDAK terverifikasi)
  ambil:X,Y      dekati, ambil, verifikasi delta items_total bila tersedia
  status         baca menu/kursor/tile/inventaris, nol input
  ui             snapshot semantic JSON, nol input dan tanpa native ID
  ui_open:REV     buka GameMenu farmhand via START; revision WAJIB
  ui_focus:KEY@REV fokus semantic via stik saja; revision WAJIB
  ui_back:REV     tutup menu pada revision yang dibaca lewat `ui`
  menu:ARAH      geser kursor menu satu langkah dan baca snap baru
  konfirmasi_menu tekan A pada pilihan menu yang TERLIHAT (terverifikasi)
  tutup_menu     tutup menu yang terbuka (terverifikasi)
  tunggu:DETIK   diam sejenak
  fokus:DETIK    UJI gerbang fokus SUNGGUHAN selama N detik, nol input

Contoh:
  arti_gamepad.py --target artihand jalan:64,17 hadap:bawah pakai
  arti_gamepad.py --target artihand --sungguhan --join-local --runner
"""


def _revisi_ui(teks: str, nama_aksi: str) -> int:
    teks = teks.strip()
    if not teks:
        raise ValueError(
            f"{nama_aksi} wajib membawa revision dari command `ui`"
        )
    revision = int(teks)
    if revision < 0:
        raise ValueError(f"revision UI tidak boleh negatif: {revision}")
    return revision


def _target_ui_dengan_revisi(arg: str, nama_aksi: str) -> tuple[str, int]:
    target, pemisah, revision = arg.rpartition("@")
    target = target.strip()
    if not pemisah or not target:
        raise ValueError(
            f"format {nama_aksi}: {nama_aksi}:SEMANTIC.KEY@REVISION"
        )
    return target, _revisi_ui(revision, nama_aksi)


def _snapshot_ui_cli(motor: "MotorStardew") -> HasilAksi:
    snapshot = motor.ui_snapshot()
    if snapshot is None:
        return HasilAksi(
            "ui", False, False, "target/state UI belum tersedia",
            kode="TARGET_NOT_FOUND",
        )
    payload = json.dumps(
        asdict(snapshot), ensure_ascii=False, separators=(",", ":")
    )
    return HasilAksi("ui", True, True, payload)


def _urai_aksi(motor: "MotorStardew", token: str) -> HasilAksi:
    nama, _, arg = token.partition(":")
    nama = nama.strip().lower()
    if nama == "jalan":
        x, y = (int(v) for v in arg.split(","))
        return motor.jalan_ke(x, y)
    if nama == "hadap":
        return motor.hadap(arg.strip().lower())
    if nama == "pakai":
        return motor.pakai_alat()
    if nama == "ambil":
        x, y = (int(v) for v in arg.split(","))
        return motor.ambil(x, y)
    if nama == "status":
        return motor.status()
    if nama in ("ui", "ui_snapshot", "ui-snapshot"):
        return _snapshot_ui_cli(motor)
    if nama in ("ui_open", "ui-open"):
        return motor.ui_open(expected_ui_revision=_revisi_ui(arg, "ui_open"))
    if nama in ("ui_focus", "ui-focus"):
        target, revision = _target_ui_dengan_revisi(arg, "ui_focus")
        return motor.ui_focus(target, expected_ui_revision=revision)
    if nama in ("ui_back", "ui-back"):
        return motor.ui_back(expected_ui_revision=_revisi_ui(arg, "ui_back"))
    if nama == "menu":
        return motor.navigasi_menu(arg.strip().lower())
    if nama in ("konfirmasi_menu", "konfirmasi-menu"):
        return motor.konfirmasi_menu()
    if nama in ("tutup_menu", "tutup-menu"):
        return motor.tutup_menu()
    if nama == "tunggu":
        time.sleep(float(arg))
        return HasilAksi("tunggu", True, True, detik=float(arg))
    if nama == "fokus":
        return _uji_fokus(float(arg or 5.0))
    raise SystemExit(f"aksi tidak dikenal: {token}\n\n{_BANTUAN_AKSI}")


def _jalankan_batch(
    motor: "MotorStardew",
    aksi: list[str] | tuple[str, ...],
    *,
    tulis: Callable[[str], None] = print,
) -> int:
    """Jalankan satu batch; berhenti pada kegagalan tanpa mematikan motor."""
    gagal = 0
    for token in aksi:
        hasil = _urai_aksi(motor, token)
        tulis(f"  {hasil}")
        if not hasil.berhasil:
            gagal += 1
            tulis("  -> batch berhenti; runner tetap hidup.")
            break
    return gagal


def _runner_interaktif(
    motor: "MotorStardew",
    *,
    baca: Callable[[str], str] = input,
    tulis: Callable[[str], None] = print,
) -> int:
    """Terima batch dari stdin sambil mempertahankan pad yang sama.

    Satu baris = satu batch token aksi seperti CLI biasa. Kegagalan hanya
    menghentikan sisa baris itu; proses, receiver, motor, dan pad tetap hidup
    supaya operator bisa mengoreksi fokus atau tujuan tanpa membuat P2 yatim.
    """
    tulis(
        "[Motor] Runner pad persisten siap. Satu baris = satu batch; "
        "ketik 'keluar' untuk menutup pad."
    )
    while True:
        try:
            baris = baca("motor> ").strip()
        except EOFError:
            tulis("[Motor] EOF; runner berhenti.")
            return 0
        if not baris:
            continue
        if baris.lower() in ("keluar", "exit", "quit"):
            tulis("[Motor] Runner berhenti; pad akan ditutup.")
            return 0
        try:
            _jalankan_batch(motor, tuple(baris.split()), tulis=tulis)
        except (SystemExit, ValueError) as exc:
            tulis(f"[Motor] Perintah ditolak: {exc}")


def _uji_fokus(detik: float) -> HasilAksi:
    """Ukur gerbang fokus SUNGGUHAN tanpa mengirim satu tombol pun.

    Ada karena kalau tidak, gerbang fokus cuma bisa diuji dengan menyalakan
    pad sungguhan — artinya satu-satunya cara membuktikan pagarnya bekerja
    adalah dengan melewatinya. Aksi ini memisahkan keduanya: pindah ke
    window Stardew selama hitungannya berjalan, lalu baca hasilnya.
    """
    window = WindowStardew()
    mulai = time.monotonic()
    total = 0
    kena = 0
    terlihat: dict[str, int] = {}
    print(f"  [fokus] {detik:.0f} detik — pindah ke window Stardew SEKARANG...")
    while time.monotonic() - mulai < detik:
        total += 1
        judul, exe = window.keterangan()
        kunci = f"{exe or '(exe tak terbaca)':<24} {judul or '(tanpa judul)'}"
        terlihat[kunci] = terlihat.get(kunci, 0) + 1
        if window.di_depan():
            kena += 1
        time.sleep(0.1)

    persen = (100.0 * kena / total) if total else 0.0
    print(f"  [fokus] Stardew di depan {kena}/{total} sampel ({persen:.0f}%)")
    print(f"  [fokus] yang benar-benar di depan (exe + judul):")
    for k, n in sorted(terlihat.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  [fokus]   {n:>4}x  {k}")
    return HasilAksi(
        "fokus", kena > 0, True,
        "" if kena else "window Stardew TIDAK PERNAH terdeteksi di depan — "
                        "motor sungguhan akan menolak semua aksi",
        detik=time.monotonic() - mulai,
    )


def _buat_window_kering() -> WindowStardew:
    """Stub fokus dry-run dengan satu nama EXE produksi yang sah."""
    return WindowStardew(
        cari_depan=lambda: (
            "stardew valley [stub kering]",
            WindowStardew.EXE_GAME[0],
        )
    )


def _join_terpandu(
    pad: PadBackend,
    window: WindowStardew,
    *,
    baca_menu: Callable[[], str | None],
    sudah_join: Callable[[], bool] | None,
    tidur: Callable[[float], None],
    jam: Callable[[], float],
    tahan_detik: float,
    jeda_start: float,
    jeda_pantau: float,
    batas_join: float,
    batas_konfirmasi: int,
    batas_batal: int,
    catat: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Join P2 dengan MEMBACA layar, bukan menembak urutan tombol buta.

    KENAPA BUKAN `START -> A` (dibayar 30 Agu 2026): layar join lokal
    berbunyi "[recorded remark omitted]" dengan "[recorded remark omitted]" di bawahnya.
    Tombol konfirmasinya START — A tidak berarti apa-apa di sana. Urutan buta
    lama menekan A, layarnya diam, lalu seluruh join menyerah pada timeout
    hello. Terbaca live dari dunia, bukan dari layar:

        active_menu = "LocalCoopJoinMenu"   selama 15 dtk berturut-turut
        menu.components = []                 (nol komponen, jadi nol kursor)
        farmer kedua                          tidak pernah muncul

    TIDAK ADA NAMA KARAKTER DI SINI, sengaja. Yang dipakai cuma jenis menu
    dan bukti hello farmhand, supaya world/profil boleh berganti tanpa
    menyentuh kode ini.

    Menu lain (mis. `GameMenu`, artinya pad terikat ke P1 dan START membuka
    menu pause) berhenti FAIL CLOSED. Menekan B untuk "membereskan" menu
    milik orang lain bukan tugas fungsi join.
    """
    def tap(tombol: str, jeda: float) -> tuple[bool, str]:
        if not window.di_depan():
            return False, f"fokus Stardew hilang sebelum tombol {tombol}"
        try:
            pad.tekan(tombol)
            tidur(tahan_detik)
            pad.lepas(tombol)
            tidur(jeda)
        except Exception as exc:  # noqa: BLE001 — fail closed, pad wajib netral
            return False, f"gagal mengirim tombol {tombol}: {exc}"
        return True, ""

    batas = jam() + batas_join
    konfirmasi = 0
    pembuka_terkirim = False
    batal = 0
    menu_sebelumnya: object = object()
    while True:
        if sudah_join is not None and sudah_join():
            pad.reset()
            return True, "P2 join terbukti dari hello farmhand"
        if jam() >= batas:
            pad.reset()
            return False, (
                f"P2 tidak pernah join dalam {batas_join:.0f} dtk "
                f"(batal={batal}, START pembuka={pembuka_terkirim}, "
                f"konfirmasi={konfirmasi})"
            )
        menu = baca_menu()
        if catat is not None and menu != menu_sebelumnya:
            catat(f"menu={menu!r} batal={batal} pembuka={pembuka_terkirim} "
                  f"konfirmasi={konfirmasi}")
            menu_sebelumnya = menu
        if menu == MENU_JOIN_LOKAL and not pembuka_terkirim:
            # LAYAR JOIN MILIK CONTROLLER YANG MEMBUKANYA (dibayar 30 Agu
            # 2026). Layar ini sudah terbuka sebelum pad kita ada — dibuka
            # pad lain atau sisa percobaan sebelumnya. START dari pad kita
            # jatuh ke slot yang salah dan layarnya diam: terbukti live,
            # tiga START berturut-turut, `active_menu` tidak bergerak dan
            # farmer kedua nol. Jadi batalkan dulu dengan B — itu tombol
            # yang layarnya sendiri tawarkan ('[recorded remark omitted]'), bukan tebakan —
            # supaya START berikutnya membuka layar milik pad KITA.
            if batal >= batas_batal:
                pad.reset()
                return False, (
                    f"{MENU_JOIN_LOKAL} tidak bisa dibatalkan setelah "
                    f"{batal} kali B; pad kemungkinan tidak terdaftar"
                )
            batal += 1
            ok, kenapa = tap(TOMBOL_B, jeda_start)
            if not ok:
                pad.reset()
                return False, kenapa
            continue
        if menu == MENU_JOIN_LOKAL:
            if konfirmasi >= batas_konfirmasi:
                pad.reset()
                return False, (
                    f"{MENU_JOIN_LOKAL} tidak menerima START "
                    f"setelah {konfirmasi} kali"
                )
            konfirmasi += 1
            ok, kenapa = tap(TOMBOL_START, jeda_start)
            if not ok:
                pad.reset()
                return False, kenapa
            if sudah_join is None:
                # `--paksa-join` mencabut satu-satunya bukti yang kita
                # punya. Kirim START sekali lalu berhenti jujur: sukses
                # yang TIDAK terverifikasi, bukan sukses.
                pad.reset()
                return True, (
                    f"START dikirim ke {MENU_JOIN_LOKAL}; "
                    "join TIDAK terverifikasi (--paksa-join)"
                )
            continue
        if menu is None:
            if not pembuka_terkirim:
                pembuka_terkirim = True
                ok, kenapa = tap(TOMBOL_START, jeda_start)
                if not ok:
                    pad.reset()
                    return False, kenapa
                continue
            tidur(jeda_pantau)
            continue
        pad.reset()
        return False, (
            f"menu tak diharapkan saat join: {menu} "
            f"(batal={batal}, pembuka={pembuka_terkirim}, "
            f"konfirmasi={konfirmasi})"
        )


def _join_lokal(
    pad: PadBackend,
    window: WindowStardew,
    *,
    tidur: Callable[[float], None] = time.sleep,
    jam: Callable[[], float] = time.monotonic,
    tahan_detik: float = 0.15,
    jeda_start: float = 2.0,
    jeda_a: float = 1.0,
    stabil_detik: float = 1.0,
    batas_fokus: float = 20.0,
    sudah_join: Callable[[], bool] | None = None,
    tunggu_cek_join: float = 2.0,
    baca_menu: Callable[[], str | None] | None = None,
    batas_join: float = 25.0,
    jeda_pantau: float = 0.2,
    batas_konfirmasi: int = 3,
    batas_batal: int = 2,
    catat: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Masukkan pad yang SAMA ke local co-op lewat ``START -> A``.

    Ini sengaja bukan proses helper terpisah. Mematikan helper berarti
    melepas controller P2; membuat controller baru sesudah P2 terdeteksi
    berarti motor menggerakkan slot yang salah. Fokus dibaca ulang sebelum
    SETIAP tombol dan semua input dilepas pada jalur gagal.

    KENAPA ADA MASA TENANG SEBELUM TOMBOL PERTAMA (dibayar 29 Agu 2026):
    versi pertama menekan START ~0 detik sesudah `PadVirtual()` dibuat. Pad
    virtualnya nyata dan tombolnya benar-benar terkirim -- tapi Stardew belum
    sempat mendaftarkan controller yang baru muncul, jadi `LocalCoopJoinMenu`
    diam saja dan P2 tidak pernah join. Terbukti di game hidup: layar co-op
    terbuka (`active_menu: LocalCoopJoinMenu`), mod sehat ~10 event/detik,
    START -> A terkirim, farmer kedua nol.

    Helper lama `scripts/pad_urutan.py` tidak pernah kena ini karena ia
    memoles fokus dulu (`STABIL = 1.0`) sambil menunggu manusia pindah
    window -- jedanya kebetulan, bukan disengaja. Di sini jedanya dibuat
    eksplisit: fokus Stardew harus stabil `stabil_detik` sebelum tombol
    pertama. Efek sampingnya menguntungkan -- pemanggil tidak perlu lagi
    menebak kapan window game akan aktif.
    """
    pad.reset()

    # Kalau target sudah dilaporkan, tidak ada tombol join yang perlu dikirim.
    # Cek ini SEBELUM menuntut fokus: runner background tidak perlu mencuri
    # foreground hanya untuk membuktikan bahwa ia tidak akan menekan START.
    if sudah_join is not None:
        batas_cek = jam() + tunggu_cek_join
        while True:
            if sudah_join():
                pad.reset()
                return True, "P2 sudah join; urutan START -> A dilewati"
            if jam() >= batas_cek:
                break
            tidur(0.2)

    # Masa tenang: pad sudah netral di OS DAN game sudah di depan cukup lama.
    # Gagal = nol tombol, bukan "coba saja siapa tahu".
    fokus_sejak: float | None = None
    batas = jam() + batas_fokus
    while True:
        if window.di_depan():
            if fokus_sejak is None:
                fokus_sejak = jam()
            if jam() - fokus_sejak >= stabil_detik:
                break
        else:
            fokus_sejak = None
        if jam() >= batas:
            pad.reset()
            return False, (
                f"Stardew tidak pernah fokus stabil {stabil_detik:.1f} dtk "
                f"dalam {batas_fokus:.0f} dtk"
            )
        tidur(0.2)

    # JANGAN JOIN KALAU SUDAH JOIN (dibayar 29 Agu 2026, percobaan ke-4 & 5).
    #
    # BATAS SAHNYA (dibayar sejam kemudian, percobaan ke-7 s/d 9): "[recorded remark omitted]" TIDAK sama dengan "pad kita mengendalikannya".
    # Mencabut pad meninggalkan farmhand yatim — masih dilaporkan mod, tapi
    # tanpa controller. Satu-satunya jalan menautkan pad baru adalah lewat
    # `LocalCoopJoinMenu`; begitu layar itu ditutup, SEMUA tombol dari pad
    # baru hilang tanpa jejak (terbukti: B@0.20/0.35 dan START@0.15/0.35,
    # plus pad didiamkan netral 8 detik, nol perubahan `active_menu`).
    # Karena itu `--paksa-join` ada: pemanggil yang TAHU pad-nya belum
    # tertaut boleh menuntut urutan START -> A dikirim tetap.
    # Menutup pad virtual TIDAK melepas farmhand yang sudah masuk, dan pad
    # baru otomatis terikat lagi ke slot P2 yang sama. Akibatnya START bukan
    # lagi "join" melainkan MEMBUKA MENU PAUSE milik P2 -- lalu A mengklik
    # sesuatu di dalam menu itu. Terbaca dari dunia sebagai:
    #
    #   artihand -> menu='GameMenu' free=False move=False
    #
    # dan seluruh aksi berikutnya ditolak `state_layak_motor`. Menunggu lebih
    # lama tidak menolong: itu menu sungguhan, bukan transisi sesaat.
    # Kalau pemanggil bisa membaca menu dari dunia, pakai jalur terpandu.
    # Urutan buta di bawah hanya bertahan untuk pemanggil tanpa state
    # (dan untuk tes yang memang menguji urutan buta itu sendiri).
    if baca_menu is not None:
        return _join_terpandu(
            pad,
            window,
            baca_menu=baca_menu,
            sudah_join=sudah_join,
            tidur=tidur,
            jam=jam,
            tahan_detik=tahan_detik,
            jeda_start=jeda_start,
            jeda_pantau=jeda_pantau,
            batas_join=batas_join,
            batas_konfirmasi=batas_konfirmasi,
            batas_batal=batas_batal,
            catat=catat,
        )

    for tombol, jeda in ((TOMBOL_START, jeda_start), (TOMBOL_A, jeda_a)):
        if not window.di_depan():
            pad.reset()
            return False, f"fokus Stardew hilang sebelum tombol {tombol}"
        try:
            pad.tekan(tombol)
            tidur(tahan_detik)
            pad.lepas(tombol)
            tidur(jeda)
        except Exception as exc:  # noqa: BLE001 — fail closed, pad wajib netral
            pad.reset()
            return False, f"gagal mengirim tombol {tombol}: {exc}"
    pad.reset()
    return True, "urutan START -> A terkirim dari controller motor"


def main() -> int:
    import argparse

    from arti_stardew import DEFAULT_PORT, StardewReceiver

    p = argparse.ArgumentParser(
        description="Probe motor Stardew (Fase D). BAWAAN = kering, nol input.",
        epilog=_BANTUAN_AKSI,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("aksi", nargs="*", help="urutan aksi; lihat epilog")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--target", default=None, help="nama farmer P2, mis. artihand")
    p.add_argument(
        "--sungguhan", action="store_true",
        help="pakai gamepad virtual SUNGGUHAN. Tanpa ini, nol input ke OS.",
    )
    p.add_argument(
        "--join-local", action="store_true",
        help="join P2 lewat START lalu A memakai controller motor yang sama",
    )
    p.add_argument(
        "--paksa-join", action="store_true",
        help=(
            "kirim START -> A walau farmer target sudah terlihat di dunia. "
            "Dipakai saat pad sebelumnya dicabut sehingga farmhand jadi yatim "
            "— butuh layar Start Local Co-op terbuka."
        ),
    )
    p.add_argument(
        "--runner", action="store_true",
        help="pertahankan receiver + motor + pad dan terima batch dari stdin",
    )
    p.add_argument("--hotkey", default="ctrl+alt+s", help="hotkey rem darurat")
    p.add_argument(
        "--abaikan-benturan-vts", action="store_true",
        help="nyalakan walau hotkey VTS masih terikat controller (TIDAK disarankan)",
    )
    p.add_argument("--tunggu-hello", type=float, default=60.0)
    args = p.parse_args()

    if not args.aksi and not args.runner:
        print(_BANTUAN_AKSI)
        return 2

    butuh_farmer = args.runner or any(
        not t.split(":")[0].strip().lower() in ("fokus", "tunggu")
        for t in args.aksi
    )
    if args.join_local and not args.sungguhan:
        p.error("--join-local hanya boleh bersama --sungguhan")
    if args.sungguhan and butuh_farmer and not args.join_local:
        p.error("aksi farmer sungguhan wajib memakai --join-local agar controller P2 sama")

    store = StardewStateStore()
    receiver = StardewReceiver(store.catat, port=args.port)
    receiver.start()
    host, port = receiver.address
    kering = not args.sungguhan
    print(f"[Motor] Mendengarkan {host}:{port}")
    print(f"[Motor] Mode: {'KERING (nol input ke OS)' if kering else 'SUNGGUHAN'}")
    pad: PadBackend | None = None
    window: WindowStardew | None = None
    motor: MotorStardew | None = None

    try:
        if args.join_local:
            pad = PadVirtual()
            window = WindowStardew()
            def _menu_dunia() -> str | None:
                """Menu yang sedang terbuka menurut mod, bukan menurut niat.

                Sebelum P2 ada, satu-satunya yang melapor adalah pemain
                utama — dan layar join lokal memang muncul di layarnya.
                Nama karakter tidak pernah dipakai supaya world boleh
                berganti tanpa menyentuh kode ini.
                """
                for ident in store.semua_identitas():
                    snap = store.latest(ident.player_id)
                    if snap is None:
                        continue
                    if snap.active_menu == MENU_JOIN_LOKAL:
                        return snap.active_menu
                utama = [
                    i for i in store.semua_identitas() if i.is_main_player
                ]
                if len(utama) != 1:
                    return None
                snap = store.latest(utama[0].player_id)
                return snap.active_menu if snap is not None else None

            print(
                "[Motor] Join local terpandu: baca menu dari mod, "
                "START hanya saat layar join terbuka."
            )
            berhasil_join, alasan_join = _join_lokal(
                pad,
                window,
                baca_menu=_menu_dunia,
                catat=lambda pesan: print(f"[Join] {pesan}", flush=True),
                sudah_join=(
                    None
                    if args.paksa_join
                    else lambda: store.pilih_target(args.target) is not None
                ),
            )
            if not berhasil_join:
                print(f"[Motor] Join local dibatalkan: {alasan_join}. Fail closed.")
                return 1
            print(f"[Motor] {alasan_join}; menunggu hello P2 sebagai bukti.")

        # Aksi yang tidak menyentuh farmer tidak perlu menunggu mod connect.
        # Kalau tidak begini, menguji gerbang fokus jadi menuntut game hidup
        # — padahal justru itu yang mau dipisah.
        batas = time.monotonic() + args.tunggu_hello
        while butuh_farmer and store.pilih_target(args.target) is None:
            if time.monotonic() > batas:
                print("[Motor] Target farmer tidak pernah terkunci. Fail closed.")
                return 1
            time.sleep(0.2)
        pid = store.target
        if butuh_farmer:
            ident = store.identitas(pid) if pid is not None else None
            print(
                f"[Motor] Target terkunci: "
                f"{ident.player_name if ident else '?'} ({pid})"
            )
        else:
            print("[Motor] Aksi tidak menyentuh farmer — tidak menunggu mod.")

        # Mode kering memakai stub window lewat titik suntik yang memang
        # sudah ada. Gerbangnya TIDAK diubah: pad sungguhan selamanya
        # dipasangkan dengan pemeriksa fokus sungguhan, dan itu satu-satunya
        # pasangan yang bisa mengirim input ke OS.
        if pad is not None and window is not None:
            pass  # controller yang baru join P2 wajib dipakai ulang oleh motor
        elif kering:
            pad = PadPalsu()
            window = _buat_window_kering()
            print("[Motor] Gerbang fokus di-stub (mode kering, nol input).")
        else:
            pad = PadVirtual()
            window = WindowStardew()
        assert pad is not None and window is not None
        gerbang = GerbangTangan(store, window=window)
        motor = MotorStardew(store, pad, gerbang)

        tabrakan = motor.aktifkan(abaikan_benturan_vts=args.abaikan_benturan_vts)
        if tabrakan:
            print(f"[Motor] PERINGATAN benturan VTS diabaikan: {tabrakan}")
        if motor.pasang_hotkey_darurat(args.hotkey):
            print(f"[Motor] Rem darurat: {args.hotkey}")

        try:
            gagal = _jalankan_batch(motor, tuple(args.aksi))
            if args.runner:
                return _runner_interaktif(motor)
        finally:
            motor.matikan()

        if kering and isinstance(pad, PadPalsu):
            tekan = pad.tombol_ditekan()
            print(f"[Motor] Kering: {len(pad.jejak)} kejadian pad, "
                  f"tombol={tekan or 'nol'} — tidak ada yang dikirim ke OS.")
        if motor.deadman_menyala:
            print(f"[Motor] Deadman turun tangan {motor.deadman_menyala}x — periksa.")
        return 1 if gagal else 0
    except KeyboardInterrupt:
        print("\n[Motor] Dihentikan.")
        return 130
    finally:
        if pad is not None:
            pad.tutup()
        receiver.stop()


if __name__ == "__main__":
    raise SystemExit(main())
