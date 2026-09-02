"""Panel craft melayang — display entity vanilla, tanpa plugin/mod di server.

Kebutuhan Bohan (2026-08-07): "aku pengen liat dia crafting dari POV kamera
ini" — UI craft yang terlihat dari luar, setengah tembus pandang, bukan GUI
yang cuma ada di klien pemain yang crafting.

Tidak ada plugin jadi untuk itu. Tapi sejak 1.19.4 Minecraft punya
`item_display`/`text_display`/`block_display`: entity yang menampilkan
item/teks/blok melayang, terlihat SEMUA pemain, dan bisa disummon lewat RCON.
Jadi panelnya dirakit dari entity biasa — tidak perlu mod di klien kamera,
tidak perlu overlay OBS, dan penonton YouTube melihat hal yang sama.

Diverifikasi langsung di server Bohan (Paper 1.21.4), bukan dari dokumentasi:

  * `summon item_display` / `text_display` / `block_display` -> jalan.
  * TIDAK bisa ditempelkan ke pemain — `/ride ... mount <pemain>` dijawab
    "Players can't be ridden". Maka panel MENGIKUTI Arti lewat `tp` berkala.
  * `teleport_duration` (1.20.2+) ADA di 1.21.4 — dites dengan menyummon lalu
    membaca balik NBT-nya. Ini yang membuat panel bergerak mulus walau tp-nya
    cuma ~3x/detik, dan sekaligus jadi animasi barang meluncur dari kanan
    bawah tanpa trik apa pun.
  * `text_display` berisi spasi TIDAK menggambar latar. Sempat dipakai lempeng
    `block_display` (kaca hitam) sebagai latar, lalu DIBUANG: Bohan cuma mau
    ikon + label, dan lempeng itu yang paling kentara menembus blok dunia.

Modul ini MURNI: semua fungsi mengembalikan string perintah. Yang mengirim ke
server adalah bridge (lihat `_craft_panel_*`).
"""

from __future__ import annotations

import math
import re

TAG = "arti_craft"

# Tinggi mata pemain Minecraft dari kaki. Panel dipatok ke MATA, bukan ke
# kaki: dipatok ke ketinggian tetap, panelnya melenceng ke atas layar begitu
# Arti menunduk — dan saat crafting dia PASTI menunduk, karena mineflayer
# memanggil `lookAt(meja)` sebelum mengirim paket klik-kanan.
MATA = 1.62
# Jarak dari mata. DULU 2.0, dan itu salah: saat crafting dia berdiri ~2 blok
# dari meja sambil menghadapinya, jadi panelnya mendarat TEPAT di dalam meja
# (dan pohon di belakangnya). Keluhan operator [date removed]: "item itemnya nembus
# ke dalam crafting table ... kayak ada collision". Didekatkan ke ruang yang
# ditempati badannya sendiri, di situ hampir tidak pernah ada blok.
JARAK = 1.15
# Semua ukuran di bawah ditulis untuk jarak 2.0, lalu diskalakan otomatis.
# Dengan begitu ukuran DI LAYAR tidak berubah waktu JARAK digeser — kalau
# tidak, mendekatkan panel akan membuatnya membesar dan makin bertabrakan.
_K = JARAK / 2.0
SLOT = 0.42 * _K        # jarak antar kotak resep
SKALA_ITEM = 0.36 * _K
SKALA_HASIL = 0.50 * _K
TELEPORT_TICK = 7       # durasi interpolasi pindah (tick); 7 ~ 0,35 dtk

# Titik masuk animasi: kanan bawah, di luar panel. Barang meluncur dari sini
# ke kotaknya masing-masing — permintaan operator: "pas crafting animasi dari
# bawah ke atas item item nya yang mau dicraft ... dia tarik dari kanan bawah".
MASUK_DX = 1.35 * _K
MASUK_DY = -1.30 * _K

_ITEM_RE = re.compile(r"^[a-z_]{2,40}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


# --- geometri --------------------------------------------------------------

def basis(yaw: float, pitch: float):
    """Basis layar (depan, kanan, atas) dari arah pandang.

    `atas` = depan x kanan. Urutan kebalikannya (kanan x depan) menghasilkan
    (0,-1,0) dan membalik panel terbalik — judul jatuh ke bawah layar. Sudah
    kejadian sekali; tes menjaga tandanya.
    """
    ry, rp = math.radians(yaw), math.radians(pitch)
    cp = math.cos(rp)
    f = (-math.sin(ry) * cp, -math.sin(rp), math.cos(ry) * cp)
    r = (math.cos(ry), 0.0, math.sin(ry))
    u = (f[1] * r[2] - f[2] * r[1],
         f[2] * r[0] - f[0] * r[2],
         f[0] * r[1] - f[1] * r[0])
    return f, r, u


def titik(pose, dx: float, dy: float, depan: float = 0.0):
    """Koordinat dunia untuk offset (dx, dy) pada layar, di depan mata Arti."""
    x, y, z, yaw, pitch = pose
    f, r, u = basis(yaw, pitch)
    d = JARAK + depan
    return (x + f[0] * d + r[0] * dx + u[0] * dy,
            y + MATA + f[1] * d + u[1] * dy,
            z + f[2] * d + r[2] * dx + u[2] * dy)


def parse_pose(pos_reply: str, rot_reply: str):
    """Balasan RCON `data get entity <p> Pos|Rotation` -> (x,y,z,yaw,pitch).

    Balasan sukses berbunyi "<nama> has the following entity data: [...]";
    kata "Pos" TIDAK muncul, jadi jangan mengecek keberadaannya di teks.
    """
    if "[" not in (pos_reply or "") or "[" not in (rot_reply or ""):
        raise ValueError("Arti tidak ketemu di server")
    p = pos_reply.split("[", 1)[1].split("]", 1)[0].replace("d", "")
    x, y, z = (float(v.strip()) for v in p.split(","))
    r = rot_reply.split("[", 1)[1].split("]", 1)[0].replace("f", "").split(",")
    return x, y, z, float(r[0]), float(r[1])


# --- resep -> tata letak ---------------------------------------------------

def normalize_grid(grid, size: int):
    """Resep apa pun -> matriks size x size, ditengahkan.

    Bot mengirim `inShape` apa adanya: resep pickaxe 3x3, tapi papan kayu 1x1
    dan obor 1x2. Kalau tidak ditengahkan, resep kecil menempel di pojok kiri
    atas panel dan terlihat seperti bug.
    """
    baris = [list(b) for b in (grid or []) if b is not None][:size]
    tinggi = len(baris)
    lebar = max((len(b) for b in baris), default=0)
    lebar = min(lebar, size)
    atas = (size - tinggi) // 2
    kiri = (size - lebar) // 2
    out = [[None] * size for _ in range(size)]
    for b in range(tinggi):
        for k in range(min(len(baris[b]), lebar)):
            nama = baris[b][k]
            out[atas + b][kiri + k] = nama if nama else None
    return out


def slot_offset(baris: int, kolom: int, size: int):
    """Offset layar satu kotak resep. Kotak tengah grid = pusat layar."""
    tengah = (size - 1) / 2.0
    return ((kolom - tengah) * SLOT, (tengah - baris) * SLOT)


def judul_untuk(item: str) -> str:
    """'wooden_pickaxe' -> 'MEMBUAT WOODEN PICKAXE' (aman untuk NBT)."""
    bersih = _CTRL_RE.sub("", str(item or ""))[:40]
    bersih = re.sub(r"[^A-Za-z0-9_ ]", "", bersih).replace("_", " ").strip()
    return f"  MEMBUAT {bersih.upper()}  " if bersih else "  MEMBUAT  "


# --- perintah --------------------------------------------------------------

def _tags(nama: str) -> str:
    return f'Tags:["{TAG}","{TAG}_{nama}"]'


def _item_display(nama: str, xyz, item: str, skala: float, tp_tick: int) -> str:
    X, Y, Z = xyz
    return (f'summon item_display {X:.3f} {Y:.3f} {Z:.3f} '
            f'{{{_tags(nama)},item:{{id:"minecraft:{item}",count:1}},'
            f'billboard:"center",brightness:{{sky:15,block:15}},'
            f'teleport_duration:{tp_tick},transformation:{{'
            f'scale:[{skala}f,{skala}f,{skala}f],translation:[0f,0f,0f],'
            f'left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f]}}}}')


def bagian(grid, size: int):
    """Daftar (nama_tag, dx, dy, depan) untuk satu panel. Urutan = urutan tp.

    TANPA latar. Dulu ada lempeng kaca hitam di belakang resepnya; dibuang
    atas permintaan Bohan 2026-08-07 ("grey panenya gausah deh, yang nurut
    cuma icon iconnya aja sama label"). Lempeng itu juga yang paling kelihatan
    menembus blok dunia.
    """
    out = [("judul", 0.0, size * SLOT * 0.5 + 0.62 * _K, -0.06)]
    for b in range(size):
        for k in range(size):
            if grid[b][k]:
                dx, dy = slot_offset(b, k, size)
                out.append((f"s{b}{k}", dx, dy, -0.10))
    out.append(("hasil", 0.0, size * SLOT * 0.5 + 0.28 * _K, -0.10))
    return out


def build_show(pose, grid, hasil: str, size: int = 3) -> list[str]:
    """Summon panel. Barang RESEP lahir di kanan bawah — `build_follow`
    berikutnya yang menariknya ke kotak, dan `teleport_duration` yang
    membuatnya meluncur, bukan meloncat."""
    grid = normalize_grid(grid, size)
    cmds = [f"kill @e[tag={TAG}]"]
    for i, (nama, dx, dy, depan) in enumerate(bagian(grid, size)):
        if nama == "judul":
            X, Y, Z = titik(pose, dx, dy, depan)
            cmds.append(
                f'summon text_display {X:.3f} {Y:.3f} {Z:.3f} '
                f'{{{_tags(nama)},text:\'{{"text":"{judul_untuk(hasil)}"}}\','
                f'billboard:"center",shadow:1b,background:2919235584,'
                f'see_through:1b,alignment:"center",'
                f'teleport_duration:{TELEPORT_TICK},transformation:{{'
                f'scale:[{0.45 * _K:.3f}f,{0.45 * _K:.3f}f,{0.45 * _K:.3f}f],'
                f'translation:[0f,0f,0f],'
                f'left_rotation:[0f,0f,0f,1f],right_rotation:[0f,0f,0f,1f]}}}}')
        elif nama == "hasil":
            # Hasil TIDAK meluncur: dia muncul di tempatnya begitu jadi.
            cmds.append(_item_display(nama, titik(pose, dx, dy, depan),
                                      hasil, SKALA_HASIL, TELEPORT_TICK))
        else:
            b, k = int(nama[1]), int(nama[2])
            # Lahir di kanan bawah; durasi beda-beda supaya datangnya
            # bergantian, bukan serentak seperti satu blok.
            cmds.append(_item_display(
                nama, titik(pose, MASUK_DX, MASUK_DY, depan),
                grid[b][k], SKALA_ITEM, TELEPORT_TICK + (i % 4) * 2))
    return cmds


def build_follow(pose, grid, size: int = 3) -> list[str]:
    """Tarik semua bagian ke posisinya relatif Arti sekarang."""
    grid = normalize_grid(grid, size)
    cmds = []
    for nama, dx, dy, depan in bagian(grid, size):
        X, Y, Z = titik(pose, dx, dy, depan)
        cmds.append(f'tp @e[tag={TAG}_{nama},limit=1] {X:.3f} {Y:.3f} {Z:.3f}')
    return cmds


def build_clear() -> list[str]:
    return [f"kill @e[tag={TAG}]"]


def grid_valid(grid, size: int = 3) -> bool:
    """Nama item dari bot dipakai mentah di perintah RCON — saring dulu.

    Bot kita sendiri yang mengirimnya, tapi satu nama aneh berarti perintah
    `summon` rusak dan panelnya bolong tanpa jejak.
    """
    if not isinstance(grid, list) or not grid or len(grid) > size:
        return False
    for baris in grid:
        if not isinstance(baris, list) or len(baris) > size:
            return False
        for nama in baris:
            if nama and not _ITEM_RE.match(str(nama)):
                return False
    return True
