"""Refleks instan — Arti bereaksi SEBELUM sempat mikir.

MASALAH YANG DIPECAHKAN (terukur di live 2026-08-05): dari dipukul sampai ada
bunyi apa pun keluar butuh MINIMAL ~6 detik (LLM 5,6-8,9 dtk + sintesis
0,6-1,3 dtk), dan lebih lama lagi kalau antrean sibuk. Reaksi yang datang 8
detik sesudah kejadian otomatis terdengar seperti LAPORAN, bukan reaksi —
sebanyak apa pun prompt-nya diperbaiki. Keluhan Bohan: "terlalu matter of
fact".

CARANYA: satu lapisan refleks di depan pipeline. Kalimat pendek ("Aduh!",
"Eh curang!") disintesis SEKALI ke berkas WAV memakai suara Arti sendiri,
lalu diputar langsung dari cache saat event datang — nol LLM, nol sintesis,
di bawah 100 ms. Composer tetap mulai berpikir di detik yang sama, jadi
urutannya jadi: "Aduh!" (instan) -> beberapa detik -> kalimat lengkapnya.

Modul ini MURNI (pemilihan kalimat, pemetaan event, rate limit). Pemutaran
audio & sintesis ada di bridge — lihat `_play_reflex`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Pustaka kalimat — 1-5 kata, refleks, bukan komentar.
# ---------------------------------------------------------------------------
# Aturan menulis: ini BUKAN tempat opini/lelucon panjang. Yang keluar duluan
# harus terdengar seperti orang kaget beneran — pendek, spontan, dan boleh
# tidak selesai. Kalimat pintarnya menyusul dari LLM beberapa detik kemudian.

REFLEX_LINES: dict[str, tuple[str, ...]] = {
    # Kena pukul / kaget — kelompok terbesar, paling sering kepakai.
    "kaget": (
        "Ah!",
        "Aduh!",
        "Eh?!",
        "Ih sakit!",
        "Woy!",
        "Astaga!",
        "Hah?!",
        "Eh eh eh!",
        "Aw!",
        "Ya ampun!",
        "Apaan tuh?!",
        "Eh bentar!",
        "Sakit tau!",
        "Waduh!",
        "Hei!",
        "Kok gitu sih?!",
        "Ish!",
        "Eh curang!",
        "Woy jangan!",
        "Kok aku sih?!",
    ),
    # Nyeri yang menyusul — nada lebih rendah, bukan kaget lagi.
    "sakit": (
        "Aduuuh...",
        "Sakit banget...",
        "Perih ih...",
        "Aw aw aw...",
        "Pelan-pelan dong!",
        "Nyeri nih...",
        "Uh, lumayan sakit.",
        "Bentar, sakit.",
    ),
    # HP kritis — panik, bukan cuma sakit.
    "panik": (
        "Gawat!",
        "Waduh gawat!",
        "Bahaya nih!",
        "Tolong dong!",
        "Aku sekarat!",
        "Nyawa tipis!",
        "Mati aku mati aku!",
        "Jangan sekarang!",
    ),
    # Musuh mepet — refleks menghindar, sebelum kena.
    "bahaya": (
        "Eh eh eh eh!",
        "Mundur mundur!",
        "Awas awas!",
        "Jangan deket!",
        "Kabur kabur!",
        "Ya ampun deket!",
        "Nggak nggak nggak!",
        "Ih ngapain kamu!",
    ),
    # Mati — satu-satunya yang boleh agak panjang bunyinya.
    "mati": (
        "Yaaah...",
        "Aku mati...",
        "Yah, mati deh.",
        "Aaaa!",
        "Duh, kalah.",
        "Yah gitu doang?",
    ),
}

# Semua kalimat, untuk pembuat cache.
ALL_LINES: tuple[str, ...] = tuple(
    line for group in REFLEX_LINES.values() for line in group
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def cache_name(line: str) -> str:
    """Nama berkas cache yang stabil & aman untuk satu kalimat.

    Pakai slug + hash: slug biar bisa dibaca manusia saat mengaudit folder
    cache, hash biar dua kalimat yang beda tanda baca ("Ah!" vs "Ah?") tidak
    pernah bertabrakan jadi satu berkas.
    """
    slug = _SLUG_RE.sub("-", line.strip().lower()).strip("-")[:32] or "x"
    digest = hashlib.sha1(line.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}.wav"


# ---------------------------------------------------------------------------
# Event mana yang pantas dapat refleks
# ---------------------------------------------------------------------------
# Sengaja TIDAK semuanya. Refleks yang terlalu sering = berisik, dan yang
# berisik akan dimatikan operator dalam 10 menit.

def reflex_for_event(ev: dict) -> str | None:
    """Kelompok kalimat untuk event ini, atau None kalau tidak layak."""
    kind = ev.get("ev")
    if kind == "death":
        return "mati"
    if kind == "low_health":
        return "panik" if int(ev.get("health") or 0) > 0 else None
    if kind == "hurt":
        # Luka tanpa sumber (jatuh / kesenggol medan saat pathfinding) tetap
        # dapat refleks — orang jatuh ya bilang "aduh" — tapi nada nyeri,
        # bukan kaget dituduh diserang. Lihat fix [date removed].
        return "kaget" if str(ev.get("source", "unknown")) != "unknown" else "sakit"
    if kind == "hostile_near":
        return "bahaya"
    return None


@dataclass
class ReflexLimiter:
    """Rate limit + anti-ulang. Satu instance per sesi Minecraft."""

    last_ts: float = 0.0
    recent: list[str] = field(default_factory=list)
    max_recent: int = 8
    # Jam TERPISAH per kategori prioritas — lihat _PRIORITAS_GAP.
    last_prioritas_ts: dict = field(default_factory=dict)
    # Kapan tiap sumber serangan terakhir bikin dia bereaksi (nama -> ts).
    # Dipakai memutuskan apakah ancaman ini kabar BARU atau itu-itu lagi.
    sumber_ts: dict = field(default_factory=dict)
    # Kategori refleks terakhir yang bunyi — dipakai membandingkan prioritas.
    last_category: str = ""


# Kategori yang BOLEH menembus jeda. Audit [date removed] membuktikan: bot mengirim
# hurt -> low_health -> death dalam milidetik yang SAMA, dan jeda tunggal 3 dtk
# membuat yang pertama (hurt/"Ish!") menang — jadi 14 dari 50 kalimat (panik +
# mati) tidak pernah kepakai, dan momen paling dramatis justru dapat celetukan
# receh. Kematian selalu didahului penurunan HP, jadi tanpa pengecualian ini
# kelompok "mati" mustahil bunyi.
_PRIORITAS = frozenset({"mati", "panik"})

# Kategori yang boleh MEMOTONG audio yang sedang berbunyi. Aturan operator
# [date removed]: "jangan kepotong, biarin selesai dulu audionya baru pasang baru,
# KECUALI ada event yang baru kayak mati, jadi kayak eskalasi". Cuma kematian
# — itu satu-satunya kejadian yang membuat kalimat sebelumnya jadi tidak
# relevan lagi. Sisanya menunggu giliran.
ESKALASI = frozenset({"mati"})


def boleh_memotong(category: str) -> bool:
    """Kategori yang selalu boleh menyela, apa pun keadaannya."""
    return category in ESKALASI


# Berapa lama satu sumber serangan dianggap "masih yang tadi". Dipukuli zombie
# yang sama berkali-kali BUKAN kabar baru; panah skeleton yang tiba-tiba
# nancap, atau spider yang baru menyerang, ITU kabar baru.
SUMBER_BARU_SEC = 25.0


def sumber_serangan(ev: dict | None) -> str:
    """Siapa/apa yang menyerang, "" kalau tidak jelas.

    Sumber yang tidak dikenal TIDAK PERNAH dihitung baru — kalau tidak, jatuh
    dari ketinggian dan kerusakan tanpa pelaku akan menyela terus-menerus.
    """
    if not isinstance(ev, dict):
        return ""
    kind = ev.get("ev")
    if kind == "hurt":
        # Pelakunya dulu (zombie/skeleton/pemain); kalau tidak ada pelaku,
        # TIPE lukanya yang jadi sumber — jatuh, lava, potion, panah nyasar
        # itu kejadian yang berbeda-beda buat Arti, bukan "tidak diketahui".
        nama = str(ev.get("source") or "").strip()
        if nama.lower() in ("", "unknown", "none", "null"):
            nama = str(ev.get("damage_type") or "").strip()
    elif kind == "hostile_near":
        nama = str(ev.get("kind") or "").strip()
    else:
        return ""
    return "" if nama.lower() in ("", "unknown", "none", "null") else nama.lower()


# Urutan siapa yang lebih penting kalau beberapa kejadian datang berdekatan
# (permintaan operator [date removed]: "jadi ada priority mana yang duluan").
# Makin kecil angkanya, makin didahulukan.
PRIORITAS_URUT = ("mati", "panik", "bahaya", "sakit", "kaget")
# Yang lebih penting boleh mendahului, tapi tidak boleh menempel — dua bunyi
# berjarak 0,2 dtk terdengar seperti satu suara pecah, bukan dua reaksi.
_PRIORITAS_LANTAI_SEC = 1.0


def prioritas(category: str) -> int:
    try:
        return PRIORITAS_URUT.index(category)
    except ValueError:
        return len(PRIORITAS_URUT)


def sumber_baru(ev: dict | None, limiter, now: float,
                jeda: float = SUMBER_BARU_SEC) -> bool:
    """Penyerang ini kabar BARU? Sekaligus mencatatnya sebagai sudah dikenal.

    Aturan Bohan 2026-08-07: "kalau lagi dipukulin zombie, trus ada arrow
    skeleton nancap, itu boleh dicela; kalau ada spider baru nyerang, itu baru
    boleh cela". Jadi yang menyela adalah PERUBAHAN ancaman, bukan pengulangan.
    """
    nama = sumber_serangan(ev)
    if not nama:
        return False
    terakhir = limiter.sumber_ts.get(nama, 0.0)
    limiter.sumber_ts[nama] = now
    # 0.0 = BELUM PERNAH (pelajaran bug _cooled), jadi penyerang pertama pun
    # dihitung baru.
    return terakhir <= 0.0 or (now - terakhir) >= jeda
# Menembus jeda umum BUKAN berarti tanpa rem. Audit verifikasi [date removed]:
# bot sungguhan memancarkan 3 `low_health` dalam ~1,5 detik (HP 5 -> 2 -> 0),
# dan tanpa jam sendiri semuanya bunyi — yang kedua memotong yang pertama di
# stream audio global. Hasilnya "Gawa—" "Nyawa tipis!". Jadi tiap kategori
# prioritas punya jam sendiri yang lebih longgar.
_PRIORITAS_GAP = {"mati": 6.0, "panik": 8.0}


def should_react(
    limiter: ReflexLimiter,
    now: float,
    config: dict | None = None,
    category: str | None = None,
) -> bool:
    """Boleh bunyi sekarang? (jeda minimum antar refleks)

    Kategori prioritas menembus jeda UMUM — sebuah "Aduh!" 1 ms sebelumnya
    tidak boleh menelan teriakan kematian — tapi tetap tunduk pada jam
    kategorinya sendiri supaya tidak jadi rentetan.
    """
    if category in _PRIORITAS:
        gap = _PRIORITAS_GAP.get(category, 6.0)
        last = limiter.last_prioritas_ts.get(category, 0.0)
        return last <= 0.0 or (now - last) >= gap
    gap = float((config or {}).get("reflex_min_gap_sec", 3.0))
    if limiter.last_ts <= 0.0 or (now - limiter.last_ts) >= gap:
        return True
    # Masih di dalam jeda — tapi yang LEBIH PENTING boleh mendahului. Tanpa
    # ini "Ish!" (kaget) menelan "Mundur mundur!" (bahaya) hanya karena datang
    # 1 detik lebih dulu, padahal creeper yang mendekat lebih layak disuarakan.
    # Tetap ada lantai jeda supaya tidak menumpuk jadi rentetan.
    if (category is not None
            and prioritas(category) < prioritas(limiter.last_category)
            and (now - limiter.last_ts) >= _PRIORITAS_LANTAI_SEC):
        return True
    return False


def pick_line(
    category: str, limiter: ReflexLimiter, rng=None
) -> str | None:
    """Satu kalimat dari kelompok, hindari yang baru saja dipakai.

    `rng` = callable tanpa argumen -> float [0,1), diinjeksi di test.
    """
    pool = REFLEX_LINES.get(category)
    if not pool:
        return None
    if rng is None:
        import random  # noqa: PLC0415

        rng = random.random
    fresh = [ln for ln in pool if ln not in limiter.recent] or list(pool)
    line = fresh[min(int(rng() * len(fresh)), len(fresh) - 1)]
    limiter.recent.append(line)
    if len(limiter.recent) > limiter.max_recent:
        limiter.recent.pop(0)
    return line


def mark_reacted(limiter: ReflexLimiter, now: float, category: str | None = None) -> None:
    limiter.last_ts = now
    # Dicatat supaya kejadian berikutnya bisa membandingkan kepentingannya.
    limiter.last_category = category or ""
    if category in _PRIORITAS:
        limiter.last_prioritas_ts[category] = now


# Mood yang menyusul sesudah "aware" (spek operator: "aware aja trus lanjut ke
# lampu + ekspresi sedih/marah/bingung, kayak diselipin"). Refleks jadi giliran
# bicara MINI: mata melebar -> lampu+mood selama bunyinya -> balik default.
#
# WAJIB memakai emosi yang benar-benar ada di arti_expression_runtime.EMOTION_MAP
# (senang/sedih/marah/bingung/neutral). Nama di luar itu no-op TANPA peringatan —
# pelajaran dari "kaget" yang hampir kupakai.
REFLEX_MOOD: dict[str, str] = {
    "kaget": "bingung",   # kaget = paling dekat ke bingung di set yang ada
    "sakit": "sedih",
    "panik": "bingung",
    "bahaya": "bingung",
    "mati": "sedih",
}
# Dipukul PEMAIN itu beda rasanya dari dipukul mob — bukan kaget, tapi kesal.
# Bisa dibedakan sejak penyerang diambil dari paket damage_event server.
REFLEX_MOOD_PLAYER_HIT = "marah"


def mood_for(category: str, ev: dict | None = None) -> str:
    """Emosi overlay untuk refleks ini."""
    e = ev or {}
    # `killer_kind` untuk event death, `source_kind` untuk hurt — dua-duanya
    # dipancarkan bot tapi `killer_kind` dulu tidak pernah dibaca, jadi
    # dibunuh operator dan dibunuh creeper sama-sama "sedih".
    pelaku = e.get("source_kind") or e.get("killer_kind")
    if pelaku == "player" and category in ("kaget", "mati"):
        return REFLEX_MOOD_PLAYER_HIT
    return REFLEX_MOOD.get(category, "neutral")


# State VTS yang dipicu barengan refleks: "aware" — ArtiAware.exp3.json, yang
# kerjanya melebarkan kelopak mata jadi agak membelalak (operator [date removed]:
# "dulu dipake pas dia mikir, kayak sadar ada yang manggil"). Pas betul untuk
# refleks: wajahnya "!" duluan, suara menyusul, kalimat pintarnya belakangan.
#
# JANGAN pakai apply_speaking() di sini, dua alasan yang dua-duanya gagal DIAM:
#   1. dia menyalakan state "bicara" (lampu/mulut) padahal refleks BUKAN giliran
#      bicara — nyangkut, dan bentrok dengan turn TTS yang menyusul;
#   2. emosi seperti "kaget" TIDAK ADA di EMOTION_MAP (isinya cuma senang/sedih/
#      marah/bingung/neutral), jadi overlay-nya no-op tanpa satu pun peringatan.
# Persis pola bug yang pernah menyembunyikan ArtiSenyum hilang berbulan-bulan.
REFLEX_VTS_STATE = "aware"


def note_for_llm(line: str) -> str:
    """Baris konteks supaya LLM tahu dia SUDAH sempat bersuara.

    Tanpa ini dia bakal mengulang "aduh" lagi di depan kalimat panjangnya —
    kedengaran seperti orang yang kaget dua kali untuk satu kejadian.
    """
    return (
        f'(Kamu sudah refleks teriak "{line}" barusan — JANGAN ulangi bunyi '
        "kagetnya, langsung lanjut ke reaksi/komentarmu.)"
    )
