"""Kamera penonton: jaga klien Minecraft asli tetap mengunci pandangan ke Arti.

Kenapa modul ini ada (keputusan 2026-08-07): renderer web (prismarine-viewer)
ternyata alat DEBUG untuk pengembang bot, bukan renderer siaran — tanpa
animasi, tanpa siang-malam, mesh mob rusak, tanpa indikator blok pecah. Semua
itu gratis di klien Minecraft asli, jadi POV siaran pindah ke sana: satu klien
kedua (username offline) berdiri sebagai kamera dan men-spectate Arti.

Masalah satu-satunya jalur itu: **spectate LEPAS begitu targetnya mati.** Di
log 6 & 7 Agustus Arti mati 4 kali dalam ~10 menit, jadi tanpa pemulihan
otomatis kameranya jatuh di menit-menit pertama dan tidak ada yang
membetulkan — padahal justru sesi AFK yang paling butuh.

Di sini isinya fungsi MURNI (bisa dites tanpa server): kapan kamera perlu
dikunci ulang, dan perintah apa yang dikirim. Eksekusi RCON-nya di bridge.
"""

from __future__ import annotations

# Event yang membuat kamera lepas dari Arti. `respawn` adalah yang utama —
# `death` sendiri belum cukup karena saat itu dia belum ada di dunia lagi,
# jadi mengunci ulang di detik kematian akan gagal.
EVENT_PEMICU = frozenset({"respawn", "spawned"})

# Jeda minimal antar-percobaan. Server kadang mengirim respawn + status
# beruntun; tanpa ini satu kematian bisa memicu beberapa perintah RCON.
GAP_MIN_SEC = 2.0


def normalize_name(name) -> str:
    """Username Minecraft: buang spasi & '@' yang sering ikut tersalin."""
    return str(name or "").strip().lstrip("@")


def is_enabled(config: dict | None) -> bool:
    """Kamera hanya aktif kalau namanya diisi — default MATI.

    Sengaja tidak ada kill switch terpisah: nama kosong = tidak ada klien
    kamera = tidak ada yang perlu dijaga. Satu setelan, tidak bisa
    setengah-nyala.
    """
    cfg = config or {}
    return bool(normalize_name(cfg.get("minecraft_spectator_name")))


def is_orbit(config: dict | None) -> bool:
    """Mode kamera "orbit": TANPA /spectate sama sekali — kamera di-teleport
    relatif terhadap badan Arti (`execute at`). Keluhan streamer 2026-08-09 yang
    melahirkannya: (1) sudut pandang spectate tidak pernah berubah, (2) "yang
    paling ganggu pas udah mati ga balik lagi ke badan arti". Orbit lolos dari
    keduanya secara struktural: tidak ada kunci yang bisa putus — saat dia
    mati, `execute at` gagal diam-diam (entity-nya hilang), dan begitu respawn
    teleportnya nempel lagi sendiri. `execute at` juga membawa kamera ikut
    PINDAH DIMENSI.
    """
    return str((config or {}).get("minecraft_camera_mode") or "").strip().lower() == "orbit"


def orbit_offset(t: float, radius: float, period: float,
                 tinggi: float) -> tuple[float, float, float]:
    """Posisi kamera relatif Arti pada detik-t: lingkaran horizontal.

    Murni & deterministik supaya bisa diuji: t yang sama selalu memberi titik
    yang sama, dan satu period penuh kembali ke titik awal.
    """
    import math
    if period <= 0:
        period = 60.0
    sudut = (t % period) / period * 2.0 * math.pi
    return radius * math.cos(sudut), tinggi, radius * math.sin(sudut)


def orbit_command(config: dict | None, dx: float, dy: float, dz: float) -> str:
    """Satu perintah tp relatif; "" kalau nama belum lengkap."""
    cfg = config or {}
    arti = str(cfg.get("minecraft_bot_name") or "").strip()
    kam = str(cfg.get("minecraft_spectator_name") or "").strip()
    if not arti or not kam:
        return ""
    return (f"execute at {arti} run tp {kam} "
            f"~{dx:.2f} ~{dy:.2f} ~{dz:.2f} facing entity {arti} eyes")


def build_commands(config: dict | None) -> list[str]:
    """Perintah RCON untuk mengunci kamera ke Arti. [] kalau kamera mati.

    `spectate` WAJIB dijalankan atas nama si penonton (`execute as ...`) —
    dari console polos server menolaknya karena console bukan entitas yang
    bisa menonton. `gamemode spectator` didahulukan: `spectate` diabaikan
    diam-diam kalau pemainnya belum dalam mode itu.
    """
    cfg = config or {}
    penonton = normalize_name(cfg.get("minecraft_spectator_name"))
    target = normalize_name(cfg.get("minecraft_bot_name")) or "Arti"
    if not penonton or penonton == target:
        return []
    return [
        # Tas kamera dikosongkan tiap kunci ulang. Kalau dia sempat lepas ke
        # survival, dia MEMUNGUT barang (kejadian [date removed]: 8 barang), dan
        # barang di tas kamera muncul sebagai ikon MELAYANG di layar "klik E"
        # karena resource pack menghapus kotak latarnya. Akun kamera memang
        # tidak pernah perlu membawa apa pun.
        f"clear {penonton}",
        f"gamemode spectator {penonton}",
        f"execute as {penonton} run spectate {target}",
    ]


def spectator_gamemode_query(config: dict | None) -> str:
    """Perintah RCON untuk MEMERIKSA kamera masih spectator atau tidak.

    Detak jantung sengaja MEMERIKSA dulu, bukan mengunci ulang membabi buta:
    tiap perintah `spectate` berpotensi menyentak kamera di depan penonton,
    dan itu alasan detaknya dulu dimatikan sama sekali. Membaca NBT tidak
    mengganggu apa pun.
    """
    penonton = normalize_name((config or {}).get("minecraft_spectator_name"))
    return f"data get entity {penonton} playerGameType" if penonton else ""


def gamemode_lepas(balasan: str | None) -> bool:
    """Balasan query gamemode -> kamera SUDAH TIDAK spectator?

    Balasannya berbunyi "<nama> has the following entity data: 3".
    Tidak bisa dibaca (pemain offline / RCON gagal) dianggap TIDAK lepas —
    lebih baik diam daripada menyentak kamera karena salah baca.
    """
    t = (balasan or "").strip()
    if "has the following entity data:" not in t:
        return False
    return t.rsplit(":", 1)[-1].strip() != "3"


def should_resync(ev: dict | None, last_dim: str | None,
                  cfg: dict | None = None) -> tuple[bool, str | None]:
    """(perlu kunci ulang?, dimensi terbaru) dari satu event bot.

    Dua pemicu:
      * respawn/spawned — dia baru ada lagi di dunia, kamera pasti lepas.
      * pindah dimensi — kamera juga lepas saat target pindah world, dan itu
        tidak memancarkan event tersendiri, jadi dibaca dari perubahan `dim`
        di status.
    """
    if not isinstance(ev, dict):
        return False, last_dim
    kind = ev.get("ev")
    if kind in EVENT_PEMICU:
        return True, last_dim
    # Kamera relog: mode spectator-nya ikut tersimpan, tapi target tontonannya
    # hilang. Tidak terlihat dari gamemode, jadi harus dari event ini.
    if kind == "player_join":
        penonton = normalize_name((cfg or {}).get("minecraft_spectator_name"))
        if penonton and normalize_name(ev.get("name")) == penonton:
            return True, last_dim
    if kind == "status":
        dim = ev.get("dim")
        if not dim:
            return False, last_dim
        if last_dim is None:
            # Pertama kali: cukup DICATAT. Mengunci ulang di sini akan
            # menembak tiap status (10 dtk sekali) padahal tidak ada yang
            # berubah — kamera glitch berkala di depan penonton.
            return False, dim
        if dim != last_dim:
            return True, dim
    return False, last_dim


def cooled(last_ts: float, now: float, gap: float = GAP_MIN_SEC) -> bool:
    """Sudah lewat jeda minimal? 0.0 = BELUM PERNAH, bukan 'barusan'.

    Pelajaran bug _cooled di ReactionLimiter: nilai awal 0.0 dibaca sebagai
    'baru saja' membuat percobaan pertama selalu terlewat.
    """
    if last_ts <= 0.0:
        return True
    return (now - last_ts) >= gap


def result_failed(replies: list[str] | None) -> str:
    """Alasan gagal dari balasan server, atau "" kalau berhasil.

    Server membalas kalimat error dengan status yang sama seperti sukses —
    satu-satunya penanda adalah isinya.
    """
    for teks in replies or []:
        t = (teks or "").strip()
        if not t:
            continue
        if "No player was found" in t or "Unknown" in t or "Incorrect" in t:
            return t[:120]
    return ""


# ---------------------------------------------------------------------------
# Momen "klik E" — isi tas Arti tampil di layar kamera
# ---------------------------------------------------------------------------
#
# Kebutuhan operator ([date removed]): "kadang dia perlu 'klik e' buat liat inventory
# kan? nah itu." Bukan panel bikinan sendiri — dia mau GUI Minecraft yang asli.
#
# Tiga hal yang DIUJI di server ini, bukan diasumsikan:
#
# 1. `execute as Kamera run invsee arti_berarti` TIDAK BISA. Semua plugin
#    invsee mendaftarkan perintahnya lewat plugin.yml gaya lama dan menolak
#    pengirim yang bukan Player; `execute as` memberi ProxiedCommandSender,
#    jadi server menjawab "This command can only be used by players!".
#    Dicek di bytecode InvSee++ DAN OpenInv, lalu dikonfirmasi langsung.
#
# 2. `sudo player <pemain> <perintah>` BISA — plugin Sudo memanggil
#    `player.performCommand`, jadi pengirimnya benar-benar pemain itu.
#    Terbukti: jendela `minecraft:generic_9x6` terbuka di klien.
#
# 3. Menutupnya lagi TIDAK ADA di vanilla. Diuji satu per satu dan semuanya
#    gagal: tp, tp jauh, ganti gamemode, spectate, blindness, naik entitas,
#    damage, clear, reload plugin. Yang berhasil cuma PINDAH DIMENSI dan MATI
#    — dua-duanya merusak siaran. Karena itu ada plugin `TutupTas` (30 baris,
#    ditulis sendiri) yang cuma memanggil `Player#closeInventory()`.
#
# Tanpa penutup, GUI-nya menempel di layar penonton SELAMANYA. Jadi
# `open_commands` dan `close_commands` harus selalu berpasangan.

INVSEE_SEC_DEFAULT = 6.0
INVSEE_SEC_MIN, INVSEE_SEC_MAX = 1.0, 30.0


def invsee_enabled(cfg: dict) -> bool:
    """Butuh kamera yang hidup — momen ini terjadi DI LAYAR kamera."""
    return bool(cfg.get("minecraft_invsee_enabled", True)) and is_enabled(cfg)


def invsee_seconds(cfg: dict) -> float:
    """Berapa lama GUI dibiarkan terbuka, dibatasi supaya tidak menutupi
    siaran kelewat lama kalau salah setel."""
    try:
        detik = float(cfg.get("minecraft_invsee_sec", INVSEE_SEC_DEFAULT))
    except (TypeError, ValueError):
        detik = INVSEE_SEC_DEFAULT
    return max(INVSEE_SEC_MIN, min(INVSEE_SEC_MAX, detik))


def invsee_open_commands(cfg: dict) -> list[str]:
    """Perintah membuka isi tas Arti di layar kamera. [] = tidak bisa."""
    if not invsee_enabled(cfg):
        return []
    penonton = normalize_name(cfg.get("minecraft_spectator_name"))
    target = normalize_name(cfg.get("minecraft_bot_name")) or "Arti"
    if not penonton or penonton == target:
        return []
    # 1. Kosongkan tas KAMERA. Resource pack `ZZ-Kamera-UI` membuat kotak
    #    inventory bawah TRANSPARAN, tapi slotnya masih ada — jadi barang apa
    #    pun yang dipegang akun kamera akan tampil sebagai ikon MELAYANG di
    #    udara tanpa kotak. Akun kamera memang tidak pernah butuh barang.
    # 2. Tutup dulu apa pun yang masih terbuka: kalau sesi sebelumnya berhenti
    #    di tengah jalan (bridge mati, RCON putus), GUI lama masih menempel dan
    #    membuka yang baru tidak menghapusnya dari hitungan kita.
    return [f"clear {penonton}",
            f"tutuptas {penonton}",
            f"sudo player {penonton} invsee {target}"]


def invsee_close_commands(cfg: dict) -> list[str]:
    penonton = normalize_name(cfg.get("minecraft_spectator_name"))
    if not penonton:
        return []
    return [f"tutuptas {penonton}"]
