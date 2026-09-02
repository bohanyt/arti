"""Vault learning quality gate + model output sanitization."""
from __future__ import annotations

import re
import time
from pathlib import Path

_THINK_TAGS = (
    "think",
    "redacted_reasoning",
    "redacted_thinking",
)
_THINKING_RE = re.compile(
    "(?:" + "|".join(rf"<{t}>.*?</{t}>" for t in _THINK_TAGS) + ")",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_RE = re.compile(r"/no_think", re.IGNORECASE)

_SKIP_SUBSTRINGS = (
    "tidak ditemukan",
    "jawaban streamer tadi",
    "live stream baru saja dimulai",
    "live stream baru dimulai pada",
    "live stream dimulai pada",
    "live stream dimulai sekitar",
    "live stream baru dimulai sekitar",
    "vtuber baru saja memulai live stream",
    "arti baru saja memulai live stream",
    "arti baru saja mulai streaming",
    "arti baru saja mulai live",
    "baru saja memulai live stream",
    "baru mulai live stream",
    "baru mulai streaming",
    "memulai live stream",
    "arti berulang kali memastikan stream sudah menyala",
    "streamer berulang kali mengucapkan terima kasih",
    "tidak ada catatan baru",
    "stream fact: none",
    "penonton baru datang dan belum punya konteks waktu",
)

_MIN_LEARNING_LEN = 12
_MAX_LEARNING_BULLETS = 60


def sanitize_model_text(text: str) -> str:
    """Strip thinking blocks and /no_think residue from LLM output."""
    if not text:
        return ""
    out = _THINKING_RE.sub("", text)
    out = _NOISE_RE.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _normalize_fact(fact: str) -> str:
    t = fact.strip().lower()
    t = re.sub(r"^stream fact:\s*", "", t)
    t = re.sub(r"^reflection:\s*", "", t)
    t = re.sub(r"\s+", " ", t)
    return t


# Kata fungsi Bahasa Indonesia — dibuang sebelum membandingkan supaya "Arti debut
# co-host PADA [date removed]" dan "debut co-host JATUH pada [date removed]" dikenali sama.
_STOPWORDS = frozenset({
    "yang", "di", "ke", "dari", "pada", "adalah", "sedang", "akan", "untuk", "dan",
    "itu", "ada", "dalam", "sebuah", "tentang", "juga", "sudah", "telah", "dengan",
    "oleh", "saat", "kalau", "bahwa", "ini", "atau", "nya", "para", "lebih", "masih",
})

# Ambang kemiripan token. Dipilih dari data nyata, bukan tebakan:
# pada 60 entri vault per [date removed], kemiripan TERTINGGI antara dua fakta yang memang
# BERBEDA cuma 0,154 — jadi 0,35 memberi jarak aman 2,3x. Di ambang ini klaster
# "debut co-host" (6 varian) runtuh jadi 1, dan nol fakta sah yang salah digabung.
#
# Sengaja konservatif karena kesalahannya tidak simetris: duplikat yang lolos masih
# bisa dibersihkan belakangan, tapi fakta sah yang ditolak hilang diam-diam dan tidak
# akan pernah ketahuan.
_DUP_SIMILARITY_THRESHOLD = 0.35


def _fact_tokens(norm: str) -> frozenset[str]:
    """Token bermakna dari fakta yang sudah dinormalisasi.

    Angka SELALU dipertahankan berapa pun panjangnya. Filter `len > 2` yang polos
    membuang "1", "2", "27" — sehingga "episode 1" vs "episode 2" dan "debut 27 Mei"
    vs "debut 28 Mei" jadi identik. Ini bukan hipotesis: terukur 1.000 sebelum
    diperbaiki, dan EP1/EP2 memang lore nyata di stream ini.
    """
    cleaned = re.sub(r"[^a-z0-9\s]", " ", norm)
    return frozenset(
        w for w in cleaned.split()
        if (len(w) > 2 or w.isdigit()) and w not in _STOPWORDS
    )


def _fact_numbers(norm: str) -> frozenset[str]:
    """Semua rentetan angka di dalam fakta — termasuk yang menempel ('ep2' -> '2')."""
    return frozenset(re.findall(r"\d+", norm))


def fact_similarity(a: str, b: str) -> float:
    """Jaccard antar himpunan token. 1.0 = identik, 0.0 = tidak ada kata yang sama."""
    ta, tb = _fact_tokens(_normalize_fact(a)), _fact_tokens(_normalize_fact(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _numbers_conflict(a_norm: str, b_norm: str) -> bool:
    """True kalau kedua fakta memuat angka DAN angkanya berbeda.

    Angka sangat membedakan makna: episode 1 vs 2, 27 vs 28 Mei, target 1 jam vs 3 jam.
    Kalau angkanya bentrok, dua fakta itu bukan duplikat sebesar apa pun kemiripan
    kata-katanya — jadi ini memveto uji kemiripan, bukan sekadar menurunkan skornya.
    Kalau salah satu tidak punya angka, tidak ada yang bisa dibandingkan: lolos ke
    uji kemiripan seperti biasa.
    """
    na, nb = _fact_numbers(a_norm), _fact_numbers(b_norm)
    return bool(na) and bool(nb) and na != nb


def is_duplicate_learning(
    fact: str,
    existing_lines: list[str],
    *,
    threshold: float = _DUP_SIMILARITY_THRESHOLD,
) -> bool:
    """True kalau `fact` sudah terwakili entri yang ada.

    Tiga lapis, dari paling murah:
      1. sama persis setelah normalisasi
      2. salah satu memuat yang lain (substring)
      3. kemiripan himpunan token >= ambang  <- LAPIS BARU

    Lapis 3 ditambahkan 2026-07-31. Tanpa itu, gerbang ini buta terhadap parafrase:
    terbukti 6 dari 6 varian "Arti debut co-host 27 Mei 2026" lolos semua, karena
    sisipan kata di tengah ("pada", "tanggal", "adalah") memutus uji substring.
    Itulah yang membuat vault menumpuk jadi 60 entri dengan 28 di antaranya duplikat.
    """
    norm = _normalize_fact(fact)
    if len(norm) < 4:
        return True
    new_tokens = _fact_tokens(norm)
    for line in existing_lines:
        m = re.match(r"^-\s*\[\d{4}-\d{2}-\d{2}\]\s*(.+)$", line.strip())
        prev_raw = m.group(1) if m else line.strip().lstrip("- ").strip()
        if not prev_raw:
            continue
        prev = _normalize_fact(prev_raw)
        if norm == prev:
            return True
        if len(norm) > 20 and (norm in prev or prev in norm):
            return True
        if threshold > 0 and new_tokens and not _numbers_conflict(norm, prev):
            prev_tokens = _fact_tokens(prev)
            if prev_tokens:
                sim = len(new_tokens & prev_tokens) / len(new_tokens | prev_tokens)
                if sim >= threshold:
                    return True
    return False


def should_save_learning(fact: str, existing_lines: list[str] | None = None) -> bool:
    t = (fact or "").strip()
    if len(t) < _MIN_LEARNING_LEN:
        return False
    low = t.lower()
    if any(s in low for s in _SKIP_SUBSTRINGS):
        return False
    if existing_lines and is_duplicate_learning(t, existing_lines):
        return False
    return True


def list_learning_bullets(text: str) -> list[str]:
    bullets: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("## Memori Jangka Panjang"):
            in_section = True
            continue
        if in_section and line.strip().startswith("##"):
            break
        if in_section and line.strip().startswith("-"):
            bullets.append(line.strip())
    return bullets


def append_learning(
    path: Path,
    fact: str,
    *,
    max_bullets: int = _MAX_LEARNING_BULLETS,
    config: dict | None = None,
) -> bool:
    """Append learning at end of section; cap total bullets (drop oldest).

    Dua lapis gerbang duplikat, disengaja beda jangkauan:
      1. `should_save_learning` di atas — hanya lihat bullet DI BERKAS TUJUAN `path`
         sendiri (existing_lines datang dari file itu saja).
      2. `arti_vault_rag.fakta_sudah_ada` di bawah — lihat LINTAS semua berkas
         `vault/sessions/*` lewat indeks FTS yang sudah ada. Ini gerbang baru
         (19 Agu 2026) yang menutup celah nyata: gerbang lapis 1 buta kalau fakta
         yang sama pernah ditulis di berkas LAIN, sehingga "Arti debut co-host 27
         Mei 2026" bisa ditulis ulang di puluhan sesi karena tiap berkas mulai
         dari nol. Semua tiga jalur tulis fakta (curator observer, reflection
         openrouter, save_long_term_memory bridge) funnel lewat sini, jadi satu
         perbaikan di titik ini menutup celah di ketiganya sekaligus.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    existing = list_learning_bullets(text)
    if not should_save_learning(fact, existing):
        print(f"[Memory] Skip learning (quality gate): {fact[:72]}...")
        return False

    # `config is not None` sengaja jadi syarat: pemanggil lama (skrip, tes) yang tidak
    # tahu-menahu soal gerbang lintas-berkas ini TIDAK diam-diam ikut menoleh ke DB RAG
    # produksi yang mungkin kebetulan ada di disk (`data/vault_rag.db`) — perilaku
    # mereka persis seperti sebelum gerbang ini ada. Tiga pemanggil produksi
    # (arti_curator, arti_openrouter, hermes_vtuber_bridge) semuanya SUDAH diperbarui
    # untuk mengirim config, jadi jalur nyata tetap terlindungi.
    if config is not None:
        try:
            import arti_vault_rag

            if arti_vault_rag.fakta_sudah_ada(fact, config):
                print(f"[Vault] fakta duplikat di-skip: {fact[:72]}...")
                return False
        except Exception as e:
            print(f"[Memory] fakta_sudah_ada gagal, lanjut tulis: {e}")

    line = f"- [{time.strftime('%Y-%m-%d')}] {fact.strip()}"
    if "## Memori Jangka Panjang" not in text:
        text += f"\n\n## Memori Jangka Panjang\n\n{line}\n"
    else:
        parts = text.split("## Memori Jangka Panjang", 1)
        header = parts[0] + "## Memori Jangka Panjang\n\n"
        body = parts[1]
        bullets = existing + [line]
        if len(bullets) > max_bullets:
            bullets = bullets[-max_bullets:]
        body_rest = body
        for _ in bullets:
            body_rest = re.sub(r"^-\s*\[[^\]]+\].*\n?", "", body_rest, count=1, flags=re.MULTILINE)
        new_body = "\n".join(bullets) + "\n" + body_rest.lstrip("\n")
        text = header + new_body

    path.write_text(text, encoding="utf-8")
    print(f"[Memory] Learning saved: {line[:80]}...")
    return True


def filter_memories_for_startup(memories: list[str], today: str | None = None) -> list[str]:
    """Return bullets from today only (for startup snippet)."""
    today = today or time.strftime("%Y-%m-%d")
    prefix = f"[{today}]"
    return [m for m in memories if prefix in m]
