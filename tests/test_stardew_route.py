"""Kernel rute kardinal murni: graf eksplisit, tanpa gerakan ke game.

Tes ini membuktikan perilaku, bukan teks sumber. Oracle independen di graf
kecil memeriksa jalur terpendek dan tidak ada sisi yang tidak dipasok.
"""
from __future__ import annotations

import ast
import copy
import heapq
import itertools
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arti_stardew_route import (  # noqa: E402
    EMPTY_GOALS,
    FOUND,
    INVALID_INPUT,
    NODE_MAX,
    PATH_LIMIT_EXHAUSTED,
    UNREACHABLE,
    WINDOW_SPAN_MAX,
    cari_rute_kardinal,
)

MODUL = ROOT / "arti_stardew_route.py"


def _hasil(nodes, edges, start, goals, max_path_length=16):
    return cari_rute_kardinal(
        nodes=nodes,
        edges=edges,
        start=start,
        goals=goals,
        max_path_length=max_path_length,
    )


def _oracle_jarak(nodes, edges, start, goals):
    """BFS independen: urutan tetangga terbalik, heapq, tanpa memakai kernel."""
    node_set = set(nodes)
    adj = {n: [] for n in node_set}
    for src, dst in edges:
        adj[src].append(dst)
    for n in adj:
        adj[n] = sorted(adj[n], reverse=True)
    dist = {start: 0}
    heap = [(0, start[1], start[0], start)]
    seen = set()
    while heap:
        d, _y, _x, u = heapq.heappop(heap)
        if u in seen:
            continue
        seen.add(u)
        for v in adj[u]:
            nd = d + 1
            if nd < dist.get(v, 10**9):
                dist[v] = nd
                heapq.heappush(heap, (nd, v[1], v[0], v))
    kandidat = [g for g in goals if g in dist]
    if not kandidat:
        return None
    return min(dist[g] for g in kandidat)


def _sisi_di_jalur(path, edges):
    boleh = {(tuple(a), tuple(b)) for a, b in edges}
    return all((path[i], path[i + 1]) in boleh for i in range(len(path) - 1))


def test_lurus_pendek_ke_kanan():
    nodes = {(0, 0), (1, 0), (2, 0)}
    edges = {((0, 0), (1, 0)), ((1, 0), (2, 0))}
    hasil = _hasil(nodes, edges, (0, 0), {(2, 0)})
    assert hasil.status == FOUND
    assert hasil.found is True
    assert hasil.path == ((0, 0), (1, 0), (2, 0))
    assert hasil.steps == ("right", "right")
    assert hasil.chosen_goal == (2, 0)
    assert hasil.visited_count <= NODE_MAX


def test_lurus_atas_bawah_kiri():
    nodes = {(2, 2), (2, 1), (2, 3), (1, 2)}
    edges = (
        ((2, 2), (2, 1)),
        ((2, 2), (2, 3)),
        ((2, 2), (1, 2)),
    )
    atas = _hasil(nodes, edges, (2, 2), {(2, 1)})
    assert atas.path == ((2, 2), (2, 1))
    assert atas.steps == ("up",)
    bawah = _hasil(nodes, edges, (2, 2), {(2, 3)})
    assert bawah.steps == ("down",)
    kiri = _hasil(nodes, edges, (2, 2), {(1, 2)})
    assert kiri.steps == ("left",)


def test_detour_karena_sisi_tengah_tidak_dipasok():
    # Lurus (0,0)->(1,0)->(2,0) terputus; harus naik lalu kanan lalu turun.
    nodes = {(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)}
    edges = {
        ((0, 0), (0, 1)),
        ((0, 1), (1, 1)),
        ((1, 1), (2, 1)),
        ((2, 1), (2, 0)),
        ((1, 0), (2, 0)),  # sisa sisi yang tidak menolong dari start
    }
    hasil = _hasil(nodes, edges, (0, 0), {(2, 0)})
    assert hasil.status == FOUND
    assert hasil.path == ((0, 0), (0, 1), (1, 1), (2, 1), (2, 0))
    assert len(hasil.steps) == 4
    assert _sisi_di_jalur(hasil.path, edges)
    assert _oracle_jarak(nodes, edges, (0, 0), {(2, 0)}) == 4


def test_tujuan_tidak_terhubung():
    nodes = {(0, 0), (1, 0), (3, 0)}
    edges = {((0, 0), (1, 0))}
    hasil = _hasil(nodes, edges, (0, 0), {(3, 0)})
    assert hasil.status == UNREACHABLE
    assert hasil.found is False
    assert hasil.path == ()
    assert hasil.steps == ()
    assert hasil.chosen_goal is None
    assert "Farm" not in (hasil.reason or "")


def test_start_sudah_di_tujuan_nol_langkah():
    nodes = {(4, 5), (4, 6)}
    edges = {((4, 5), (4, 6))}
    hasil = _hasil(nodes, edges, (4, 5), {(4, 5), (4, 6)}, max_path_length=0)
    assert hasil.status == FOUND
    assert hasil.path == ((4, 5),)
    assert hasil.steps == ()
    assert hasil.chosen_goal == (4, 5)
    assert hasil.visited_count == 1


def test_goals_kosong_bukan_unreachable():
    nodes = {(0, 0), (1, 0)}
    edges = {((0, 0), (1, 0))}
    hasil = _hasil(nodes, edges, (0, 0), set())
    assert hasil.status == EMPTY_GOALS
    assert hasil.found is False
    assert hasil.visited_count == 0


def test_tujuan_berjarak_sama_pilih_xy_terkecil():
    nodes = {(1, 1), (0, 1), (1, 0), (2, 1)}
    edges = {
        ((1, 1), (0, 1)),
        ((1, 1), (1, 0)),
        ((1, 1), (2, 1)),
    }
    goals = {(0, 1), (1, 0), (2, 1)}
    hasil = _hasil(nodes, edges, (1, 1), goals)
    assert hasil.status == FOUND
    assert hasil.chosen_goal == (0, 1)
    assert hasil.path == ((1, 1), (0, 1))


def test_deterministik_terhadap_permutasi_masukan():
    nodes = [(2, 2), (2, 1), (3, 2), (1, 2)]
    edges = [
        ((2, 2), (2, 1)),
        ((2, 2), (3, 2)),
        ((2, 2), (1, 2)),
    ]
    goals = [(2, 1), (3, 2), (1, 2)]
    acuan = _hasil(nodes, edges, (2, 2), goals)
    assert acuan.chosen_goal == (1, 2)
    for n_perm in itertools.permutations(nodes):
        for e_perm in itertools.permutations(edges):
            for g_perm in itertools.permutations(goals):
                hasil = _hasil(n_perm, e_perm, (2, 2), g_perm)
                assert hasil.status == acuan.status
                assert hasil.path == acuan.path
                assert hasil.steps == acuan.steps
                assert hasil.chosen_goal == acuan.chosen_goal


def test_sisi_berarah_tidak_dibalik():
    nodes = {(0, 0), (1, 0)}
    edges = {((0, 0), (1, 0))}
    maju = _hasil(nodes, edges, (0, 0), {(1, 0)})
    assert maju.status == FOUND
    balik = _hasil(nodes, edges, (1, 0), {(0, 0)})
    assert balik.status == UNREACHABLE


def test_ubin_bersebelahan_tanpa_sisi_bukan_jalur():
    nodes = {(5, 5), (6, 5), (5, 6)}
    edges = ()
    hasil = _hasil(nodes, edges, (5, 5), {(6, 5), (5, 6)})
    assert hasil.status == UNREACHABLE
    assert hasil.path == ()


def test_siklus_tidak_menggulung():
    nodes = {(0, 0), (1, 0), (1, 1), (0, 1)}
    edges = {
        ((0, 0), (1, 0)),
        ((1, 0), (1, 1)),
        ((1, 1), (0, 1)),
        ((0, 1), (0, 0)),
    }
    hasil = _hasil(nodes, edges, (0, 0), {(0, 1)})
    assert hasil.status == FOUND
    assert hasil.path == ((0, 0), (1, 0), (1, 1), (0, 1))
    assert hasil.steps == ("right", "down", "left")
    assert _sisi_di_jalur(hasil.path, edges)
    assert hasil.visited_count == 4


def test_bool_ditolak():
    # True == 1 di Python; jangan taruh bool di set bareng integer.
    assert (
        _hasil([(True, 0)], (), (0, 0), [(0, 0)]).status == INVALID_INPUT
    )
    assert (
        _hasil(
            [(0, 0), (0, 1)],
            [((False, 0), (0, 1))],
            (0, 0),
            [(0, 1)],
        ).status
        == INVALID_INPUT
    )
    hasil = _hasil(
        [(0, 0), (1, 0)],
        [((0, 0), (1, 0))],
        (0, 0),
        [(1, 0)],
        max_path_length=True,
    )
    assert hasil.status == INVALID_INPUT


def test_diagonal_dan_teleport_ditolak():
    nodes = {(0, 0), (1, 1), (2, 0)}
    diagonal = _hasil(nodes, {((0, 0), (1, 1))}, (0, 0), {(1, 1)})
    assert diagonal.status == INVALID_INPUT
    teleport = _hasil(
        {(0, 0), (2, 0)}, {((0, 0), (2, 0))}, (0, 0), {(2, 0)}
    )
    assert teleport.status == INVALID_INPUT


def test_masukan_cacat_dan_bukan_koleksi():
    assert _hasil(None, (), (0, 0), {(0, 0)}).status == INVALID_INPUT
    assert _hasil("nodes", (), (0, 0), {(0, 0)}).status == INVALID_INPUT
    assert _hasil({(0, 0)}, 123, (0, 0), {(0, 0)}).status == INVALID_INPUT
    assert _hasil({(0, 0)}, (), (0, 0, 1), {(0, 0)}).status == INVALID_INPUT
    assert _hasil({(0, 0)}, {((0, 0),)}, (0, 0), {(0, 0)}).status == INVALID_INPUT


def test_jendela_dan_jumlah_simpul_berlebih():
    garis = {(i, 0) for i in range(WINDOW_SPAN_MAX + 1)}
    hasil = _hasil(garis, (), (0, 0), {(0, 0)})
    assert hasil.status == INVALID_INPUT
    assert "jendela" in (hasil.reason or "")

    banyak = [(i % 9, i // 9) for i in range(NODE_MAX + 1)]
    hasil = _hasil(banyak, (), banyak[0], {banyak[0]})
    assert hasil.status == INVALID_INPUT
    assert "melebihi" in (hasil.reason or "")


def test_iterasi_sisi_berlebih_ditolak():
    edges = [((1, 1), (1, 0))] * (NODE_MAX * 4 + 1)
    hasil = _hasil({(1, 1), (1, 0)}, edges, (1, 1), {(1, 0)})
    assert hasil.status == INVALID_INPUT
    assert "melebihi" in (hasil.reason or "")


def test_start_bukan_anggota_nodes():
    hasil = _hasil({(0, 0)}, (), (1, 0), {(0, 0)})
    assert hasil.status == INVALID_INPUT


def test_tujuan_di_luar_nodes_dan_float():
    luar = _hasil({(0, 0), (1, 0)}, {((0, 0), (1, 0))}, (0, 0), {(2, 0)})
    assert luar.status == INVALID_INPUT
    pecahan = _hasil({(0, 0), (1.5, 0)}, (), (0, 0), {(0, 0)})
    assert pecahan.status == INVALID_INPUT
    start_di_tujuan = _hasil(
        [(0, 0)],
        (),
        (0, 0),
        [(0, 0)],
        max_path_length=True,
    )
    assert start_di_tujuan.status == INVALID_INPUT


def test_ujung_sisi_di_luar_nodes():
    hasil = _hasil({(0, 0)}, {((0, 0), (1, 0))}, (0, 0), {(0, 0)})
    assert hasil.status == INVALID_INPUT
    assert "luar" in (hasil.reason or "")


def test_koordinat_batas_nol_dan_negatif():
    nodes = {(0, 0), (0, 1)}
    edges = {((0, 0), (0, 1))}
    ok = _hasil(nodes, edges, (0, 0), {(0, 1)})
    assert ok.status == FOUND
    neg = _hasil({(-1, 0), (0, 0)}, {((-1, 0), (0, 0))}, (0, 0), {(0, 0)})
    assert neg.status == INVALID_INPUT


def test_batas_panjang_jalur():
    nodes = {(0, 0), (1, 0), (2, 0), (3, 0)}
    edges = {((0, 0), (1, 0)), ((1, 0), (2, 0)), ((2, 0), (3, 0))}
    muat = _hasil(nodes, edges, (0, 0), {(3, 0)}, max_path_length=3)
    assert muat.status == FOUND
    assert len(muat.steps) == 3
    habis = _hasil(nodes, edges, (0, 0), {(3, 0)}, max_path_length=2)
    assert habis.status == PATH_LIMIT_EXHAUSTED
    assert habis.found is False
    assert habis.path == ()
    assert habis.chosen_goal == (3, 0)
    putus = _hasil(nodes, {((0, 0), (1, 0))}, (0, 0), {(3, 0)}, max_path_length=2)
    assert putus.status == UNREACHABLE


def test_generator_tak_terbatas_ditolak_tanpa_menggantung():
    def tak_hingga_simpul():
        n = 0
        while True:
            yield (n, 0)
            n += 1

    def tak_hingga_sisi():
        while True:
            yield ((0, 0), (1, 0))

    def tak_hingga_tujuan():
        n = 0
        while True:
            yield (n, 0)
            n += 1

    hasil = _hasil(tak_hingga_simpul(), (), (0, 0), {(0, 0)})
    assert hasil.status == INVALID_INPUT
    assert "melebihi" in (hasil.reason or "")

    hasil = _hasil({(0, 0), (1, 0)}, tak_hingga_sisi(), (0, 0), {(1, 0)})
    assert hasil.status == INVALID_INPUT
    assert "melebihi" in (hasil.reason or "")

    hasil = _hasil({(0, 0)}, (), (0, 0), tak_hingga_tujuan())
    assert hasil.status == INVALID_INPUT
    assert "melebihi" in (hasil.reason or "")


def test_koleksi_masukan_tidak_dimutasi():
    nodes = [(0, 0), (1, 0)]
    edges = [((0, 0), (1, 0))]
    goals = [(1, 0)]
    n2, e2, g2 = copy.deepcopy(nodes), copy.deepcopy(edges), copy.deepcopy(goals)
    _hasil(nodes, edges, (0, 0), goals)
    assert nodes == n2
    assert edges == e2
    assert goals == g2


def test_data_kerja_dan_hasil_tetap_terbatas():
    nodes = {(x, y) for x in range(9) for y in range(9)}
    edges = set()
    for x in range(9):
        for y in range(9):
            if x + 1 < 9:
                edges.add(((x, y), (x + 1, y)))
            if y + 1 < 9:
                edges.add(((x, y), (x, y + 1)))
    hasil = _hasil(nodes, edges, (0, 0), {(8, 8)}, max_path_length=80)
    assert hasil.status == FOUND
    assert len(hasil.path) <= NODE_MAX
    assert hasil.visited_count <= NODE_MAX
    assert len(hasil.steps) == 16
    assert "world_press" not in hasil.__dataclass_fields__
    assert "safe_to_walk" not in hasil.__dataclass_fields__


def test_oracle_graf_2x2_semua_sisi_berarah():
    nodes = ((0, 0), (1, 0), (0, 1), (1, 1))
    kandidat_sisi = []
    node_set = set(nodes)
    for x, y in nodes:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            dst = (x + dx, y + dy)
            if dst in node_set:
                kandidat_sisi.append(((x, y), dst))
    assert len(kandidat_sisi) == 8
    subset_tujuan = []
    for r in range(1, 5):
        subset_tujuan.extend(itertools.combinations(nodes, r))

    for mask in range(1 << 8):
        edges = tuple(
            kandidat_sisi[i] for i in range(8) if mask & (1 << i)
        )
        for start in nodes:
            for goals in subset_tujuan:
                hasil = _hasil(nodes, edges, start, goals, max_path_length=8)
                if start in goals:
                    assert hasil.status == FOUND
                    assert hasil.path == (start,)
                    assert hasil.steps == ()
                    continue
                jarak = _oracle_jarak(nodes, edges, start, goals)
                if jarak is None:
                    assert hasil.status == UNREACHABLE
                    assert hasil.path == ()
                    continue
                assert hasil.status == FOUND
                assert len(hasil.path) - 1 == jarak
                assert hasil.chosen_goal in goals
                assert hasil.path[0] == start
                assert hasil.path[-1] == hasil.chosen_goal
                assert _sisi_di_jalur(hasil.path, edges)
                # Tidak ada loncatan: setiap langkah Manhattan 1.
                for a, b in zip(hasil.path, hasil.path[1:]):
                    assert abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def test_oracle_batas_panjang_di_rantai():
    nodes = tuple((i, 0) for i in range(5))
    edges = tuple(((i, 0), (i + 1, 0)) for i in range(4))
    for batas in range(0, 6):
        hasil = _hasil(nodes, edges, (0, 0), {(4, 0)}, max_path_length=batas)
        jarak = _oracle_jarak(nodes, edges, (0, 0), {(4, 0)})
        assert jarak == 4
        if batas >= 4:
            assert hasil.status == FOUND
            assert len(hasil.steps) == 4
        else:
            assert hasil.status == PATH_LIMIT_EXHAUSTED


def test_modul_tidak_mengimpor_inbox_atau_farm():
    pohon = ast.parse(MODUL.read_text(encoding="utf-8"))
    nama = set()
    for node in ast.walk(pohon):
        if isinstance(node, ast.Import):
            nama.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            nama.add(node.module.split(".")[0])
    terlarang = {
        "arti_stardew_command",
        "arti_stardew_farm_v0",
        "arti_stardew_farm_v1",
        "arti_stardew",
        "arti_stardew_observation",
        "arti_stardew_ui",
        "arti_stardew_tier3",
    }
    assert nama.isdisjoint(terlarang)


def test_tidak_ada_durasi_world_press_di_hasil():
    hasil = _hasil(
        {(0, 0), (1, 0)},
        {((0, 0), (1, 0))},
        (0, 0),
        {(1, 0)},
    )
    assert hasil.steps == ("right",)
    assert all(isinstance(s, str) for s in hasil.steps)
    assert not any(isinstance(s, int) for s in hasil.steps)
    with pytest.raises(AttributeError):
        getattr(hasil, "ticks")
    with pytest.raises(AttributeError):
        getattr(hasil, "world_press")
