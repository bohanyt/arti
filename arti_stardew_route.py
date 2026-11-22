"""Pencarian rute kardinal murni di atas graf berarah yang dipasok pemanggil.

Fungsi ini adalah kernel matematika: BFS berbiaya satuan, tanpa IO, tanpa
kotak masuk, tanpa API game, tanpa cache, dan tanpa model dunia persisten.
Dia tidak mengirim gerakan, tidak mengarang `world_press`, dan tidak
mengklaim farmer sudah tiba.

Sisi yang boleh dilalui hanyalah sisi kardinal berarah yang dipasok
eksplisit. Dua ubin yang kelihatan bersebelahan, atau dua hasil inspect
yang "bersih", TIDAK membuat sisi. Sisi balik, diagonal, teleport, dan
tetangga implisit tidak pernah ditambahkan.

Pemanggil masa depan (adapter traversal), bukan fungsi ini, yang memutuskan
sisi mana yang sah di dunia nyata. Kernel ini tidak mensertifikasi keamanan,
kesegaran, geometri bounding-box, atau kelengkapan sisi yang dipasok.
Khususnya, hasil destinasi `inspect_stand` saja tidak boleh diam-diam
dijadikan wewenang menyeberang.

Navigasi tanaman nanti menarget posisi berdiri yang bersebelahan dengan
tanaman; pemanggil yang memasok himpunan tujuan. Fungsi ini tidak menganggap
ubin tanaman itu sendiri sebagai tujuan berdiri, dan tidak memilih tanaman.

Batas masukan (ditolak sebelum salinan/pencarian mahal):
- paling banyak 81 simpul di jendela lokal 9x9 (span x dan y masing-masing
  paling banyak 9 ubin);
- paling banyak 4 sisi keluar per simpul;
- iterasi masukan ikut dibatasi; generator tak terbatas ditolak.

Tie-break deterministik, tidak bergantung urutan penyisipan set/dict:
- tujuan berjarak terpendek sama: `(x, y)` leksikografis terkecil;
- pohon BFS: tetangga keluar diekspansi urut `(x, y)`; induk simpul adalah
  pendahulu pertama yang menemukannya.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

WINDOW_SPAN_MAX = 9
NODE_MAX = WINDOW_SPAN_MAX * WINDOW_SPAN_MAX
OUT_DEGREE_MAX = 4
EDGE_INPUT_MAX = NODE_MAX * OUT_DEGREE_MAX

FOUND = "FOUND"
EMPTY_GOALS = "EMPTY_GOALS"
UNREACHABLE = "UNREACHABLE"
PATH_LIMIT_EXHAUSTED = "PATH_LIMIT_EXHAUSTED"
INVALID_INPUT = "INVALID_INPUT"

_DELTA_KE_ARAH = {
    (1, 0): "right",
    (-1, 0): "left",
    (0, 1): "down",
    (0, -1): "up",
}


@dataclass(frozen=True)
class HasilRute:
    """Putusan kernel. `found` hanya True jika ada jalur di graf yang dipasok."""

    status: str
    found: bool
    path: tuple[tuple[int, int], ...]
    steps: tuple[str, ...]
    chosen_goal: tuple[int, int] | None
    visited_count: int
    reason: str | None = None


def cari_rute_kardinal(
    nodes: object,
    edges: object,
    start: object,
    goals: object,
    max_path_length: object,
) -> HasilRute:
    """Cari jalur kardinal terpendek dari `start` ke salah satu `goals`.

    `nodes`: koleksi terhingga koordinat integer `(x, y)` nonnegatif.
    `edges`: koleksi terhingga sisi berarah `((x1, y1), (x2, y2))` dengan
    jarak Manhattan tepat 1; kedua ujung harus anggota `nodes`.
    `goals`: himpunan posisi berdiri yang dipasok pemanggil.
    `max_path_length`: batas panjang jalur (jumlah sisi), integer polos >= 0.

    Contoh::

        cari_rute_kardinal(
            nodes={(0, 0), (1, 0), (2, 0)},
            edges={((0, 0), (1, 0)), ((1, 0), (2, 0))},
            start=(0, 0),
            goals={(2, 0)},
            max_path_length=8,
        )
    """
    node_list, err = _ambil_terbatas(nodes, NODE_MAX, "nodes")
    if err is not None:
        return _invalid(err)
    node_set: set[tuple[int, int]] = set()
    for item in node_list:
        xy, err = _baca_xy(item, "simpul")
        if err is not None:
            return _invalid(err)
        node_set.add(xy)
    if not node_set:
        return _invalid("nodes kosong")
    xs = [x for x, _y in node_set]
    ys = [y for _x, y in node_set]
    span_x = max(xs) - min(xs) + 1
    span_y = max(ys) - min(ys) + 1
    if span_x > WINDOW_SPAN_MAX or span_y > WINDOW_SPAN_MAX:
        return _invalid(
            f"jendela {span_x}x{span_y} melebihi {WINDOW_SPAN_MAX}x{WINDOW_SPAN_MAX}"
        )

    edge_list, err = _ambil_terbatas(edges, EDGE_INPUT_MAX, "edges")
    if err is not None:
        return _invalid(err)
    keluar: dict[tuple[int, int], set[tuple[int, int]]] = {n: set() for n in node_set}
    for item in edge_list:
        pasangan, err = _baca_sisi(item)
        if err is not None:
            return _invalid(err)
        src, dst = pasangan
        if src not in node_set or dst not in node_set:
            return _invalid(f"ujung sisi {src}->{dst} di luar nodes")
        if abs(src[0] - dst[0]) + abs(src[1] - dst[1]) != 1:
            return _invalid(f"sisi {src}->{dst} bukan kardinal Manhattan 1")
        keluar[src].add(dst)
        if len(keluar[src]) > OUT_DEGREE_MAX:
            return _invalid(
                f"simpul {src} punya >{OUT_DEGREE_MAX} sisi keluar"
            )

    awal, err = _baca_xy(start, "start")
    if err is not None:
        return _invalid(err)
    if awal not in node_set:
        return _invalid(f"start {awal} tidak ada di nodes")

    goal_list, err = _ambil_terbatas(goals, NODE_MAX, "goals")
    if err is not None:
        return _invalid(err)
    tujuan: set[tuple[int, int]] = set()
    for item in goal_list:
        xy, err = _baca_xy(item, "tujuan")
        if err is not None:
            return _invalid(err)
        if xy not in node_set:
            return _invalid(f"tujuan {xy} tidak ada di nodes")
        tujuan.add(xy)

    if not _int_polos(max_path_length) or max_path_length < 0:
        return _invalid(
            f"max_path_length harus integer >= 0, dapat {max_path_length!r}"
        )
    batas = max_path_length

    if not tujuan:
        return HasilRute(
            status=EMPTY_GOALS,
            found=False,
            path=(),
            steps=(),
            chosen_goal=None,
            visited_count=0,
            reason="goals kosong",
        )

    if awal in tujuan:
        return HasilRute(
            status=FOUND,
            found=True,
            path=(awal,),
            steps=(),
            chosen_goal=awal,
            visited_count=1,
        )

    adj = {n: tuple(sorted(tujuan_n)) for n, tujuan_n in keluar.items()}
    dist = {awal: 0}
    parent: dict[tuple[int, int], tuple[int, int] | None] = {awal: None}
    antrian = deque([awal])
    while antrian:
        u = antrian.popleft()
        for v in adj[u]:
            if v in dist:
                continue
            dist[v] = dist[u] + 1
            parent[v] = u
            antrian.append(v)

    teraih = [g for g in tujuan if g in dist]
    if not teraih:
        return HasilRute(
            status=UNREACHABLE,
            found=False,
            path=(),
            steps=(),
            chosen_goal=None,
            visited_count=len(dist),
            reason="tidak ada tujuan yang terhubung di graf yang dipasok",
        )

    terpilih = min(teraih, key=lambda g: (dist[g], g[0], g[1]))
    jarak = dist[terpilih]
    if jarak > batas:
        return HasilRute(
            status=PATH_LIMIT_EXHAUSTED,
            found=False,
            path=(),
            steps=(),
            chosen_goal=terpilih,
            visited_count=len(dist),
            reason=(
                f"jalur terpendek {jarak} sisi melebihi max_path_length {batas}"
            ),
        )

    jejak: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = terpilih
    while cur is not None:
        jejak.append(cur)
        cur = parent[cur]
    jejak.reverse()
    langkah = tuple(
        _DELTA_KE_ARAH[(b[0] - a[0], b[1] - a[1])]
        for a, b in zip(jejak, jejak[1:])
    )
    return HasilRute(
        status=FOUND,
        found=True,
        path=tuple(jejak),
        steps=langkah,
        chosen_goal=terpilih,
        visited_count=len(dist),
    )


def _invalid(reason: str) -> HasilRute:
    return HasilRute(
        status=INVALID_INPUT,
        found=False,
        path=(),
        steps=(),
        chosen_goal=None,
        visited_count=0,
        reason=reason,
    )


def _int_polos(nilai: object) -> bool:
    return isinstance(nilai, int) and not isinstance(nilai, bool)


def _ambil_terbatas(
    nilai: object, batas: int, nama: str
) -> tuple[list[object], str | None]:
    if nilai is None or isinstance(nilai, (str, bytes, bytearray)):
        return [], f"{nama} harus koleksi terhingga, dapat {type(nilai).__name__}"
    try:
        iterator = iter(nilai)  # type: ignore[arg-type]
    except TypeError:
        return [], f"{nama} harus iterable, dapat {type(nilai).__name__}"
    out: list[object] = []
    for item in iterator:
        if len(out) >= batas:
            return [], f"{nama} melebihi batas {batas}"
        out.append(item)
    return out, None


def _baca_xy(nilai: object, nama: str) -> tuple[tuple[int, int], str | None]:
    if not isinstance(nilai, (tuple, list)) or len(nilai) != 2:
        return (0, 0), f"{nama} harus (x, y), dapat {nilai!r}"
    x, y = nilai[0], nilai[1]
    for label, angka in (("x", x), ("y", y)):
        if isinstance(angka, bool) or not isinstance(angka, int):
            return (0, 0), f"{nama}.{label} harus integer polos, dapat {angka!r}"
        if angka < 0:
            return (0, 0), f"{nama}.{label} harus >= 0, dapat {angka}"
    return (x, y), None


def _baca_sisi(
    nilai: object,
) -> tuple[tuple[tuple[int, int], tuple[int, int]], str | None]:
    if not isinstance(nilai, (tuple, list)) or len(nilai) != 2:
        return ((0, 0), (0, 0)), f"sisi harus ((x1, y1), (x2, y2)), dapat {nilai!r}"
    src, err = _baca_xy(nilai[0], "sisi.asal")
    if err is not None:
        return ((0, 0), (0, 0)), err
    dst, err = _baca_xy(nilai[1], "sisi.tujuan")
    if err is not None:
        return ((0, 0), (0, 0)), err
    return (src, dst), None
