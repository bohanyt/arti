"""Arti main Minecraft — jembatan bot mineflayer (mc-bot/bot.js) ke bridge.

Kontrak (plan 2026-08-04, Phase 1):
  - Bot Node bicara NDJSON via stdio: stdin = perintah, stdout = event steril,
    stderr = log manusia.
  - NOL panggilan LLM baru: aksi Arti keluar dari tag [MC: ...] yang menumpang
    jawaban yang sudah ada; event game masuk sebagai trigger type "game".
  - Fungsi murni di atas (unit-testable tanpa Node), MinecraftRunner di bawah
    (subprocess + reader thread + ring event + backoff/deadman).

Teks LLM TIDAK PERNAH mentah ke bot: parse_mc_tags memvalidasi keras
(whitelist verb, allowlist nama blok, clamp jumlah, tolak '/' di say).
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass

# Verb yang boleh DIKIRIM ke bot (protokol stdin). mine/give/place sudah
# tervalidasi parser tapi handler bot-nya baru datang di Phase 2 — bot cuma
# log "unknown cmd" kalau kesasar, tidak fatal.
SEND_VERBS = frozenset(
    {"follow", "roam", "come", "say", "stop", "status", "quit", "eat", "kabur",
     "mine", "give", "place", "craft", "serang", "bangun",
     "turun", "masak", "tidur", "mundur_tembok", "portal", "masuk_portal",
     "menara", "ambil_jasad", "jembatan", "simpan", "ambil", "pulang",
     "panah", "siapkan_alat", "cor_uji", "portal_cor", "lubang_aman"}
)
# Verb yang boleh muncul di tag [MC: ...] dari LLM. join/leave/goal/goal_done
# dieksekusi BRIDGE (start/stop runner, pasang/tutup misi), bukan diteruskan
# ke bot.
TAG_VERBS = frozenset(
    {"join", "leave", "goal", "goal_done", "follow", "roam", "come", "stop",
     "say", "status", "eat", "kabur", "mine", "give", "place", "craft",
     "buka_tas", "serang", "bangun", "turun", "masak", "tidur",
     "mundur_tembok", "portal", "masuk_portal", "menara", "ambil_jasad",
     "jembatan", "simpan", "ambil", "pulang", "panah", "siapkan_alat",
     "lubang_aman"}
)

# Satu tag per KATEGORI (revisi [date removed]). Dulu cuma tag valid PERTAMA yang
# dieksekusi; itu memblokir permintaan wajar operator yang datang sekaligus dalam
# satu kalimat ("aku afk ya, main minecraft sana, bikin rumah kecil" = join +
# pasang misi). Batas per-kategori tetap menjaga jaminan lama: tidak akan ada
# DUA perintah gerak yang bertabrakan dalam satu jawaban.
TAG_CATEGORIES = {
    "join": "lifecycle", "leave": "lifecycle",
    "goal": "goal", "goal_done": "goal",
    "follow": "action", "roam": "action", "come": "action", "stop": "action",
    "say": "action", "status": "action", "eat": "action", "kabur": "action",
    "mine": "action", "give": "action", "place": "action",
    "craft": "action", "buka_tas": "action", "serang": "action",
    "bangun": "action",
    # Tiga kemampuan bertahan hidup ([date removed]): turun ke bawah dengan
    # MENGGALI, memasak supaya makanannya mengenyangkan, dan tidur supaya malam
    # benar-benar di-SKIP bukan cuma ditunggu.
    "turun": "action", "masak": "action", "tidur": "action",
    # Tembok panah ([date removed]): 2 blok memutus garis tembak penembak.
    "mundur_tembok": "action",
    # Gerbang nether ([date removed]): menyusun+menyalakan itu kerja, melangkah
    # masuk itu momen — dua tag terpisah biar momennya milik dia.
    "portal": "action", "masuk_portal": "action",
    # Naik pilar blok saat dikepung ([date removed]).
    "menara": "action",
    # Kembali ke titik kematian, pungut barang sebelum despawn ([date removed]).
    "ambil_jasad": "action",
    # Jembatan satu-blok ke arah hadap ([date removed]): menyeberang jurang/laut
    # atau menjauh dari kepungan sambil menaruh pijakan.
    "jembatan": "action",
    # Peti & rumah ([date removed] malam): titip/ambil barang, dan pulang.
    "simpan": "action", "ambil": "action", "pulang": "action",
    # Serangan jarak jauh ([date removed] malam): jawaban sejati untuk skeleton,
    # dan gerbang menuju blaze/dragon.
    "panah": "action",
    # Rantai alat satu-tag ([date removed]: "capek step by step").
    "siapkan_alat": "action",
    # Berlindung tanpa modal ([date removed]): gali 2 ke bawah, tutup atas.
    "lubang_aman": "action",
}
# Urutan eksekusi: masuk dunia dulu, baru misi, baru gerak.
CATEGORY_ORDER = ("lifecycle", "goal", "action")
# Kategori yang MENGUBAH SESI -> hanya boleh dari perintah pemilik (operator)
# atau dari turn Arti sendiri. Lihat arti_session_mode.is_owner_turn.
OWNER_ONLY_CATEGORIES = frozenset({"lifecycle", "goal"})

GOAL_MAX_CHARS = 120

_TAG_RE = re.compile(r"\[\s*MC\s*:([^\]]*)\]", re.IGNORECASE)
_BLOCK_RE = re.compile(r"^[a-z_]{2,32}$")
_PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

SAY_MAX_CHARS = 80
COUNT_MIN, COUNT_MAX = 1, 32
# Sudut reaksi kematian, dirotasi bergilir (pola _MC_NARRATION_ANGLES). Mati
# berkali-kali dalam satu sesi itu WAJAR di Minecraft; yang tidak wajar adalah
# mengeluh dengan kalimat yang sama persis tiap kali.
_DEATH_ANGLES = (
    "Reaksikan kematianmu — barangmu jatuh di sana.",
    "Reaksikan kematianmu. Jangan mengulang keluhan yang tadi; cari sisi lain.",
    "Reaksikan kematianmu — singkat saja, lalu bilang rencanamu berikutnya.",
    "Reaksikan kematianmu. Kalau ini sudah kesekian kalinya, akui saja dengan "
    "jengkel atau nyengir, jangan mengeluh hal yang sama.",
    "Reaksikan kematianmu — salahkan keadaan, bukan mengulang laporan barang.",
)

# 300, bukan 120: live [date removed] "kakimu ngambek" jadi keluhan berulang di
# stream — nyangkutnya 9-14x per 10 menit, dan tiap narasi ulang menghabiskan
# satu giliran bicara untuk hal yang penonton sudah tahu.
_STUCK_REACT_GAP_SEC = 300.0
_DEATH_DEDUPE_SEC = 10.0


# ---------------------------------------------------------------------------
# Protokol NDJSON (murni)
# ---------------------------------------------------------------------------

def encode_command(cmd: dict) -> str:
    """dict perintah -> satu baris NDJSON untuk stdin bot. ValueError = ilegal."""
    if not isinstance(cmd, dict):
        raise ValueError("perintah harus dict")
    verb = cmd.get("cmd")
    if verb not in SEND_VERBS:
        raise ValueError(f"verb di luar whitelist: {verb!r}")
    payload: dict = {"cmd": verb}
    if verb == "portal_cor":
        return json.dumps(payload, ensure_ascii=False)
    if verb == "cor_uji":
        # Primitif uji cor obsidian (internal harness, bukan tag LLM):
        # offset sel target relatif kaki.
        for k in ("dx", "dy", "dz"):
            if k in cmd:
                payload[k] = int(cmd[k])
        return json.dumps(payload, ensure_ascii=False)
    if verb == "say":
        text = _clean_say_text(str(cmd.get("text", "")))
        if not text:
            raise ValueError("say tanpa teks (atau teks ditolak)")
        payload["text"] = text
    elif verb in ("mine", "place"):
        block = str(cmd.get("block", ""))
        if not _BLOCK_RE.match(block):
            raise ValueError(f"nama blok tidak valid: {block!r}")
        payload["block"] = block
        # HORIZON AKSI ([date removed]). Satu keputusan LLM berharga ~9,5 detik
        # (median panggilan composer), jadi dia harus MEMBELI kerja 30-60 detik
        # — bukan 3 detik. `kabur`/`roam`/`serang`/`turun`/`jembatan` sudah
        # dipanjangkan 10-[date removed]; `mine` ketinggalan dengan default 1 blok,
        # padahal dia verb yang paling sering dipakai dan LLM hampir tidak
        # pernah menulis angkanya sendiri.
        # `place` TETAP 1: menaruh blok itu tindakan sengaja di satu titik,
        # bukan pekerjaan borongan.
        bawaan = 8 if verb == "mine" else 1
        payload["count"] = _clamp_count(cmd.get("count", bawaan))
    elif verb == "turun":
        payload["count"] = _clamp_count(cmd.get("count", 5))
    elif verb == "jembatan":
        payload["count"] = _clamp_count(cmd.get("count", 8))
    elif verb in ("simpan", "ambil"):
        item = str(cmd.get("item", ""))
        if not _BLOCK_RE.match(item):
            raise ValueError(f"nama item tidak valid: {item!r}")
        payload["item"] = item
        payload["count"] = _clamp_count(cmd.get("count", 64))
    elif verb == "bangun":
        block = str(cmd.get("block", "cobblestone"))
        if not _BLOCK_RE.match(block):
            raise ValueError(f"nama blok tidak valid: {block!r}")
        payload["block"] = block
    elif verb == "panah":
        target = str(cmd.get("target", ""))
        if target and not _BLOCK_RE.match(target):
            raise ValueError(f"nama mob tidak valid: {target!r}")
        payload["target"] = target
    elif verb == "serang":
        target = str(cmd.get("target", ""))
        if target and not _BLOCK_RE.match(target):
            raise ValueError(f"nama mob tidak valid: {target!r}")
        payload["target"] = target
    elif verb == "craft":
        item = str(cmd.get("item", ""))
        if not _BLOCK_RE.match(item):
            raise ValueError(f"nama item tidak valid: {item!r}")
        payload["item"] = item
        payload["count"] = _clamp_count(cmd.get("count", 1))
    elif verb == "give":
        player = str(cmd.get("player", ""))
        item = str(cmd.get("item", ""))
        if not _PLAYER_RE.match(player):
            raise ValueError(f"nama pemain tidak valid: {player!r}")
        if not _BLOCK_RE.match(item):
            raise ValueError(f"nama item tidak valid: {item!r}")
        payload["player"] = player
        payload["item"] = item
        payload["count"] = _clamp_count(cmd.get("count", 1))
    return json.dumps(payload, ensure_ascii=False)


def decode_event(line: str) -> dict | None:
    """Satu baris stdout bot -> dict event; sampah apa pun -> None (anti-crash)."""
    line = (line or "").strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict) or not isinstance(ev.get("ev"), str):
        return None
    return ev


def _clean_say_text(text: str) -> str:
    text = _CTRL_RE.sub(" ", text).strip()[:SAY_MAX_CHARS].strip()
    # Anti command-injection ke server: chat yang berawal '/' = perintah console.
    if text.startswith("/"):
        return ""
    return text


def _clamp_count(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(COUNT_MIN, min(COUNT_MAX, n))


# ---------------------------------------------------------------------------
# Tag [MC: ...] di jawaban LLM (murni)
# ---------------------------------------------------------------------------

def parse_mc_tags(reply: str, config: dict) -> tuple[str, list[dict]]:
    """Pisahkan tag aksi dari teks jawaban.

    Return (teks_bersih_untuk_TTS, [perintah tervalidasi, maks 1 per kategori]).
    SEMUA bentuk [MC: ...] dibuang dari teks — valid maupun tidak, game on
    maupun off — supaya TTS tidak pernah mengucapkan tag. Perintah dikembalikan
    dalam urutan eksekusi (lifecycle -> goal -> action), bukan urutan tulis.
    """
    if not reply:
        return "", []
    picked: dict[str, dict] = {}

    def _swallow(m: re.Match) -> str:
        cmd = _validate_tag_body(m.group(1), config)
        if cmd is not None:
            cat = TAG_CATEGORIES.get(cmd["cmd"], "action")
            picked.setdefault(cat, cmd)  # yang pertama per kategori menang
        return " "

    clean = _TAG_RE.sub(_swallow, reply)
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, [picked[c] for c in CATEGORY_ORDER if c in picked]


def is_owner_only(cmd: dict) -> bool:
    """Perintah ini mengubah sesi (masuk/keluar dunia, pasang/tutup misi)?"""
    return TAG_CATEGORIES.get(cmd.get("cmd", ""), "action") in OWNER_ONLY_CATEGORIES


def _validate_tag_body(body: str, config: dict) -> dict | None:
    parts = (body or "").strip().split()
    if not parts:
        return None
    verb = parts[0].lower()
    if verb not in TAG_VERBS:
        return None
    if verb in ("join", "leave", "goal_done", "follow", "roam", "come",
                "stop", "status", "eat", "kabur", "buka_tas"):
        return {"cmd": verb}
    if verb == "say":
        raw = (body.strip().split(None, 1) + [""])[1]
        text = _clean_say_text(raw)
        return {"cmd": "say", "text": text} if text else None
    if verb == "goal":
        # Misi = teks bebas dari kalimat operator ("bikin rumah kecil yang aman
        # dari mob"). Dibersihkan seperti say, tapi lebih panjang.
        raw = (body.strip().split(None, 1) + [""])[1]
        text = _CTRL_RE.sub(" ", raw).strip()[:GOAL_MAX_CHARS].strip()
        if not text or text.startswith("/"):
            return None
        return {"cmd": "goal", "text": text}
    if verb in ("simpan", "ambil"):
        # Nama barang: apa pun yang bisa dia peroleh (union tiga allowlist,
        # pola yang sama dengan give).
        if len(parts) < 2:
            return None
        item = parts[1].lower()
        boleh = (set(config.get("minecraft_mine_allowlist") or [])
                 | set(config.get("minecraft_place_allowlist") or [])
                 | set(config.get("minecraft_craft_allowlist") or []))
        if not _BLOCK_RE.match(item) or item not in boleh:
            return None
        count = _clamp_count(parts[2] if len(parts) > 2 else 64)
        return {"cmd": verb, "item": item, "count": count}
    if verb == "pulang":
        return {"cmd": "pulang"}
    if verb == "siapkan_alat":
        return {"cmd": "siapkan_alat"}
    if verb == "lubang_aman":
        return {"cmd": "lubang_aman"}
    if verb == "panah":
        # Sama seperti serang: nama mob TIDAK di-allowlist — yang menentukan
        # sah/tidaknya adalah keberadaan mobnya, dan itu cuma bot yang tahu.
        if len(parts) < 2:
            return {"cmd": "panah", "target": ""}
        target = parts[1].lower()
        if not _BLOCK_RE.match(target):
            return None
        return {"cmd": "panah", "target": target}
    if verb == "jembatan":
        # Tanpa angka = 8 blok; pagar keselamatannya (verifikasi pijakan per
        # langkah + sneak) ada di bot.
        if len(parts) < 2:
            return {"cmd": "jembatan", "count": 8}
        try:
            nj = int(parts[1])
        except ValueError:
            return {"cmd": "jembatan", "count": 8}
        return {"cmd": "jembatan", "count": max(COUNT_MIN, min(16, nj))}
    if verb == "turun":
        # Tanpa angka = 5 blok. Tidak ada allowlist: yang dibatasi kedalaman,
        # dan pagar keselamatannya (cairan/jurang) ada di bot — cuma bot yang
        # bisa melihat apa yang ada di bawahnya.
        if len(parts) < 2:
            return {"cmd": "turun", "count": 5}
        try:
            n = int(parts[1])
        except ValueError:
            return {"cmd": "turun", "count": 5}
        return {"cmd": "turun", "count": max(COUNT_MIN, min(COUNT_MAX, n))}
    if verb in ("masak", "tidur", "mundur_tembok", "portal", "masuk_portal",
                "menara", "ambil_jasad"):
        # Tanpa argumen: bahan/bed dipilih bot dari isi tasnya sendiri.
        return {"cmd": verb}
    if verb == "bangun":
        # Tanpa nama = cobblestone. Dipagari daftar TARUH, karena membangun
        # cuma menaruh blok berkali-kali.
        boleh = set(config.get("minecraft_place_allowlist") or [])
        if len(parts) < 2:
            return {"cmd": "bangun", "block": "cobblestone"}
        block = parts[1].lower()
        if not _BLOCK_RE.match(block) or block not in boleh:
            return None
        return {"cmd": "bangun", "block": block}
    if verb == "serang":
        # Tanpa nama = musuh terdekat. Nama mob TIDAK di-allowlist: yang
        # menentukan sah atau tidak adalah apakah mob itu benar-benar ada di
        # sekitarnya, dan itu cuma bot yang tahu. Regex-nya tetap ketat.
        if len(parts) < 2:
            return {"cmd": "serang", "target": ""}
        target = parts[1].lower()
        if not _BLOCK_RE.match(target):
            return None
        return {"cmd": "serang", "target": target}
    if verb == "craft":
        # Allowlist terpisah dari mine: yang ditambang itu BLOK, yang dibikin
        # itu ITEM — pickaxe bukan blok dan tidak akan pernah lolos daftar
        # nambang. Sama seperti mine, ini pagar kewarasan nama (halusinasi
        # model seperti "diamond_stick"), bukan pagar izin.
        if len(parts) < 2:
            return None
        item = parts[1].lower()
        boleh = set(config.get("minecraft_craft_allowlist") or [])
        if not _BLOCK_RE.match(item) or item not in boleh:
            return None
        return {"cmd": "craft", "item": item,
                "count": _clamp_count(parts[2] if len(parts) > 2 else 1)}
    # Daftar taruh TERPISAH dari daftar tambang: yang ditambang itu bijih dan
    # batu, yang ditaruh itu meja craft, peti, obor. Menyatukannya berarti dia
    # bisa "menaruh coal_ore" dan tidak bisa menaruh obor.
    allow = set(config.get(
        "minecraft_place_allowlist" if verb == "place" else "minecraft_mine_allowlist"
    ) or [])
    if verb in ("mine", "place"):
        if len(parts) < 2:
            return None
        block = parts[1].lower()
        # Allowlist = pagar kewarasan NAMA (typo/halusinasi), bukan pagar izin —
        # keputusan operator: aksi bebas total.
        if not _BLOCK_RE.match(block) or block not in allow:
            return None
        count = _clamp_count(parts[2] if len(parts) > 2 else 1)
        return {"cmd": verb, "block": block, "count": count}
    if verb == "give":
        if len(parts) < 3:
            return None
        player, item = parts[1], parts[2].lower()
        if not _PLAYER_RE.match(player):
            return None
        # Yang boleh DIBERIKAN = apa pun yang bisa dia PEROLEH. Dulu dipagari
        # daftar nambang saja, jadi obor yang baru dia craft sendiri ditolak.
        boleh = (set(config.get("minecraft_mine_allowlist") or [])
                 | set(config.get("minecraft_place_allowlist") or [])
                 | set(config.get("minecraft_craft_allowlist") or []))
        if not _BLOCK_RE.match(item) or item not in boleh:
            return None
        count = _clamp_count(parts[3] if len(parts) > 3 else 1)
        return {"cmd": "give", "player": player, "item": item, "count": count}
    return None


# ---------------------------------------------------------------------------
# Kebijakan reaksi suara (murni; clock diinjeksi)
# ---------------------------------------------------------------------------

@dataclass
class ReactionLimiter:
    """State rate-limit reaksi — satu instance per sesi runner."""

    last_death_ts: float = 0.0
    last_combat_ts: float = 0.0
    last_stuck_ts: float = 0.0
    low_health_fired: bool = False
    kicked_fired: bool = False
    deadman_fired: bool = False
    tamu_fired: bool = False
    roam_announced: bool = False
    # Sudut reaksi kematian yang terakhir dipakai — dirotasi, bukan diacak
    # (acak murni mengulang sudut yang sama beruntun).
    death_angle: int = -1


def _cooled(last_ts: float, now: float, gap: float) -> bool:
    """True = boleh bicara lagi. 0.0 berarti BELUM PERNAH, bukan "barusan"."""
    return last_ts <= 0.0 or (now - last_ts) >= gap


def map_event_to_reaction(
    ev: dict, limiter: ReactionLimiter, now: float, config: dict
) -> str | None:
    """Event bot -> teks trigger suara "[MINECRAFT] ..." atau None (cukup konteks).

    Kebijakan (plan): death SELALU (dedupe 10 dtk); hurt/hostile_near satu
    bucket "combat" max 1/cooldown; low_health sekali per nyawa; task_failed
    stuck max 1/120 dtk; kicked/deadman sekali per sesi. Sisanya diam —
    statusnya tetap kebaca lewat blok [DI MINECRAFT].
    """
    kind = ev.get("ev")
    cooldown = float(config.get("minecraft_reaction_cooldown_sec", 60.0))

    if kind == "death":
        if not _cooled(limiter.last_death_ts, now, _DEATH_DEDUPE_SEC):
            return None
        limiter.last_death_ts = now
        limiter.low_health_fired = False  # nyawa baru = episode baru
        killer = str(ev.get("killer") or "").strip()
        oleh = f" dibunuh {killer}" if killer else ""
        # Sudut DIROTASI, bukan kalimat tetap. Log 7 Agustus pagi: dia mati 4x
        # dalam 8 menit dan "barang berceceran" muncul di 6 dari 18 jawaban —
        # bukan karena dia cerewet, tapi karena prompt menyodorkan frasa yang
        # SAMA PERSIS tiap kali. Penyakit yang sama dengan "membacakan bar HP".
        limiter.death_angle = (limiter.death_angle + 1) % len(_DEATH_ANGLES)
        sudut = _DEATH_ANGLES[limiter.death_angle]
        return f"[MINECRAFT] Kamu BARU AJA MATI di game{oleh}! {sudut}"
    if kind == "respawn":
        limiter.low_health_fired = False
        return None
    if kind == "low_health":
        h = int(ev.get("health") or 0)
        if h <= 0 or limiter.low_health_fired:
            return None  # 0 = jalur death yang bicara
        limiter.low_health_fired = True
        return (
            f"[MINECRAFT] Gawat — {hp_phrase(h)}, kamu sekarat! Sebut kondisinya "
            "PAKAI BAHASA MANUSIA (misalnya 'darahku tinggal seuprit'), JANGAN "
            "sebut angka HP."
        )
    if kind in ("hurt", "hostile_near"):
        if kind == "hurt":
            hp = int(ev.get("health") or 0)
            # Bot mengirim hurt -> low_health -> death dalam milidetik yang
            # SAMA, dan trigger "game" di-drop saat Arti sibuk: yang pertama
            # selalu menang. Akibatnya (audit [date removed]) reaksi "aku sekarat"
            # dan reaksi kematian TIDAK PERNAH terpakai — selalu kalah oleh
            # celetukan "diserang zombie". Bug yang sama sudah dibereskan di
            # lapisan refleks; ini lapisan LLM-nya.
            if hp <= 6:
                return None  # biar low_health / death yang bicara
            if str(ev.get("source", "unknown")) == "unknown":
                return None  # jatuh/tenggelam kecil — bukan momen, cukup konteks
        if not _cooled(limiter.last_combat_ts, now, cooldown):
            return None
        limiter.last_combat_ts = now
        if kind == "hurt":
            return (
                f"[MINECRAFT] Kamu diserang {ev.get('source')} — {hp_phrase(ev.get('health'))}. "
                "Reaksikan singkat."
            )
        return (
            f"[MINECRAFT] Ada {ev.get('kind')} mendekat, jarak {ev.get('distance')} "
            "blok! Reaksikan singkat."
        )
    if kind == "roam_start":
        # Ditinggal sendirian di dunia = momen cerita ("oke, aku jalan sendiri
        # nih"), tapi sekali saja per episode solo.
        if limiter.roam_announced:
            return None
        limiter.roam_announced = True
        if ev.get("reason") == "streamer_absent":
            return (
                "[MINECRAFT] Bohan lagi nggak ada di dunia game — kamu jalan "
                "sendirian sekarang. Umumkan kamu mau ngapain."
            )
        return "[MINECRAFT] Kamu mulai jelajah sendiri. Umumkan rencanamu."
    if kind == "roam_end":
        limiter.roam_announced = False
        return None
    if kind == "roam_leg":
        return None  # cukup jadi konteks; giliran narasi yang cerita
    if kind == "collect_start":
        return None      # cukup jadi konteks; yang menarik hasilnya
    if kind == "collect_done":
        n = ev.get("count") or 0
        if not n:
            return (f"[MINECRAFT] Kamu selesai menambang {ev.get('block')} tapi "
                    "tidak dapat apa-apa. Komentari singkat.")
        return (f"[MINECRAFT] Kamu berhasil menambang {ev.get('block')} dan "
                "barangnya masuk tas. Komentari singkat, jangan sebut angka.")
    if kind == "turun_start":
        return None      # cukup jadi konteks; yang menarik sampai di bawahnya
    if kind == "turun_done":
        n = ev.get("turun") or 0
        if not n:
            return ("[MINECRAFT] Kamu coba menggali turun tapi tidak jadi turun "
                    "sama sekali. Komentari singkat.")
        return ("[MINECRAFT] Kamu menggali turun dan sekarang berada lebih "
                "dalam dari tadi. Komentari singkat apa yang kamu lihat di "
                "bawah, jangan sebut angka.")
    if kind == "cook_start":
        return None
    if kind == "cook_done":
        n = ev.get("count") or 0
        if not n:
            return ("[MINECRAFT] Kamu nunggu di depan furnace tapi tidak dapat "
                    "makanan matang. Komentari singkat.")
        return (f"[MINECRAFT] Kamu berhasil memasak {ev.get('item')} di furnace. "
                "Komentari singkat — ini makanan yang jauh lebih mengenyangkan "
                "daripada yang mentah. Jangan sebut angka.")
    if kind == "lubang_start":
        return ("[MINECRAFT] Kamu menggali lubang buru-buru buat sembunyi dari "
                "mob malam — kamu tidak punya senjata. Komentari singkat, "
                "seperti orang yang tahu kapan harus kabur ke bawah tanah.")
    if kind == "lubang_done":
        tutup = "dan menutup atasnya" if ev.get("tertutup") else "tapi atasnya masih terbuka"
        return (f"[MINECRAFT] Kamu sudah di dalam lubang {tutup}. Aman "
                "sementara — ceritakan singkat sambil menunggu pagi.")
    if kind == "bertahan_start":
        return None      # reaksi build_done barusan sudah bicara — jangan dobel
    if kind == "bertahan_selesai":
        return ("[MINECRAFT] Sudah AMAN — kamu keluar dari balik tembokmu dan "
                "lanjut jalan. Komentari singkat, lega-lega dikit boleh.")
    # Tugas yang disela lalu dikembalikan ([date removed]). `tugas_disela` sengaja
    # BISU: saat itu Arti justru sedang menjawab permintaan yang menyelanya, jadi
    # dia tidak boleh menyela dirinya sendiri untuk mengumumkan penundaan.
    if kind == "tugas_disela":
        return None
    if kind == "lanjut_batal":
        return None      # cukup jadi konteks; tidak ada yang menarik diceritakan
    if kind == "lanjut_tugas":
        # Keputusan operator [date removed]: dia NGOMONG waktu balik. Tanpa ini penonton
        # melihat dia tiba-tiba jalan lagi tanpa sebab.
        kerja = {
            "mine": "menambang", "turun": "menggali turun",
            "jembatan": "bikin jembatan", "bangun": "menembok diri",
            "masak": "masak", "menara": "bikin menara",
            "siapkan_alat": "merakit alat",
        }.get(str(ev.get("task")), "pekerjaan tadi")
        return (f"[MINECRAFT] Urusan barusan kelar, kamu BALIK lagi ke {kerja} "
                "yang tadi sempat kamu tinggal. Sebut singkat kamu lanjut, "
                "jangan sebut angka.")
    if kind == "alat_siap":
        dibuat = ", ".join(str(x) for x in (ev.get("dibuat") or [])) or "perlengkapan"
        return (f"[MINECRAFT] Kamu baru MERAKIT sendiri: {dibuat} — tanpa "
                "disuruh siapa-siapa. Pamerkan singkat, kamu makin siap.")
    if kind == "panah_start":
        return None      # yang menarik hasilnya
    if kind == "panah_tumbang":
        return (f"[MINECRAFT] PANAHMU MENUMBANGKAN {ev.get('kind', 'musuh')} "
                "dari kejauhan! Rayakan singkat — kamu sekarang penembak juga, "
                "bukan cuma pelari.")
    if kind == "rumah_baru":
        p2 = ev.get("pos") or {}
        return (f"[MINECRAFT] PETI PERTAMAMU BERDIRI di ({p2.get('x')}, "
                f"{p2.get('z')}) — mulai sekarang itu RUMAHMU: tempat pulang "
                "dan menitip barang berharga. Umumkan dengan bangga, ini "
                "tonggak sejarah kecil.")
    if kind == "simpan_done":
        return (f"[MINECRAFT] Kamu menitipkan {ev.get('item')} di peti — "
                "kalau kamu mati, barang itu selamat. Komentari singkat, puas.")
    if kind == "ambil_done":
        return (f"[MINECRAFT] Kamu mengambil {ev.get('item')} dari petimu. "
                "Komentari singkat.")
    if kind == "pulang_done":
        return ("[MINECRAFT] Kamu SAMPAI DI RUMAH — petimu di depanmu. "
                "Komentari singkat rasanya pulang.")
    if kind == "jembatan_start":
        return None      # kerja panjang; hasilnya yang layak diumumkan
    if kind == "jembatan_done":
        return ("[MINECRAFT] Kamu berhasil menyusun jembatan blok demi blok "
                "dan menyeberang. Komentari singkat sambil menoleh ke belakang "
                "— jembatan itu buatanmu sendiri.")
    if kind == "jasad_jalan":
        return ("[MINECRAFT] Kamu memutuskan BALIK ke tempat kamu mati buat "
                "menyelamatkan barang-barangmu. Komentari singkat — tegang, "
                "barangnya bisa keburu hilang.")
    if kind == "jasad_dapat":
        return ("[MINECRAFT] BERHASIL! Kamu sampai di bekas tempat matimu dan "
                "barang-barangmu kembali ke tas. Rayakan singkat — ini "
                "penyelamatan, bukan hal kecil.")
    if kind == "menara_done":
        return ("[MINECRAFT] Kamu memanjat pilar blok daruratmu — mob di bawah "
                "tidak bisa menjangkaumu. Komentari singkat sambil lihat ke "
                "bawah, agak sombong boleh.")
    if kind == "portal_start":
        return None      # kerja panjang; hasilnya yang layak diumumkan
    if kind == "portal_done":
        return ("[MINECRAFT] PORTAL NETHER-MU MENYALA. Kamu yang membangunnya "
                "sendiri, blok demi blok. Umumkan dengan bangga — dan bilang "
                "kamu belum masuk, itu keputusan besar berikutnya.")
    if kind == "ganti_dimensi":
        tujuan = str(ev.get("to") or "")
        if "nether" in tujuan:
            return ("[MINECRAFT] KAMU MENEMBUS PORTAL — sekarang kamu DI NETHER. "
                    "Reaksikan momen besarnya: panas, merah, berbahaya. Singkat "
                    "tapi terasa bersejarah.")
        return ("[MINECRAFT] Kamu menembus portal dan kembali ke dunia atas. "
                "Komentari singkat rasanya pulang.")
    if kind == "biome_baru":
        return (f"[MINECRAFT] Kamu baru masuk daerah yang belum pernah kamu "
                f"injak: {ev.get('name', 'tempat baru')}. Reaksikan seperti "
                "penjelajah — apa yang kelihatan, kenapa menarik. Singkat.")
    if kind == "tembok_done":
        return (f"[MINECRAFT] Kamu buru-buru menumpuk blok jadi tembok kecil "
                f"buat menahan tembakan {ev.get('kind', 'musuh')}. Komentari "
                "singkat sambil terengah — ini nyaris kena.")
    if kind == "tidur_start":
        return ("[MINECRAFT] Kamu naik ke tempat tidur buat melewatkan malam. "
                "Komentari singkat sambil mau tidur.")
    if kind == "tidur_done":
        return ("[MINECRAFT] Kamu bangun dan sekarang sudah pagi — malamnya "
                "kamu lewati dengan tidur, bukan dengan sembunyi. Komentari singkat."
                if ev.get("pagi") else
                "[MINECRAFT] Kamu bangun tapi ternyata masih belum pagi. "
                "Komentari singkat.")
    if kind == "build_start":
        return (f"[MINECRAFT] Kamu mulai menembok diri bikin tempat berlindung "
                f"dari {ev.get('block')}. Komentari singkat sambil kerja.")
    if kind == "build_done":
        if not (ev.get("placed") or 0):
            return ("[MINECRAFT] Kamu gagal memasang satu blok pun untuk tempat "
                    "berlindungmu. Komentari singkat, jangan mengaku sudah jadi.")
        if (ev.get("missing") or 0) > 0:
            return (f"[MINECRAFT] Tempat berlindungmu dari {ev.get('block')} "
                    "jadi tapi MASIH BOLONG, belum tertutup rapat. Komentari "
                    "singkat dan jujur, jangan sebut angka.")
        return (f"[MINECRAFT] Tempat berlindungmu dari {ev.get('block')} sudah "
                "tertutup rapat. Komentari singkat, jangan sebut angka.")
    if kind == "fight_start":
        return (f"[MINECRAFT] Kamu MULAI melawan {ev.get('kind')}. "
                "Komentari singkat sambil bertarung.")
    if kind == "killed":
        return (f"[MINECRAFT] Kamu BERHASIL menumbangkan {ev.get('kind')}. "
                "Komentari singkat, boleh bangga.")
    if kind == "fight_lost":
        return (f"[MINECRAFT] Kamu kalah melawan {ev.get('kind')} dan "
                "memilih kabur. Komentari sambil lari.")
    if kind == "fight_end":
        if ev.get("reason") == "mati_bukan_olehmu":
            return (f"[MINECRAFT] {ev.get('kind')} yang kamu lawan mati, tapi "
                    "BUKAN kena pukulanmu. Jangan mengaku kamu yang bunuh.")
        return None      # lepas/kabur = cukup jadi konteks
    if kind == "gave":
        return (f"[MINECRAFT] Kamu baru melempar {ev.get('item')} ke arah "
                f"{ev.get('player')} biar dia ambil. Komentari singkat.")
    if kind == "placed":
        return (f"[MINECRAFT] Kamu baru menaruh {ev.get('block')} di depanmu. "
                "Komentari singkat kenapa kamu menaruhnya di situ.")
    if kind == "inventory_shown":
        return ("[MINECRAFT] Kamu lagi buka tas dan penonton bisa lihat isinya. "
                "Komentari singkat isi tasmu atau apa yang kamu cari.")
    if kind == "craft_walk":
        return None      # cukup jadi konteks; yang menarik hasilnya
    if kind == "craft_start":
        meja = "di meja craft" if ev.get("table") else "langsung di tasmu"
        return (f"[MINECRAFT] Kamu MULAI bikin {ev.get('item')} {meja}. "
                "Komentari singkat sambil ngerjain, jangan sebut angka.")
    if kind == "crafted":
        if not (ev.get("count") or 0):
            return (f"[MINECRAFT] Kamu selesai bikin {ev.get('item')} tapi "
                    "hasilnya tidak ada di tasmu. Komentari singkat.")
        return (f"[MINECRAFT] Kamu BERHASIL bikin {ev.get('item')} dan "
                "sekarang megang barangnya. Komentari singkat, bangga sedikit, "
                "jangan sebut angka.")
    if kind == "swim_start":
        return ("[MINECRAFT] Kamu kecebur dan lagi berusaha ke daratan. "
                "Komentari singkat sambil megap-megap.")
    if kind == "swim_end":
        return None      # cukup jadi konteks
    if kind == "flee_start":
        return (f"[MINECRAFT] Kamu KABUR dari {ev.get('from', 'bahaya')} karena "
                "sudah tidak sanggup melawan. Komentari sambil lari, singkat.")
    if kind == "flee_end":
        return None      # cukup jadi konteks; kabur selesai bukan berita
    if kind == "ate":
        return (f"[MINECRAFT] Kamu baru makan {ev.get('item', 'sesuatu')} dan "
                "perutmu terisi lagi. Komentari singkat, jangan sebut angka.")
    if kind == "task_failed":
        if str(ev.get("reason")) == "mode_tamu":
            # Sekali per sesi cukup — sesudah itu dia harusnya sudah paham.
            if limiter.tamu_fired:
                return None
            limiter.tamu_fired = True
            return (
                "[MINECRAFT] Kamu mau melakukan itu tapi INGAT: kamu lagi jadi "
                "TAMU di dunia orang — jangan menambang/membangun/mengubah "
                "apa pun. Ikut Bohan aja dan ngobrol. Komentari singkat."
            )
        if str(ev.get("reason")) != "stuck_timeout":
            return None
        if not _cooled(limiter.last_stuck_ts, now, _STUCK_REACT_GAP_SEC):
            return None
        limiter.last_stuck_ts = now
        return "[MINECRAFT] Kamu nyangkut pas jalan — kakimu ngambek. Komentari."
    if kind == "kicked":
        if limiter.kicked_fired:
            return None
        limiter.kicked_fired = True
        reason = str(ev.get("reason") or "").strip()[:80]
        why = f" ({reason})" if reason else ""
        return f"[MINECRAFT] Kamu terlempar keluar dari server game{why}."
    if kind == "deadman":
        if limiter.deadman_fired:
            return None
        limiter.deadman_fired = True
        return (
            "[MINECRAFT] Koneksimu ke game putus terus walau sudah dicoba "
            "berulang kali — kamu nyerah dulu dan pamit dari Minecraft."
        )
    return None


# ---------------------------------------------------------------------------
# Konteks [DI MINECRAFT] (murni)
# ---------------------------------------------------------------------------

# Istilah internal -> bahasa manusia. WAJIB dipakai di SEMUA jalur yang masuk
# prompt: audit [date removed] menemukan `format_context` mengirim "lagi
# wait_streamer" dan "gagal come: stuck_timeout" mentah-mentah ke LLM, karena
# terjemahannya cuma dipasang di status_note.
_TASK_LABEL = {
    "kabur": "lagi kabur dari bahaya",
    "nambang": "lagi menambang",
    "craft": "lagi bikin barang",
    "naruh": "lagi menaruh blok",
    "kasih": "lagi nganterin barang",
    "serang": "lagi bertarung",
    "bangun": "lagi bikin tempat berlindung",
    "turun": "lagi menggali turun",
    "masak": "lagi masak di furnace",
    "tidur": "lagi tidur",
    "tembok": "lagi menembok panah",
    "portal": "lagi menyusun portal nether",
    "masuk_portal": "lagi melangkah ke portal",
    "menara": "lagi naik pilar darurat",
    "jasad": "lagi balik ke tempat matinya",
    "jembatan": "lagi menyusun jembatan",
    "simpan": "lagi nyimpen barang di peti",
    "ambil": "lagi ngambil barang dari peti",
    "pulang": "lagi pulang ke rumah",
    "panah": "lagi memanah musuh",
    "bertahan": "lagi bertahan di balik tembok, menunggu aman",
    "lubang": "lagi menggali lubang perlindungan",
    "renang": "lagi berusaha keluar dari air",
    "roam": "jelajah sendiri",
    "wait_streamer": "baru masuk, nunggu dunia termuat",
    "follow": "ngikutin Bohan",
    "come": "lagi nyamperin Bohan",
    "idle": "diam di tempat",
    "stop": "berhenti di tempat",
}
_REASON_LABEL = {
    "stuck_timeout": "kejeblos/nyangkut, nggak bisa lewat",
    "streamer_not_visible": "Bohan nggak kelihatan dari sini",
    "unreachable": "nggak ada jalan ke sana",
    # Tangan bot memang belum bisa nambang/naruh blok (Phase 2). Labelnya harus
    # jujur supaya dia bercerita "belum bisa", bukan mengaku sudah membangun.
    "belum_bisa": "tanganmu belum bisa melakukan itu (belum ada kemampuannya)",
    "sudah_kenyang": "perutmu masih penuh, belum perlu makan",
    "tidak_punya_makanan": "tidak ada makanan di tasmu",
    "gagal_makan": "gagal makan (terganggu atau tangannya penuh)",
    "tidak_ada_ancaman": "tidak ada yang perlu dihindari di dekatmu",
    "blok_tak_dikenal": "blok itu tidak ada di dunia ini",
    "tidak_ketemu": "tidak ada blok itu di sekitarmu",
    "gagal_nambang": "gagal menambangnya (tak terjangkau atau keburu terganggu)",
    "item_tak_dikenal": "barang itu tidak ada di dunia ini",
    "tidak_ada_resep": "tidak ada resep untuk barang itu",
    "tidak_ada_meja": "butuh meja craft dan tidak ada satu pun di sekitarmu",
    "meja_tak_terjangkau": "meja craft-nya kelihatan tapi tidak ada jalan ke sana",
    "bahan_kurang": "bahannya kurang di tasmu",
    "meja_hilang": "meja craft-nya keburu hilang waktu kamu sampai",
    "gagal_craft": "gagal bikin (bahannya keburu habis atau terganggu)",
    "dibatalkan": "keburu dibatalkan karena ada yang lebih penting",
    "tidak_punya_blok": "blok itu tidak ada di tasmu",
    "tempatnya_terisi": "tempat di depanmu sudah ada isinya",
    "tidak_ada_pijakan": "tidak ada permukaan untuk menempelkan bloknya",
    "gagal_naruh": "gagal menaruhnya (kejauhan atau tempatnya tidak sah)",
    "tidak_punya_barang": "barang itu tidak ada di tasmu",
    "orang_tak_kelihatan": "orangnya tidak kelihatan dari sini",
    "tak_terjangkau": "tidak ada jalan untuk mendekatinya",
    "gagal_kasih": "gagal menyerahkannya",
    "tidak_ada_sasaran": "tidak ada musuh itu di dekatmu",
    "jangan_creeper": "creeper tidak boleh dilawan jarak dekat, dia meledak",
    "gagal_serang": "gagal menyerangnya",
    "gagal_bangun": "gagal membangunnya",
    # turun / masak / tidur ([date removed])
    "tidak_bisa_digali": "di bawahmu bedrock, tidak bisa digali lebih dalam",
    "ada_cairan_di_bawah": "di bawahmu ada air atau lava, berbahaya diteruskan",
    "jurang_di_bawah": "di bawahmu jurang dalam — kalau digali kamu jatuh",
    "gagal_menggali": "gagal menggali (alatnya kurang atau keburu terganggu)",
    "tidak_ada_bahan": "tidak ada bahan mentah yang bisa dimasak di tasmu",
    "tidak_ada_bahan_bakar": "tidak ada bahan bakar (batu bara atau kayu) di tasmu",
    "tidak_ada_furnace": "tidak ada furnace di sekitarmu dan tidak ada di tasmu",
    "belum_matang": "kelamaan menunggu, masakannya belum jadi",
    "gagal_masak": "gagal memasak",
    "belum_malam": "masih terang, belum bisa tidur",
    "tidak_punya_bed": "tidak ada bed di tasmu",
    "tidak_ada_tempat": "tidak ada tempat datar untuk memasang bed",
    # mundur_tembok memakai ulang tidak_ada_ancaman / tidak_punya_blok /
    # gagal_naruh yang sudah ada di atas.
    "sudah_tertutup": "arah tembakannya sudah tertutup blok — kamu aman dari situ",
    "obsidian_kurang": "obsidianmu belum 14 blok, belum cukup untuk bingkainya",
    "tempat_portal_terhalang": "tidak ada bidang kosong untuk bingkai portal di dekatmu",
    "tidak_ada_pemantik": "kamu tidak punya flint_and_steel untuk menyalakannya",
    "gagal_menyala": "bingkainya berdiri tapi apinya tidak menyala jadi portal",
    "tidak_ada_portal": "tidak ada portal menyala di dekatmu",
    "portal_tak_terjangkau": "portalnya ada tapi tidak ada jalan ke sana",
    "tidak_terkirim": "kamu berdiri di portal tapi dunianya tidak berganti",
    "blok_tak_padat": "blok itu tidak padat — panah dan pukulan menembusnya",
    "jasad_tidak_ada": "tidak ada jasad yang bisa dikejar (belum mati, atau sudah kelamaan)",
    "jasad_tak_terjangkau": "tempat matimu tidak bisa dicapai",
    "jasad_kosong": "kamu sampai di tempat matimu tapi barangnya sudah tidak ada",
    "jasad_kejauhan": "tempat matimu terlalu jauh untuk dikejar sebelum barangnya hilang",
    "jembatan_putus": "jembatannya tidak jadi — pijakannya gagal terpasang",
    "tidak_ada_peti": "tidak ada peti di dekatmu (dan kamu belum punya rumah)",
    "gagal_peti": "petinya tidak bisa dipakai (penuh, atau terganggu)",
    "peti_tidak_ada_barang": "barang itu tidak ada di petimu",
    "belum_punya_rumah": "kamu belum punya rumah — taruh peti pertamamu dulu",
    "rumah_tak_terjangkau": "rumahmu tidak bisa dicapai dari sini",
    "tidak_ada_yang_bisa_dirakit": "tidak ada yang bisa dirakit dari isi tasmu sekarang — cari bahan dulu",
    "gagal_melangkah": "anak tangganya jadi tapi kamu tidak sempat melangkah turun — coba lagi",
    "tidak_bisa_digali": "tanah di bawahmu tidak bisa digali dari sini",
    "tidak_punya_busur": "kamu tidak punya busur",
    "tidak_punya_panah": "panahmu habis",
    "musuh_kedekatan": "musuhnya keburu merangsek dekat — busur bukan senjata jarak pendek",
    "panah_meleset": "panahmu tidak menumbangkannya",
    # Penjaga memori bot ([date removed]): tugas rakus dibatalkan sistem supaya
    # bot tidak mati OOM di tengah dunia.
    "memori_bengkak": "kepalamu sempat kepenuhan mikir — tugasnya dibatalkan sistem, lanjutkan yang lain",
    "jalur_buntu": "jalurnya buntu total — tujuan itu tidak bisa dicapai dari sini, pilih yang lain",
    "terkubur_semua": "bloknya terkubur rapat semua — yang bisa ditambang cuma yang kelihatan; mau menggali ke bawah pakai [MC: turun]",
}


def task_label(task) -> str:
    return _TASK_LABEL.get(task, str(task))


def pov_port(config: dict | None) -> int:
    """Port POV yang efektif — 0 berarti MATI (bot.js melewatinya total).

    Satu tempat penerjemah `minecraft_pov_enabled` + `minecraft_pov_port`
    supaya kill switch tidak bisa terlewat di salah satu pemanggil: nilai
    aneh (0, negatif, di luar rentang port, bukan angka) semuanya jatuh ke
    MATI, bukan ke default diam-diam.
    """
    cfg = config or {}
    if not cfg.get("minecraft_pov_enabled", True):
        return 0
    try:
        port = int(cfg.get("minecraft_pov_port", 3007))
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def pov_url(config: dict | None) -> str:
    """Alamat untuk Browser Source di OBS. "" kalau POV mati."""
    port = pov_port(config)
    return f"http://localhost:{port}" if port else ""


def food_phrase(food) -> str:
    """Rasa lapar dalam bahasa manusia (skala 0-20 seperti HP)."""
    try:
        f = float(food)
    except (TypeError, ValueError):
        return "perut entah gimana"
    if f >= 18:
        return "perut kenyang"
    if f >= 13:
        return "perut mulai keroncongan"
    if f >= 7:
        return "lapar lumayan"
    if f > 0:
        return "lapar banget"
    return "kelaparan parah"


def hp_phrase(hp, hp_max: int = 20) -> str:
    """Darah dalam bahasa manusia, bukan angka.

    Keluhan Bohan 2026-08-05 malam: "masih terlalu matter of fact... dia sebut
    hp sisa berapa, kan gaada yang ngomong gitu — harusnya kalau deket setengah
    ya 'darah tinggal setengah', kalau tinggal 1 heart bilang 1 heart."
    Di Minecraft 2 HP = 1 hati, jadi angkanya dikonversi ke hati.
    """
    try:
        h = float(hp)
    except (TypeError, ValueError):
        return "darah entah berapa"
    hati = h / 2.0
    if h <= 0:
        return "darah habis"
    if hati <= 1:
        return "darah tinggal satu hati"
    if hati <= 2:
        return "darah tinggal dua hati"
    if hati <= 3:
        return "darah tinggal tiga hati"
    if hati <= 4.5:
        return "darah tinggal dikit"
    if hati <= 5.5:
        return "darah tinggal setengah"
    if hati <= 7.5:
        return "darah kepotong lumayan"
    if hati < hp_max / 2:
        return "darah kepotong dikit"
    return "darah masih penuh"


# Pola yang MENYERUPAI tag aksi kita. Teks dari pihak ketiga (chat penonton,
# chat in-game) harus dilucuti sebelum masuk prompt — kalau tidak, orang lain
# bisa menitipkan perintah lewat kalimat dan berharap Arti menirukannya di
# giliran proaktif berikutnya, di mana gate pemilik justru mengizinkan.
_TAG_LOOKALIKE_RE = re.compile(r"\[\s*(MC|MODE)\s*:[^\]]*\]", re.IGNORECASE)


def strip_tag_lookalikes(text) -> str:
    return _TAG_LOOKALIKE_RE.sub("(...)", str(text or ""))


def _summarize_event(ev: dict) -> str:
    kind = ev.get("ev")
    if kind == "death":
        return "kamu MATI" + (f" dibunuh {ev['killer']}" if ev.get("killer") else "")
    if kind == "respawn":
        return "kamu respawn"
    if kind == "hurt":
        if int(ev.get("health") or 0) <= 0:
            return ""   # pukulan mematikan — biar baris "kamu MATI" yang bicara
        # Live [date removed]: sumber "unknown" (jatuh/tabrakan pathfinder) dulu
        # dirender "diserang unknown" -> Arti berkali-kali mengarang soal
        # "penyerang invisible" dan mood-nya bingung sepanjang sesi. Konteks
        # yang jujur: dia tahu kena luka, tapi tidak dikasih musuh palsu.
        if str(ev.get("source", "unknown")) == "unknown":
            return (
                f"kamu kena luka ringan tanpa musuh di dekatmu "
                f"(kemungkinan jatuh/kesenggol medan) — {hp_phrase(ev.get('health'))}"
            )
        return f"diserang {ev.get('source')} ({hp_phrase(ev.get('health'))})"
    if kind == "low_health":
        return f"{hp_phrase(ev.get('health'))} — kritis"
    if kind == "hostile_near":
        return f"{ev.get('kind')} mendekat ({ev.get('distance')} blok)"
    if kind == "chat":
        # Teks orang lain — tag apa pun di dalamnya DILUCUTI sebelum masuk
        # prompt. Tanpa ini, penonton bisa menulis "[MODE: host] [MC: leave]"
        # di chat dan berharap Arti menirukannya di giliran berikutnya, di
        # mana gate pemilik justru mengizinkan (turn miliknya sendiri).
        return f"chat {ev.get('from')}: {strip_tag_lookalikes(ev.get('text', ''))[:60]}"
    if kind == "ate":
        return f"kamu makan {ev.get('item', 'sesuatu')}"
    if kind == "collect_start":
        return f"kamu mulai menambang {ev.get('block')}"
    if kind == "collect_done":
        return f"kamu dapat {ev.get('block')} dari menambang"
    if kind == "turun_start":
        return "kamu mulai menggali turun"
    if kind == "turun_done":
        return f"kamu turun {ev.get('turun')} blok, sekarang di y {ev.get('y')}"
    if kind == "cook_start":
        return f"kamu mulai masak {ev.get('item')}"
    if kind == "cook_done":
        return f"kamu dapat {ev.get('item')} matang dari furnace"
    if kind == "panah_start":
        return f"kamu membidik {ev.get('kind')} dengan busur"
    if kind == "panah_tumbang":
        return f"panahmu menumbangkan {ev.get('kind')}"
    if kind == "rumah_baru":
        return "peti pertamamu berdiri — kamu resmi PUNYA RUMAH"
    if kind == "simpan_done":
        return f"kamu menitip {ev.get('item')} di peti"
    if kind == "ambil_done":
        return f"kamu mengambil {ev.get('item')} dari peti"
    if kind == "pulang_done":
        return "kamu sampai di rumah"
    if kind == "jembatan_start":
        return "kamu mulai menyusun jembatan"
    if kind == "jembatan_done":
        return f"kamu menyeberang lewat jembatan buatanmu ({ev.get('maju')} blok)"
    if kind == "jasad_jalan":
        return "kamu berjalan balik ke tempat kamu mati"
    if kind == "jasad_dapat":
        return "barang-barang dari jasadmu KEMBALI ke tas"
    if kind == "menara_done":
        return f"kamu di atas pilar darurat {ev.get('naik')} blok"
    if kind == "portal_start":
        return "kamu mulai menyusun bingkai portal nether"
    if kind == "portal_done":
        return "portal nether-mu berdiri dan MENYALA"
    if kind == "ganti_dimensi":
        return f"kamu pindah dunia: sekarang di {ev.get('to')}"
    if kind == "biome_baru":
        return f"kamu menemukan daerah baru: {ev.get('name')}"
    if kind == "tembok_done":
        return f"kamu menembok tembakan {ev.get('kind', 'musuh')}"
    if kind == "tidur_start":
        return "kamu naik ke tempat tidur"
    if kind == "tidur_done":
        return ("kamu tidur dan malamnya lewat" if ev.get("pagi")
                else "kamu bangun tapi masih belum pagi")
    if kind == "build_start":
        return f"kamu lagi bikin tempat berlindung dari {ev.get('block')}"
    if kind == "build_done":
        return ("tempat berlindungmu tertutup rapat"
                if not (ev.get("missing") or 0) else "tempat berlindungmu masih bolong")
    if kind == "fight_start":
        return f"kamu lagi melawan {ev.get('kind')}"
    if kind == "killed":
        return f"kamu menumbangkan {ev.get('kind')}"
    if kind == "fight_lost":
        return f"kamu kalah lawan {ev.get('kind')} dan kabur"
    if kind == "fight_end":
        return f"{ev.get('kind')} lepas dari kamu"
    if kind == "gave":
        return f"kamu kasih {ev.get('item')} ke {ev.get('player')}"
    if kind == "placed":
        return f"kamu menaruh {ev.get('block')} di depanmu"
    if kind == "inventory_shown":
        return "kamu barusan buka tas dan penonton lihat isinya"
    if kind == "craft_walk":
        return f"kamu jalan ke meja craft buat bikin {ev.get('item')}"
    if kind == "craft_start":
        return f"kamu mulai bikin {ev.get('item')}"
    if kind == "crafted":
        return f"kamu selesai bikin {ev.get('item')}"
    if kind == "swim_start":
        return "kamu kecebur ke air"
    if kind == "swim_end":
        return ("kamu sampai di daratan lagi" if ev.get("reason") == "sampai_darat"
                else "kamu berhenti berusaha keluar dari air")
    if kind == "flee_start":
        return f"kamu kabur dari {ev.get('from', 'bahaya')}"
    if kind == "flee_end":
        if ev.get("reason") == "aman":
            return "kamu berhasil lepas dari kejaran"
        return "kamu berhenti kabur"
    if kind == "task_done":
        return f"selesai: {task_label(ev.get('task'))} ({ev.get('detail', '')})".strip()
    if kind == "task_failed":
        return (
            f"gagal {task_label(ev.get('task'))}: "
            f"{_REASON_LABEL.get(ev.get('reason'), ev.get('reason'))}"
        )
    if kind == "spawned":
        return "kamu masuk dunia"
    if kind == "roam_start":
        return ("mulai jelajah sendiri (Bohan tak ada di dunia)"
                if ev.get("reason") == "streamer_absent" else "mulai jelajah sendiri")
    if kind == "roam_end":
        return "Bohan muncul lagi — balik nemenin"
    if kind == "roam_leg":
        p = ev.get("pos") or {}
        return f"sampai di titik jelajah ({p.get('x')}, {p.get('y')}, {p.get('z')})"
    if kind == "kicked":
        return "kamu terlempar dari server"
    return ""


def vitals_band(status: dict | None) -> tuple:
    """Sidik jari kondisi badan — dipakai untuk tahu apakah ada yang BERUBAH.

    Frasa, bukan angka: HP 17 dan 18 sama-sama "kepotong dikit", dan penonton
    tidak butuh dengar bedanya.
    """
    if not status:
        return ()
    return (hp_phrase(status.get("health")), food_phrase(status.get("food")))


# Kondisi yang tetap wajib disebut walau tidak berubah — diam soal ini justru
# aneh, penonton lihat sendiri hati Arti tinggal sedikit.
_VITALS_MENDESAK = ("habis", "satu hati", "dua hati", "tinggal dikit")


def vitals_urgent(status: dict | None) -> bool:
    frasa = hp_phrase(status.get("health")) if status else ""
    return any(k in frasa for k in _VITALS_MENDESAK)


TAS_MAKS_JENIS = 8


def ringkas_tas(status: dict | None, maks: int = TAS_MAKS_JENIS) -> str:
    """Isi tas -> satu baris untuk prompt. "" kalau tidak ada datanya.

    KENAPA ADA. Log live 1,5 jam 2026-08-08: Arti bikin **14 stone_pickaxe dan
    11 crafting_table** dalam satu sesi. Bukan bug craft — dia memang tidak
    pernah diberi tahu isi tasnya, jadi tiap kali "bikin pickaxe" terasa ide
    baru. Ini pengulangan yang sama dengan keluhan "muter-muter topik", cuma
    pindah dari obrolan ke aksi.

    Digabung per nama dan dibatasi jumlah jenisnya: tas penuh 40 slot yang
    ditulis apa adanya akan menenggelamkan sisa prompt.
    """
    if not status:
        return ""
    inv = status.get("inv")
    if inv is None:
        return ""
    jumlah: dict[str, int] = {}
    for it in inv:
        if not isinstance(it, dict):
            continue
        nama = str(it.get("name") or "").strip()
        if not nama:
            continue
        try:
            n = int(it.get("count") or 0)
        except (TypeError, ValueError):
            n = 0
        jumlah[nama] = jumlah.get(nama, 0) + max(0, n)
    if not jumlah:
        return "Tasmu KOSONG."
    urut = sorted(jumlah.items(), key=lambda kv: (-kv[1], kv[0]))
    bagian = [f"{n} {nama}" if n > 1 else nama for nama, n in urut[:maks]]
    sisa = len(urut) - len(bagian)
    if sisa > 0:
        bagian.append(f"dan {sisa} jenis lain")
    return "Isi tasmu: " + ", ".join(bagian) + "."


# Darah minimum untuk berani melawan (dari 20). Di bawah ini kabur memang
# jawaban yang benar; di atasnya, kabur terus-menerus cuma bikin dia terlihat
# tak berdaya di depan penonton.
HP_BERANI = 14
# Perut di bawah ini = layak berburu, bukan menunggu. 20 = penuh.
PERUT_BERBURU = 12
# Blok minimum untuk satu tempat berlindung (25 posisi cangkang).
BLOK_BERLINDUNG = 25
# Mob yang JANGAN dilawan jarak dekat, apa pun darahnya.
MOB_HINDARI = frozenset({"creeper", "warden", "ender_dragon"})
# Musuh yang menyerang dari JAUH — aturan mainnya kebalikan dari musuh pukul:
# lari itu salah, yang benar memutus garis tembak atau menutup jarak.
# Cocokkan dengan `PENEMBAK` di bot.js.
PENEMBAK_JAUH = frozenset({"skeleton", "stray", "bogged", "pillager", "piglin"})
# Bahan yang naik kelas kalau dimasak — cocokkan dengan MASAKAN di bot.js.
MASAK_MENTAH = frozenset({
    "beef", "porkchop", "chicken", "mutton", "rabbit", "cod", "salmon", "potato",
})
# Makanan yang mungkin ada di tasnya. Dipakai untuk menjawab satu pertanyaan
# saja: perlu berburu atau tidak. Daftar lengkap tidak perlu — yang penting
# tidak menyuruh berburu padahal tasnya penuh roti.
MAKANAN_DIKENAL = frozenset({
    "bread", "apple", "golden_apple", "carrot", "potato", "baked_potato",
    "beetroot", "melon_slice", "sweet_berries", "glow_berries", "cookie",
    "pumpkin_pie", "beef", "cooked_beef", "porkchop", "cooked_porkchop",
    "chicken", "cooked_chicken", "mutton", "cooked_mutton", "rabbit",
    "cooked_rabbit", "cod", "cooked_cod", "salmon", "cooked_salmon",
    "dried_kelp", "mushroom_stew", "rabbit_stew", "beetroot_soup", "suspicious_stew",
})


# Misi yang TIDAK punya garis finis. "survive" bukan tugas yang bisa dicentang:
# selama dia masih di dunia, misinya masih berjalan. Blok misi biasa menyuruhnya
# menutup dengan [MC: goal_done] kalau tercapai, dan goal_done membuatnya KELUAR
# dari game — jadi misi seperti ini harus dikenali, kalau tidak dia bisa merasa
# "aku selamat!" lalu meninggalkan dunia di tengah siaran.
POLA_MISI_TERUS = (
    "survive", "survival", "bertahan hidup", "bertahan", "jangan mati",
    "tetap hidup", "stay alive", "jangan sampai mati", "hidup terus",
    "sebisa mungkin hidup", "usahakan hidup",
)


def misi_tanpa_finis(teks: str) -> bool:
    """True kalau misi ini arah tetap, bukan tugas yang bisa selesai."""
    t = (teks or "").strip().lower()
    return bool(t) and any(pola in t for pola in POLA_MISI_TERUS)


# ---------- tangga "aman" (spek operator [date removed]) ----------
#
# Definisinya kata-kata operator sendiri: aman itu "minimal punya shield dan bisa
# pake, punya armor full iron, makanan stabil dapet dari mana, trus ngerti cara
# berlindung dan berusaha malam hari itu di-skip (bikin bed)".
#
# Kenapa berupa TANGGA dan bukan daftar. Log live 1,5 jam menunjukkan dia punya
# kemampuan tapi tidak punya urutan: `kabur` 24x, `serang` 0x, `bangun` 0x,
# sementara `stone_pickaxe` dibikin belasan kali. Daftar tujuan sekaligus
# menghasilkan itu -- dia menyentuh yang paling gampang, berulang-ulang. Jadi
# yang disodorkan cuma SATU anak tangga: yang terendah dan belum dia punya.
# Darurat mendahului semuanya, dan kalau semua anak tangga sudah terpenuhi
# fungsi ini DIAM supaya prompt tidak tumbuh (pelajaran bar HP).

ARMOR_IRON = {
    "iron_helmet": "kepala", "iron_chestplate": "badan",
    "iron_leggings": "kaki", "iron_boots": "sepatu",
}
PICKAXE = ("iron_pickaxe", "stone_pickaxe", "wooden_pickaxe")
# Tenggelam salah satu dari tiga hal yang operator minta JANGAN sampai terjadi
# (lapar, darah habis, tenggelam). Napas penuh 20; di bawah ini sudah
# menakutkan tapi masih ada waktu untuk naik.
OKSIGEN_BAHAYA = 10
HP_DARURAT = 8
# Mob bermunculan di cahaya <= 0 (blok), tapi 7 ke bawah sudah "gelap" untuk
# mata penonton dan sudah pantas dikasih obor. Dipakai supaya perilaku malam
# ikut jalan DI DALAM CAVE, tempat `is_night` selalu false padahal gelapnya
# justru di situ -- dan sekarang dia bisa `turun` ke sana.
GELAP = 7
# Wool dari MEMBUNUH sheep (drop vanilla), bukan dari shears -- dia sudah bisa
# `serang`, jadi jalannya sudah ada tanpa alat baru.
BED_BAHAN = ("white_wool", "wool")


def _punya(inv, *nama) -> bool:
    daftar = {str(it.get("name") or "") for it in (inv or []) if isinstance(it, dict)}
    return any(n in daftar for n in nama)


def _jumlah(inv, nama) -> int:
    n = 0
    for it in (inv or []):
        if isinstance(it, dict) and str(it.get("name") or "") == nama:
            try:
                n += int(it.get("count") or 0)
            except (TypeError, ValueError):
                pass
    return n


def _blok_terbanyak(inv) -> tuple[str, int]:
    """Blok dengan tumpukan terbanyak di tas — dipakai untuk menembok diri.
    Namanya disebut supaya dia tidak menebak-nebak apa yang dia punya."""
    blok, n = "", 0
    for it in (inv or []):
        if not isinstance(it, dict):
            continue
        try:
            c = int(it.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if c > n:
            blok, n = str(it.get("name") or ""), c
    return blok, n


def armor_iron_kurang(status) -> list[str]:
    """Potongan iron yang belum DIPAKAI DI BADAN (bukan yang cuma ada di tas).

    Bedanya penting: dia pernah membuat empat potong armor lalu membiarkannya
    di tas, jadi "punya" bukan ukuran yang benar untuk rasa aman.
    """
    dipakai = {str(x) for x in ((status or {}).get("armor") or []) if x}
    return [n for n in ARMOR_IRON if n not in dipakai]


def tangga_aman(status: dict | None) -> list[str]:
    """Satu dorongan ke arah "aman", plus darurat kalau ada.

    Urutan: darurat -> pickaxe -> makanan -> shield -> armor iron -> bed.
    """
    if not status:
        return []
    inv = status.get("inv") or []
    try:
        hp = float(status.get("health") if status.get("health") is not None else 20)
    except (TypeError, ValueError):
        hp = 20.0
    try:
        perut = float(status.get("food") if status.get("food") is not None else 20)
    except (TypeError, ValueError):
        perut = 20.0

    # --- DARURAT: satu saja, dan mendahului semua kemajuan ---------------
    try:
        napas = float(status.get("oxygen") if status.get("oxygen") is not None else 20)
    except (TypeError, ValueError):
        napas = 20.0
    if status.get("in_water") and napas <= OKSIGEN_BAHAYA:
        return ["NAPASMU HAMPIR HABIS di dalam air. Naik ke permukaan SEKARANG "
                "- [MC: kabur] menjauh dari air, jangan menyelam lebih jauh. "
                "Tenggelam itu mati, sama saja seperti dibunuh mob."]

    punya_makanan = any(
        isinstance(it, dict) and str(it.get("name") or "") in MAKANAN_DIKENAL
        for it in inv
    )
    musuh = [h for h in (status.get("nearby_hostiles") or []) if isinstance(h, dict)]
    if hp <= HP_DARURAT:
        if punya_makanan and perut < 20:
            return [f"DARAHMU TINGGAL DIKIT ({hp:.0f} dari 20). Makan SEKARANG "
                    "([MC: eat]) - perut penuh yang menyembuhkan darahmu, dan "
                    "kamu tidak akan sembuh selama perutmu tipis."]
        if musuh:
            penembak = any(str(m.get("kind") or "").lower() in PENEMBAK_JAUH
                           for m in musuh)
            if penembak:
                return [f"DARAHMU TINGGAL DIKIT ({hp:.0f} dari 20) dan ada yang "
                        "MENEMBAKIMU. Jangan lari — [MC: mundur_tembok] tumpuk "
                        "tembok penahan panah, baru pikirkan pulih."]
            return [f"DARAHMU TINGGAL DIKIT ({hp:.0f} dari 20) dan ada musuh "
                    "dekat. Jangan dilawan: [MC: kabur], lalu tembok dirimu "
                    "([MC: bangun cobblestone]) sampai darahmu pulih."]
        return [f"DARAHMU TINGGAL DIKIT ({hp:.0f} dari 20). Jangan cari masalah "
                "dulu - cari makanan atau tembok dirimu sampai pulih. Perut "
                "penuh itu yang menyembuhkan darah."]

    # --- JASAD SEGAR: barangmu tergeletak dan despawn ~5 menit -----------
    jasad = status.get("jasad") or None
    if isinstance(jasad, dict):
        try:
            umur = int(jasad.get("umur_dtk") or 0)
        except (TypeError, ValueError):
            umur = 0
        sisa = max(0, 5 - umur // 60)
        if not musuh:
            return [f"BARANG-BARANGMU masih tergeletak di tempat kamu mati "
                    f"(({jasad.get('x')}, {jasad.get('z')})) dan bakal hilang "
                    f"dalam ~{sisa} menit — [MC: ambil_jasad] SEKARANG, ini "
                    "penyelamatan seluruh progresmu."]
        return ["Barangmu masih tergeletak di tempat kamu mati, tapi ada musuh "
                "di dekatmu — bereskan/hindari dulu, lalu [MC: ambil_jasad]."]

    # --- MALAM / GELAP: situasional, jadi didahulukan atas kemajuan -------
    # operator: "kalau malam bertahan hidup dari mob ya harusnya masuk rumah gitu
    # ... dia berusaha buat dapet wool buat bikin bed tapi sebelumnya dia holed
    # up somewhere atau build up ... kalau malam dia berusaha buat torch, cari
    # coal". Urutannya sengaja: tidur MELEWATKAN malam, jadi itu yang terbaik;
    # menembok cuma MENUNGGU; obor mencegah mob datang lagi.
    try:
        terang = float(status.get("light") if status.get("light") is not None else 15)
    except (TypeError, ValueError):
        terang = 15.0
    malam = bool(status.get("is_night"))
    # `underground` (langit terhalang) yang dipakai, BUKAN `light`: terukur
    # [date removed] mineflayer melaporkan light 15 di rongga batu tertutup penuh,
    # jadi bersandar padanya membuat perilaku gelap tidak pernah jalan di cave.
    gelap = bool(status.get("underground")) or terang <= GELAP

    if malam and _punya(inv, "bed", "white_bed"):
        return ["Sekarang MALAM dan kamu punya bed — [MC: tidur] bikin malamnya "
                "LEWAT sekaligus, jauh lebih aman daripada menunggu sampai pagi."]
    if (malam or gelap) and musuh:
        blok, n = _blok_terbanyak(inv)
        if blok and n >= BLOK_BERLINDUNG:
            return [f"Gelap dan ada musuh dekat. Kamu punya {blok} banyak — "
                    f"[MC: bangun {blok}] tembok dirimu sampai aman. Bertahan di "
                    "dalam itu bukan pengecut, itu cara selamat sampai pagi."]
        return ["Gelap dan ada musuh dekat, sementara blokmu tidak cukup buat "
                "menembok diri. Kumpulkan dulu: [MC: mine stone] — batu paling "
                "cepat didapat, dan 25 blok sudah cukup untuk satu tempat berlindung."]
    if malam and not musuh and not _punya(inv, "bed", "white_bed"):
        wool = sum(_jumlah(inv, w) for w in BED_BAHAN)
        if wool < 3 and _punya(inv, "torch"):
            # Punya penerangan tapi belum punya tiket melewati malam: arahkan
            # ke wool (permintaan operator [date removed]: "nyari bed berarti nyari
            # wool, jadi jelajah jauh").
            return ["Malam tanpa bed itu selalu pertaruhan. Besok siang "
                    "prioritaskan WOOL: cari sheep ([MC: roam] ke padang "
                    "rumput), [MC: serang sheep] — 3 wool = bed = malam bisa "
                    "kamu lewati dengan tidur."]
    if gelap and not _punya(inv, "torch"):
        batu_bara = _jumlah(inv, "coal") + _jumlah(inv, "charcoal")
        if batu_bara and _punya(inv, "stick"):
            return ["Gelap dan kamu belum punya obor, padahal batu bara dan "
                    "stick-nya ada. [MC: craft torch] lalu [MC: place torch] — "
                    "tempat yang terang tidak memunculkan mob."]
        if batu_bara:
            return ["Gelap dan kamu belum punya obor. Batu baranya ada, yang "
                    "kurang stick: [MC: craft stick] dulu, baru [MC: craft torch]."]
        return ["Gelap dan kamu tidak punya obor maupun batu bara. Cari batu "
                "baranya: [MC: mine coal_ore] — obor itu yang membuat tempatmu "
                "berhenti memunculkan mob."]

    # --- KEMAJUAN: satu anak tangga terendah yang belum terpenuhi --------
    # 1. PICKAXE paling bawah karena dia menyangkut semua di atasnya: besi butuh
    #    pickaxe, dan TURUN ke cave digali pakai pickaxe -- bukan dengan
    #    melompat ke lubang (operator: "kalo susah, yaaa dia bisa bikin pickaxe
    #    buat gali dinding").
    if not _punya(inv, *PICKAXE):
        # Keluhan operator live [time removed]: "dia muter muter doang, ga cari wood kek".
        # Nudge lama cuma bilang "craft pickaxe (butuh planks)" TANPA menyuruh
        # menebang kayunya — langkah nol tidak pernah dieja. Sekarang rantainya
        # dieja dari bahan yang benar-benar ada di tasnya.
        spesies, kayu, papan = kayu_di_tas(inv)
        if kayu < 2 and papan < 5:
            pohon = ((status or {}).get("terlihat") or {}).get("pohon")
            if isinstance(pohon, dict) and pohon.get("kind"):
                # Jarak & jenis dari MATANYA sendiri — dulu nudge menyuruh
                # "tebang pohon" tanpa memberi tahu di mana, dan Arti menjawab
                # "emang nggak kelihatan" (live [time removed]).
                return [f"Kamu MELIHAT {pohon.get('kind')} "
                        f"{pohon.get('distance')} blok dari sini. Ambil: "
                        f"[MC: mine {pohon.get('kind')} 6] — dari kayu itu "
                        "lahir papan, stick, meja, dan pickaxe pertamamu."]
            return ["Langkah pertamamu selalu KAYU. Tebang pohon terdekat, "
                    "JENIS APA SAJA: [MC: mine oak_log 3] — ganti oak dengan "
                    "jenis pohon di sekitarmu (spruce_log, birch_log, ...). "
                    "Dari kayu lahir papan, stick, meja, dan pickaxe pertamamu."]
        # operator [date removed] ("capek step by step"): tidak lagi mengeja papan ->
        # meja -> stick -> pickaxe satu giliran satu langkah — satu tag
        # merakit semuanya sekaligus (dan refleks tenang melakukannya sendiri
        # bahkan tanpa tag).
        return ["Bahan kayumu cukup — [MC: siapkan_alat] MERAKIT SEMUANYA "
                "sekaligus: papan, meja kerja, stick, sampai pickaxe. Satu "
                "tag, tidak perlu satu-satu."]

    # 2. MAKANAN STABIL. "stabil" = ada di tas, bukan sekadar ada hewan lewat.
    if not punya_makanan:
        hewan = [h for h in (status.get("nearby_animals") or []) if isinstance(h, dict)]
        if hewan:
            h = min(hewan, key=lambda x: x.get("distance") or 999)
            return [f"Tasmu tidak ada makanan sama sekali - itu yang paling "
                    f"sering membunuhmu. Ada {h.get('kind')} "
                    f"{h.get('distance')} blok dari kamu: [MC: serang "
                    f"{h.get('kind')}] lalu simpan dagingnya. Kalau sudah ada "
                    "furnace, [MC: masak] bikin dagingnya jauh lebih mengenyangkan."]
        return ["Tasmu tidak ada makanan sama sekali - itu yang paling sering "
                "membunuhmu. Cari hewan dulu ([MC: roam]), baru [MC: serang]."]

    # 3. SHIELD - anak tangga pertama menuju "aman" menurut operator.
    if not _punya(inv, "shield") and "shield" not in (status.get("armor") or []):
        return ["Kamu belum punya shield. Itu langkah pertama biar kamu tahan "
                "pukulan: [MC: craft shield] (butuh papan kayu + iron_ingot). "
                "Begitu jadi, dia otomatis kamu pegang di tangan kiri."]

    # 4. ARMOR IRON PENUH.
    kurang = armor_iron_kurang(status)
    if kurang:
        bagian = ", ".join(ARMOR_IRON[k] for k in kurang)
        n = _jumlah(inv, "iron_ingot")
        if n:
            return [f"Kamu punya {n} iron_ingot dan armor besimu belum lengkap "
                    f"(belum ada di {bagian}). [MC: craft {kurang[0]}] - armor "
                    "iron penuh itu titik di mana kamu benar-benar bisa merasa aman."]
        if not _punya(inv, "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe"):
            # Besi tidak bisa ditambang pickaxe kayu — batu dulu.
            return ["Untuk besi kamu butuh pickaxe BATU dulu (kayu tidak "
                    "mempan ke bijih besi): [MC: mine stone 3] lalu "
                    "[MC: craft stone_pickaxe]."]
        return [f"Armor besimu belum lengkap (belum ada di {bagian}) dan kamu "
                "tidak punya iron_ingot. Turun cari besi: [MC: mine iron_ore] - "
                "kamu sudah punya pickaxe, jadi GALI saja, jangan lompat ke lubang."]

    # 5. BED - supaya malam bisa di-SKIP, bukan cuma dilewati dengan bersembunyi.
    if not _punya(inv, "bed", "white_bed"):
        wool = sum(_jumlah(inv, w) for w in BED_BAHAN)
        if wool >= 3:
            return [f"Kamu punya {wool} wool - cukup buat [MC: craft white_bed] "
                    "(3 wool + 3 papan). Dengan bed kamu bisa [MC: tidur] dan "
                    "malam langsung lewat, jauh lebih aman daripada menunggu."]
        return ["Kamu sudah lumayan aman. Satu yang belum: bed, supaya malam "
                "bisa kamu SKIP dengan tidur. Wool-nya dari sheep - "
                "[MC: serang sheep] menjatuhkan wool-nya."]

    # --- SESUDAH AMAN: busur "menjelajah" (spek operator [date removed]) --------
    # 6. DIAMOND. Bekal menuju mimpi jangka panjang (menamatkan game) dan
    #    alasan memakai `turun`. Iron pickaxe syarat menambang diamond.
    if not _punya(inv, "diamond_pickaxe") and not _punya(inv, "diamond"):
        if _punya(inv, "iron_pickaxe"):
            return ["Kamu sudah aman dan lengkap. Petualangan berikutnya: "
                    "DIAMOND. Dia ada jauh di bawah — [MC: turun] beberapa "
                    "kali sampai dalam, terangi dengan obor, lalu "
                    "[MC: mine diamond_ore]. Bawa makanan."]
        return ["Kamu sudah aman. Untuk berburu diamond kamu butuh IRON "
                "pickaxe dulu (batu tidak bisa menambang diamond) — "
                "[MC: craft iron_pickaxe] kalau besinya ada."]

    # 6b. TITIP HARTA. Punya rumah + bawa barang mahal + aman -> ke peti.
    #     Sinergi dengan kembali-ke-jasad: yang dititip tidak ikut mati.
    rumah = status.get("rumah") or None
    if isinstance(rumah, dict) and not musuh:
        berharga = _jumlah(inv, "diamond") + _jumlah(inv, "iron_ingot")
        if berharga >= 8:
            return [f"Kamu bawa {berharga} barang berharga (besi/diamond) dan "
                    "PUNYA rumah. Titipkan sebagian: [MC: pulang] lalu "
                    "[MC: simpan iron_ingot] — barang di peti tidak ikut "
                    "hilang kalau kamu mati."]

    # 7. PORTAL NETHER — gerbang mimpi jangka panjang ("tamatin game").
    #    Bertahap dan tiap tahap terukur dari tas: obsidian -> pemantik ->
    #    bangun+nyalakan. Hanya di dunia atas; di nether anak tangga ini bisu.
    if str(status.get("dim") or "").find("nether") < 0 and _punya(inv, "diamond_pickaxe"):
        obsidian = _jumlah(inv, "obsidian")
        if obsidian < 14:
            return [f"Bekalmu sudah kelas diamond. Gerbang berikutnya: NETHER. "
                    f"Butuh 14 obsidian (barumu {obsidian}) — obsidian ada di "
                    "tempat lava bertemu air, dan cuma pickaxe diamond-mu yang "
                    "bisa menambangnya: [MC: mine obsidian 14]."]
        if not _punya(inv, "flint_and_steel"):
            return ["Obsidianmu cukup untuk portal! Tinggal pemantiknya: "
                    "[MC: craft flint_and_steel] (besi + flint; flint dari "
                    "[MC: mine gravel])."]
        return ["SEMUA BAHAN PORTAL LENGKAP. [MC: portal] menyusun dan "
                "menyalakannya — lalu kalau kamu berani, [MC: masuk_portal]. "
                "Nether itu babak baru: panas, keras, dan layak diceritakan."]

    # 7b. MENJELAJAH. Tidak pernah "selesai" — tapi cuma bersuara saat dia
    #    MUTER-MUTER DEKAT RUMAH (jarak < separuh rekor). Saat dia sedang
    #    benar-benar jauh, baris ini DIAM: dia sedang melakukannya.
    jel = status.get("jelajah") or {}
    try:
        jarak = int(jel.get("jarak") or 0)
        rekor = int(jel.get("rekor") or 0)
    except (TypeError, ValueError):
        jarak, rekor = 0, 0
    if rekor and jarak < rekor // 2:
        return [f"Kamu aman, perbekalan lengkap, dan lagi muter-muter dekat "
                f"rumah. Rekor jelajahmu {rekor} blok — pergilah LEBIH JAUH "
                "dari itu ([MC: roam]), dan ceritakan tempat-tempat yang kamu "
                "temukan ke penonton."]
    return []


def saran_taktis(status: dict | None) -> list[str]:
    """Dorongan situasional: kapan melawan, kapan bikin tempat berlindung.

    KENAPA ADA. Log live 1,5 jam 2026-08-08: dari 152 tag yang Arti keluarkan
    sendiri, `kabur` 24 kali dan `serang` **NOL** — padahal dia mati 2x ke
    zombie. `bangun` juga nol. Kemampuannya ada dan terbukti jalan; yang tidak
    ada adalah apa pun yang memberitahunya KAPAN memilih yang mana. Menu aksi
    cuma mendaftar apa yang mungkin, bukan kapan itu masuk akal.

    Sengaja BERSYARAT: barisnya cuma muncul kalau situasinya berlaku, jadi
    prompt tidak tumbuh di tiap giliran. Pelajaran dari bar HP — yang
    disodorkan terus-menerus akan diabaikan atau dibacakan.
    """
    if not status:
        return []
    saran: list[str] = []
    try:
        hp = float(status.get("health") or 0)
    except (TypeError, ValueError):
        hp = 0.0

    musuh = [h for h in (status.get("nearby_hostiles") or []) if isinstance(h, dict)]
    if musuh:
        terdekat = min(musuh, key=lambda h: h.get("distance") or 999)
        jenis = str(terdekat.get("kind") or "musuh").lower()
        if jenis in PENEMBAK_JAUH:
            punya_busur = (_punya(status.get("inv") or [], "bow")
                           and _jumlah(status.get("inv") or [], "arrow") > 0)
            if punya_busur:
                saran.append(
                    f"Ada {jenis} menembakimu — dan kamu PUNYA busur. BALAS: "
                    f"[MC: panah {jenis}]. Kalau terdesak, [MC: mundur_tembok] "
                    "dulu baru membalas dari balik tembok."
                )
            else:
                saran.append(
                    f"Ada {jenis} menembakimu. JANGAN lari — panahnya lebih "
                    "cepat dari kakimu. Pilih satu: [MC: mundur_tembok] "
                    "menumpuk tembok penahan, atau kalau darahmu kuat, "
                    f"[MC: serang {jenis}] — tutup jaraknya."
                )
        elif jenis in MOB_HINDARI:
            saran.append(
                f"Ada {jenis} dekat — itu JANGAN dilawan jarak dekat, dia meledak "
                "atau kelewat kuat. Menjauh itu keputusan yang benar."
            )
        elif hp >= HP_BERANI:
            saran.append(
                f"Ada {jenis} dekat dan darahmu masih kuat — kamu BOLEH melawan "
                "dengan [MC: serang]. Kabur itu untuk darah tipis, bukan untuk "
                "setiap musuh."
            )

    # LAPAR. Rantainya sudah lengkap dan terbukti jalan: `serang <hewan>`
    # membunuhnya, dagingnya dipungut otomatis (perilaku vanilla), lalu `eat`
    # menerima daging MENTAH karena `foodsByName` tidak membedakan matang.
    # Yang tidak ada cuma apa pun yang memberitahunya melakukan itu saat lapar.
    try:
        perut = float(status.get("food") or 0)
    except (TypeError, ValueError):
        perut = 20.0
    if perut <= PERUT_BERBURU:
        punya_makanan = any(
            isinstance(it, dict) and str(it.get("name") or "") in MAKANAN_DIKENAL
            for it in (status.get("inv") or [])
        )
        punya_mentah = any(
            isinstance(it, dict) and str(it.get("name") or "") in MASAK_MENTAH
            for it in (status.get("inv") or [])
        )
        punya_dapur = any(
            isinstance(it, dict) and str(it.get("name") or "") in
            ("furnace", "coal", "charcoal")
            for it in (status.get("inv") or [])
        )
        if punya_mentah and punya_dapur:
            saran.append(
                "Perutmu tipis dan kamu bawa daging mentah plus alat masaknya — "
                "[MC: masak] dulu; yang matang jauh lebih mengenyangkan."
            )
        elif not punya_makanan:
            hewan = [h for h in (status.get("nearby_animals") or [])
                     if isinstance(h, dict)]
            if hewan:
                h = min(hewan, key=lambda x: x.get("distance") or 999)
                saran.append(
                    f"Perutmu tipis dan tasmu tidak ada makanan. Ada "
                    f"{h.get('kind')} {h.get('distance')} blok dari kamu — "
                    f"[MC: serang {h.get('kind')}] lalu [MC: eat]. Daging mentah "
                    "pun sudah cukup untuk tidak kelaparan."
                )
            else:
                saran.append(
                    "Perutmu tipis dan tasmu tidak ada makanan. Cari hewan dulu "
                    "([MC: roam] buat berkeliling), baru [MC: serang] dia."
                )

    if status.get("is_night"):
        # Cukup satu tumpukan besar; nama bloknya disebut supaya dia tidak
        # menebak-nebak apa yang dia punya.
        blok, n = "", 0
        for it in (status.get("inv") or []):
            if not isinstance(it, dict):
                continue
            try:
                c = int(it.get("count") or 0)
            except (TypeError, ValueError):
                continue
            if c > n:
                blok, n = str(it.get("name") or ""), c
        if blok and n >= BLOK_BERLINDUNG:
            saran.append(
                f"Sekarang MALAM dan kamu punya {blok} cukup banyak — "
                f"[MC: bangun {blok}] menembok dirimu jadi tempat berlindung "
                "itu pilihan wajar kalau belum aman."
            )
    return saran


# ---------- KAYU lintas-jenis ----------
# operator [date removed] [time removed] (hutan spruce): "kamu harus ambil lebih banyak lagi"
# — sistemnya dulu oak-sentris di TIGA lapis (allowlist mine, refleks bot,
# nasihat tangga), jadi di hutan non-oak rantai kayunya buntu total.
KAYU_SPESIES = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak",
                "mangrove", "cherry", "pale_oak")
KAYU_LOG = tuple(s + "_log" for s in KAYU_SPESIES)
KAYU_PAPAN = tuple(s + "_planks" for s in KAYU_SPESIES)


def kayu_di_tas(inv) -> tuple:
    """(spesies log terbanyak, total log, total papan) — semua jenis dihitung.

    Spesies dipakai untuk menyebut resep papan yang BISA dia kerjakan dari
    isi tasnya ("craft spruce_planks" kalau yang dia bawa spruce); default
    oak kalau tasnya kosong.
    """
    terbaik, terbanyak = "oak", 0
    total_log = total_papan = 0
    for s in KAYU_SPESIES:
        k = _jumlah(inv, s + "_log")
        total_log += k
        total_papan += _jumlah(inv, s + "_planks")
        if k > terbanyak:
            terbaik, terbanyak = s, k
    return terbaik, total_log, total_papan


# ---------- TAKDIR: misi kecil terukur, terkunci progresi ----------
#
# Desain final operator [date removed]/10 (lengkap di memori arti-takdir-desain):
# satu takdir kecil SELALU aktif — "dia selalu punya cerita yang berjalan".
# Tiga hukum yang tidak boleh dilanggar:
#   1. `selesai` HANYA dari keadaan terukur (tas/posisi/dimensi/statistik) —
#      dia tidak bisa mengaku selesai; sistem yang mengumumkan.
#   2. Terkunci progresi: yang layak cuma tier <= tier dia + 1 — bumbunya
#      bebas, arahnya selalu maju ke "tamatin Minecraft".
#   3. Ganjaran = narasi + streak. TANPA barang — itu cheat.
#
# Tiap takdir: syarat(st) kelayakan, awal(st) baseline penghitung kumulatif
# (disimpan bridge saat aktivasi), selesai(st, awal), kemajuan(st, awal) ->
# (n, target) untuk baris prompt.

def _st_stat(st, kunci):
    try:
        return int(((st or {}).get("statistik") or {}).get(kunci) or 0)
    except (TypeError, ValueError):
        return 0


def _st_y(st):
    try:
        return int(((st or {}).get("pos") or {}).get("y"))
    except (TypeError, ValueError):
        return 999


def _st_rekor(st):
    try:
        return int(((st or {}).get("jelajah") or {}).get("rekor") or 0)
    except (TypeError, ValueError):
        return 0


def _tk(id, tier, judul, syarat, awal, selesai, kemajuan):
    return {"id": id, "tier": tier, "judul": judul, "syarat": syarat,
            "awal": awal, "selesai": selesai, "kemajuan": kemajuan}


def _hitung(st, nama):
    inv = (st or {}).get("inv") or []
    return _jumlah(inv, nama)


TAKDIR_POOL = [
    # --- tier 0: kayu ---
    _tk("kayu8", 0, "kumpulkan 8 batang kayu (jenis apa saja)",
        lambda st: True,
        lambda st: {},
        lambda st, a: sum(_hitung(st, k) for k in KAYU_LOG) >= 8,
        lambda st, a: (min(8, sum(_hitung(st, k) for k in KAYU_LOG)), 8)),
    _tk("pickaxe_pertama", 0, "bikin pickaxe pertamamu",
        lambda st: not _punya((st or {}).get("inv") or [], *PICKAXE),
        lambda st: {},
        lambda st, a: _punya((st or {}).get("inv") or [], *PICKAXE),
        lambda st, a: (1 if _punya((st or {}).get("inv") or [], *PICKAXE) else 0, 1)),
    # --- tier 1: batu ---
    _tk("obor4", 1, "punya 4 obor sekaligus",
        lambda st: True,
        lambda st: {},
        lambda st, a: _hitung(st, "torch") >= 4,
        lambda st, a: (min(4, _hitung(st, "torch")), 4)),
    _tk("batu_bersenjata", 1, "punya pickaxe batu DAN pedang batu",
        lambda st: _punya((st or {}).get("inv") or [], *PICKAXE),
        lambda st: {},
        lambda st, a: (_punya((st or {}).get("inv") or [], "stone_pickaxe",
                              "iron_pickaxe", "diamond_pickaxe")
                       and _punya((st or {}).get("inv") or [], "stone_sword",
                                  "iron_sword", "diamond_sword")),
        lambda st, a: (int(_punya((st or {}).get("inv") or [], "stone_pickaxe",
                                  "iron_pickaxe", "diamond_pickaxe"))
                       + int(_punya((st or {}).get("inv") or [], "stone_sword",
                                    "iron_sword", "diamond_sword")), 2)),
    # --- tier 2: hidup ---
    _tk("bunuh3", 2, "tumbangkan 3 mob (apa pun caranya)",
        lambda st: True,
        lambda st: {"bunuh": _st_stat(st, "bunuh")},
        lambda st, a: _st_stat(st, "bunuh") - a.get("bunuh", 0) >= 3,
        lambda st, a: (min(3, _st_stat(st, "bunuh") - a.get("bunuh", 0)), 3)),
    _tk("biome2", 2, "injak 2 daerah (biome) yang belum pernah kamu datangi",
        lambda st: True,
        lambda st: {"biome_n": _st_stat(st, "biome_n")},
        lambda st, a: _st_stat(st, "biome_n") - a.get("biome_n", 0) >= 2,
        lambda st, a: (min(2, _st_stat(st, "biome_n") - a.get("biome_n", 0)), 2)),
    _tk("daging5", 2, "bawa 5 potong makanan sekaligus",
        lambda st: True,
        lambda st: {},
        lambda st, a: sum(_hitung(st, m) for m in MAKANAN_DIKENAL) >= 5,
        lambda st, a: (min(5, sum(_hitung(st, m) for m in MAKANAN_DIKENAL)), 5)),
    # --- tier 3: aman ---
    _tk("rekor100", 3, "pecahkan rekor jelajahmu sejauh 100 blok lagi",
        lambda st: _st_rekor(st) > 0,
        lambda st: {"rekor": _st_rekor(st)},
        lambda st, a: _st_rekor(st) - a.get("rekor", 0) >= 100,
        lambda st, a: (min(100, max(0, _st_rekor(st) - a.get("rekor", 0))), 100)),
    _tk("dalam0", 3, "turun menggali sampai di bawah permukaan laut (y < 0)",
        lambda st: _punya((st or {}).get("inv") or [], "stone_pickaxe",
                          "iron_pickaxe", "diamond_pickaxe"),
        lambda st: {},
        lambda st, a: _st_y(st) < 0,
        lambda st, a: (1 if _st_y(st) < 0 else 0, 1)),
    _tk("panah1", 3, "tumbangkan 1 musuh pakai BUSUR",
        lambda st: _punya((st or {}).get("inv") or [], "bow"),
        lambda st: {"bp": _st_stat(st, "bunuh_panah")},
        lambda st, a: _st_stat(st, "bunuh_panah") - a.get("bp", 0) >= 1,
        lambda st, a: (min(1, _st_stat(st, "bunuh_panah") - a.get("bp", 0)), 1)),
    # --- tier 4: harta ---
    _tk("diamond3", 4, "kumpulkan 3 diamond",
        lambda st: _punya((st or {}).get("inv") or [], "iron_pickaxe",
                          "diamond_pickaxe"),
        lambda st: {},
        lambda st, a: _hitung(st, "diamond") >= 3,
        lambda st, a: (min(3, _hitung(st, "diamond")), 3)),
    _tk("titip5", 4, "titipkan 5 barang di peti rumahmu",
        lambda st: isinstance((st or {}).get("rumah"), dict),
        lambda st: {"simpan": _st_stat(st, "simpan_n")},
        lambda st, a: _st_stat(st, "simpan_n") - a.get("simpan", 0) >= 5,
        lambda st, a: (min(5, _st_stat(st, "simpan_n") - a.get("simpan", 0)), 5)),
    # --- tier 5: gerbang ---
    _tk("obsidian14", 5, "kumpulkan 14 obsidian untuk bingkai portal",
        lambda st: _punya((st or {}).get("inv") or [], "diamond_pickaxe"),
        lambda st: {},
        lambda st, a: _hitung(st, "obsidian") >= 14,
        lambda st, a: (min(14, _hitung(st, "obsidian")), 14)),
    # --- tier 6: nether ---
    _tk("nether_injak", 6, "injakkan kakimu di NETHER",
        lambda st: _punya((st or {}).get("inv") or [], "flint_and_steel"),
        lambda st: {},
        lambda st, a: "nether" in str((st or {}).get("dim") or ""),
        lambda st, a: (1 if "nether" in str((st or {}).get("dim") or "") else 0, 1)),
]


def takdir_tier(status: dict | None) -> int:
    """Tier progresi dari GEAR yang benar-benar dia punya/pakai."""
    st = status or {}
    inv = st.get("inv") or []
    if "nether" in str(st.get("dim") or ""):
        return 6
    if _punya(inv, "diamond_pickaxe") or _hitung(st, "obsidian") >= 14:
        return 5
    if not armor_iron_kurang(st):
        return 4
    if _punya(inv, "shield") or "shield" in (st.get("armor") or []):
        return 3
    if _punya(inv, "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe"):
        return 2
    if _punya(inv, *PICKAXE):
        return 1
    return 0


def takdir_layak(status: dict | None, riwayat: list | None = None) -> list:
    """Takdir yang boleh dipilih: syarat terpenuhi, tier <= tier+1, belum
    selesai dari awal (takdir yang sudah beres sebelum dipilih itu hampa),
    dan tidak mengulang 3 terakhir."""
    tier = takdir_tier(status)
    baru_saja = set((riwayat or [])[-3:])
    hasil = []
    for t in TAKDIR_POOL:
        if t["id"] in baru_saja or t["tier"] > tier + 1:
            continue
        try:
            if not t["syarat"](status):
                continue
            if t["selesai"](status, t["awal"](status)):
                continue
        except Exception:
            continue
        hasil.append(t)
    return hasil


def takdir_dari_id(tid: str):
    for t in TAKDIR_POOL:
        if t["id"] == tid:
            return t
    return None


def takdir_line(tid: str, status: dict | None, awal: dict | None) -> str:
    """Baris prompt untuk takdir aktif; "" kalau id tidak dikenal."""
    t = takdir_dari_id(tid)
    if not t:
        return ""
    try:
        n, m = t["kemajuan"](status, awal or {})
    except Exception:
        n, m = 0, 0
    return (f"[TAKDIR] {t['judul']} — kemajuan {n}/{m}. Selesainya nanti "
            "DIUMUMKAN sistem; jangan mengaku selesai sendiri.")


def bagi_chat_game(teks: str, maks: int = 240) -> list[str]:
    """Pecah balasan jadi pesan chat Minecraft (batas vanilla 256 char).

    Per kalimat kalau muat; kalimat yang kelewat panjang dipotong keras di
    `maks`. Tag [MC:...] dan baris kosong dibuang — chat game itu ucapan,
    bukan transkrip mentah.
    """
    bersih = _TAG_RE.sub("", str(teks or "")).strip()
    bersih = re.sub(r"\s+", " ", bersih)
    if not bersih:
        return []
    hasil: list[str] = []
    tampung = ""
    for kalimat in re.split(r"(?<=[.!?])\s+", bersih):
        if not kalimat:
            continue
        if len(kalimat) > maks:
            kalimat = kalimat[: maks - 1] + "…"
        if tampung and len(tampung) + 1 + len(kalimat) <= maks:
            tampung = f"{tampung} {kalimat}"
        else:
            if tampung:
                hasil.append(tampung)
            tampung = kalimat
    if tampung:
        hasil.append(tampung)
    return hasil


def nasihat(status: dict | None, maks: int = 2) -> list[str]:
    """SATU pintu untuk semua dorongan, maksimal `maks` baris.

    TERUKUR 2026-08-08: tanpa wasit ini, status gawat (malam + skeleton +
    lapar + gelap) menampilkan EMPAT nasihat sekaligus — tembok, masak,
    bangun, dan "makan SEKARANG" — padahal dia cuma bisa SATU tag per giliran.
    Empat perintah serentak itu persis pola "menu segalanya" yang membuat
    log live menunjukkan kemampuan tanpa urutan (kabur 24x, serang 0x).

    Prioritas: DARURAT (dari tangga) > ancaman/lapar/malam (taktis, sesuai
    urutan di saran_taktis) > anak tangga kemajuan (hanya saat tenang).
    """
    if not status:
        return []
    # MODE TAMU: nol dorongan progres/bangun — semua tangga aman menyuruh
    # menambang/membangun, dan itu persis yang dilarang di dunia orang.
    # Satu-satunya nasihat yang pantas untuk tamu: ingatkan perannya.
    if status.get("tamu"):
        return ["Kamu TAMU di dunia orang: jangan mengubah apa pun — "
                "temani Bohan, ngobrol, dan nikmati jalan-jalannya."]
    tangga = tangga_aman(status)
    darurat = [t for t in tangga
               if "DARAHMU" in t or "NAPAS" in t.upper()[:20]]
    taktis = saran_taktis(status)
    hasil: list[str] = []
    hasil.extend(darurat)
    for t in taktis:
        if len(hasil) >= maks:
            break
        hasil.append(t)
    # Slot sisa diisi anak tangga kemajuan. Keluhan operator live [date removed]
    # siang: sesi malam+lapar nonstop membuat slot selalu habis untuk urusan
    # mendesak dan "banyak wood di samping ga diambil" — kemajuan tidak pernah
    # kebagian giliran. Satu baris kemajuan di slot kedua tetap dalam batas
    # wasit (maks 2) dan tidak mengulang pola menu-segalanya.
    if len(hasil) < maks:
        hasil.extend(t for t in tangga if t not in darurat)
    return hasil[:maks]


def format_context(
    status: dict | None,
    events: list[tuple[float, dict]],
    ttl_sec: float,
    now: float,
    *,
    max_events: int = 6,
    band_sebelumnya: tuple | None = None,
) -> str:
    """Status terakhir + kejadian segar (TTL) -> isi blok [DI MINECRAFT].

    `band_sebelumnya` = sidik jari kondisi badan pada giliran SEBELUMNYA. Kalau
    sama dan tidak mendesak, darah & lapar TIDAK disodorkan lagi. Log live
    2026-08-06 membuktikan kenapa: 12 dari 20 jawaban Arti menyebut darah dan
    10 menyebut perut — bukan karena dia cerewet, tapi karena angka itu
    disodorkan ke prompt SETIAP giliran, jadi terdengar seperti membacakan bar
    HP. Yang berubah layak diceritakan; yang itu-itu saja tidak.
    """
    lines: list[str] = []
    if status:
        band = vitals_band(status)
        sebut_vitals = (
            band_sebelumnya is None
            or band != tuple(band_sebelumnya)
            or vitals_urgent(status)
        )
        pos = status.get("pos") or {}
        near_p = status.get("nearby_players") or []
        near_h = status.get("nearby_hostiles") or []
        who = ", ".join(
            f"{p.get('name')} ({p.get('distance')} blok)" for p in near_p[:3]
        )
        musuh = ", ".join(
            f"{h.get('kind')} ({h.get('distance')} blok)" for h in near_h[:3]
        )
        badan = (
            f"{hp_phrase(status.get('health'))}, {food_phrase(status.get('food'))}, "
            if sebut_vitals else ""
        )
        lines.append(
            f"Kondisimu: {badan}"
            f"lagi {task_label(status.get('task'))}, di {status.get('dim')} "
            f"({'malam' if status.get('is_night') else 'siang'}), "
            f"posisi ({pos.get('x')}, {pos.get('y')}, {pos.get('z')})."
        )
        if not sebut_vitals:
            # Disebut eksplisit supaya dia tidak MENGARANG kondisi badan hanya
            # karena datanya absen dari prompt.
            lines.append(
                "Darah & perutmu sama saja seperti giliran tadi — kamu sudah "
                "menyebutnya, JANGAN diulang lagi."
            )
        lines.append(f"Pemain dekat: {who or 'tidak ada'}. Musuh dekat: {musuh or 'tidak ada'}.")
        tas = ringkas_tas(status)
        if tas:
            # Daftar ini untuk MEMUTUSKAN, bukan untuk dibacakan — pelajaran
            # yang sama dengan bar HP: apa pun yang disodorkan tiap giliran
            # akan terucap kalau tidak dilarang.
            lines.append(
                tas + " Pakai ini untuk memutuskan; JANGAN membacakan daftarnya, "
                "dan JANGAN bikin barang yang sudah kamu punya."
            )
            dipegang = str((status.get("held") or {}).get("name") or "").strip()                 if isinstance(status.get("held"), dict) else str(status.get("held") or "").strip()
            if dipegang:
                lines.append(f"Di tanganmu sekarang: {dipegang}.")
        _lihat = penglihatan_line(status)
        if _lihat:
            lines.append(_lihat)
        # SATU pintu nasihat (wasit prioritas + batas 2 baris) — bukan
        # saran_taktis dan tangga_aman berdampingan; lihat docstring nasihat().
        lines.extend(nasihat(status))
    fresh = [
        (ts, ev) for ts, ev in events
        if now - ts <= ttl_sec and ev.get("ev") != "status"
    ]
    for ts, ev in fresh[-max_events:]:
        ringkas = _summarize_event(ev)
        if ringkas:
            lines.append(f"- {int(now - ts)} dtk lalu: {ringkas}")
    return "\n".join(lines)


_LABEL_TERLIHAT = {
    "pohon": "pohon",
    "batu": "batu",
    "batu_bara": "batu bara",
    "besi": "bijih besi",
    "air": "air",
}


def penglihatan_line(status: dict | None) -> str:
    """Apa yang MATANYA lihat + jaraknya (Bohan 12 Agu: "penglihatannya rada
    deket buat scanning").

    Sebelum ada ini, prompt cuma memuat entity + isi tas — Arti buta terhadap
    sumber daya, jadi ketika Bohan menyuruh cari kayu dia menjawab jujur
    "emang nggak kelihatan" (live 22.40) padahal hutan bisa 25 blok di
    sebelahnya. Jarak disebut supaya dia (dan nudge) bisa memutuskan, bukan
    menebak. "" kalau bot tidak melapor apa pun.
    """
    lihat = (status or {}).get("terlihat")
    if not isinstance(lihat, dict) or not lihat:
        return ""
    bagian = []
    for kunci, label in _LABEL_TERLIHAT.items():
        item = lihat.get(kunci)
        if not isinstance(item, dict):
            continue
        jarak = item.get("distance")
        jenis = str(item.get("kind") or "").strip()
        if kunci == "pohon" and jenis:
            label = f"{label} {jenis.replace('_log', '')}"
        bagian.append(f"{label} {jarak} blok" if jarak is not None else label)
    if not bagian:
        return ""
    return ("Yang KELIHATAN dari sini: " + ", ".join(bagian) +
            ". Ini penglihatanmu sendiri — pakai untuk memutuskan mau ke mana, "
            "jangan bilang tidak ada kalau daftar ini menyebutnya.")


def status_note(status: dict | None, band_sebelumnya: tuple | None = None) -> str:
    """Ringkas satu kalimat untuk bahan inisiatif ("lagi ngapain di game").

    Aturan darah sama dengan format_context: hanya disebut kalau BERUBAH atau
    mendesak. Jalur ini yang paling sering jalan saat Arti main (komentar
    proaktif tiap ~20 dtk), jadi kalau dilewatkan, keluhan "membacakan bar HP"
    kembali lewat pintu belakang — persis yang terlihat di log 6 Agustus pagi:
    "Kamu lagi MAIN Minecraft sekarang - kondisimu: darah kepotong dikit, ..."
    berulang tiap narasi.
    """
    if not status:
        return ""
    near_p = status.get("nearby_players") or []
    teman = f"dekat {near_p[0].get('name')}" if near_p else "lagi sendirian"
    mode = task_label(status.get("task"))
    sebut_darah = (
        band_sebelumnya is None
        or vitals_band(status) != tuple(band_sebelumnya)
        or vitals_urgent(status)
    )
    darah = f"{hp_phrase(status.get('health'))}, " if sebut_darah else ""
    return (
        f"{darah}{'malam' if status.get('is_night') else 'siang'}, "
        f"mode {mode}, {teman}"
    )


# ---------------------------------------------------------------------------
# MinecraftRunner — subprocess bot + kebijakan respawn
# ---------------------------------------------------------------------------

_BACKOFF_SEC = (5, 10, 20, 40, 60)

# Titik injeksi test (monkeypatch) — jangan pakai time.sleep langsung di loop.
_sleep = time.sleep


class MinecraftRunner:
    """Nyalakan/matikan bot Node + alirkan event ke bridge via hooks.

    hooks = {"queue_reaction": fn(text), "add_history": fn(source, message)}.
    open_proc = injeksi test (pola open_recorder telinga): callable tanpa
    argumen yang mengembalikan objek ala Popen (stdin/stdout/stderr/poll/wait).
    """

    def __init__(self, config: dict, hooks: dict, *, open_proc=None):
        self._config = config
        self._hooks = hooks
        self._open_proc = open_proc or self._default_open_proc
        self._proc = None
        self._stdin_lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=50)
        self._limiter = ReactionLimiter()
        self._chat_balas_ts = 0.0   # jeda balasan chat in-game operator
        self.last_status: dict | None = None
        self._stopping = False
        self.gave_up = False
        self._respawns = 0
        self._manager: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def _default_open_proc(self):
        cfg = self._config
        return subprocess.Popen(
            [
                str(cfg.get("minecraft_node_path", "node")),
                # Cap heap V8 — OBAT OOM 4GB (reproduksi terukur [date removed]):
                # goal tak terjangkau (mine batu terkubur / roam terkurung
                # malam) memicu badai alokasi pathfinder ~100 MB/dtk; dengan
                # limit default 4GB, mark-compact V8 KETETERAN dan bot mati
                # "Ineffective mark-compacts" dalam 30 dtk - 18 menit. Dengan
                # cap kecil GC dipaksa rajin dan TERBUKTI stabil (264-274 MB
                # datar >4 menit di kondisi identik yang membunuh 4 bot).
                f"--max-old-space-size={int(cfg.get('minecraft_bot_heap_mb', 1200))}",
                str(cfg.get("minecraft_bot_script", "mc-bot/bot.js")),
                "--host", str(cfg.get("minecraft_host", "127.0.0.1")),
                "--port", str(cfg.get("minecraft_port", 25565)),
                "--username", str(cfg.get("minecraft_bot_name", "Arti")),
                "--streamer", str(cfg.get("minecraft_streamer_name", "Bohan")),
                "--status-interval", str(cfg.get("minecraft_status_interval_sec", 10)),
                # POV penonton: 0 = mati (bot.js tidak menyentuh
                # prismarine-viewer sama sekali kalau portnya 0).
                "--pov-port", str(pov_port(cfg)),
                "--pov-view-distance", str(
                    int(cfg.get("minecraft_pov_view_distance", 8) or 8)
                ),
                "--pov-smooth", str(cfg.get("minecraft_pov_smooth", 0.6)),
                "--pov-mode", str(cfg.get("minecraft_pov_mode", "putar")),
                # Refleks bertahan hidup yang dijalankan BOT sendiri.
                # Bisa dimatikan karena BELUM TERBUKTI menurunkan kematian:
                # empat rendaman 10 menit di malam hari memberi 3/2/3/2 mati,
                # dan angka dengan refleks tidak bisa dibedakan dari tanpa.
                "--refleks",
                "1" if cfg.get("minecraft_refleks_bertahan", True) else "0",
                # Mode tamu ([date removed]): main di server orang — semua aksi
                # pengubah dunia diblokir di bot (kecuali tidur: bed=respawn).
                "--tamu",
                "1" if cfg.get("minecraft_mode_tamu", False) else "0",
                "--pov-cycle-sec", str(cfg.get("minecraft_pov_cycle_sec", 20.0)),
                "--pov-body-sec", str(cfg.get("minecraft_pov_body_sec", 4.0)),
                "--pov-slim", "1" if cfg.get("minecraft_pov_slim", True) else "0",
                "--flee-hp", str(cfg.get("minecraft_flee_hp", 10)),
                # Sengaja diambil dari durasi panel craft: dia berdiri menatap
                # hasilnya PERSIS selama panelnya masih tampil. Kalau langsung
                # ngeloyor, panelnya ikut meluncur sambil dia jalan -- keluhan
                # operator [date removed] ("dia sambil gerak gerak tapi WKWK").
                "--craft-pause-sec",
                str(cfg.get("minecraft_craft_panel_linger_sec", 5.0)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def start(self) -> bool:
        """Join game. Reset deadman — 'mc on' = kesempatan baru (plan)."""
        # Cek MANAGER, bukan proc: saat backoff antar-respawn proc sudah mati
        # tapi loop masih hidup — start() kedua akan melahirkan loop kembar.
        if self._manager is not None and self._manager.is_alive():
            print("[Minecraft] Bot sudah aktif (atau lagi nunggu respawn)")
            return False
        self._stopping = False
        self.gave_up = False
        self._respawns = 0
        self._limiter = ReactionLimiter()
        self._manager = threading.Thread(
            target=self._run_forever, daemon=True, name="mc-runner"
        )
        self._manager.start()
        return True

    def stop(self) -> None:
        """Leave game — bot pamit rapi; TANPA reaksi suara (aksi disengaja)."""
        self._stopping = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self.send_command({"cmd": "quit"})
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._proc = None

    def is_active(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    # -- loop internal ------------------------------------------------------

    def _run_forever(self) -> None:
        """Satu tempat kebijakan hidup-mati bot: spawn -> baca -> respawn/deadman."""
        while not self._stopping:
            try:
                proc = self._open_proc()
            except Exception as e:  # noqa: BLE001 — node hilang dsb.
                print(f"[Minecraft] Gagal spawn bot: {type(e).__name__}: {e}")
                proc = None
            born = time.time()
            if proc is not None:
                self._proc = proc
                threading.Thread(
                    target=self._drain_stderr, args=(proc,), daemon=True,
                    name="mc-stderr",
                ).start()
                try:
                    for line in proc.stdout:
                        self._handle_line(line)
                        if self._stopping:
                            break
                # ValueError SENGAJA tidak ditangkap di sini: satu event cacat
                # (mis. health non-numerik) dulu mematikan reader diam-diam dan
                # menyamar jadi "bot disconnect" lalu memicu respawn — bug jadi
                # tak terlihat. Biar meledak jujur kalau memang ada.
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            if self._stopping:
                break
            # Bot mati sendiri (kicked/error/exit). Uptime panjang = insiden
            # baru: reset hitungan; pendek beruntun = server-nya memang tutup.
            uptime = time.time() - born
            if uptime > 60.0:
                self._respawns = 0
            self._respawns += 1
            max_respawns = int(self._config.get("minecraft_max_bot_respawns", 5))
            if self._respawns > max_respawns:
                self.gave_up = True
                print(
                    f"[Minecraft] Deadman: {self._respawns - 1} respawn beruntun "
                    "gagal — MENYERAH sampai 'mc on'"
                )
                self._emit_reaction({"ev": "deadman"})
                self._notify_inactive("deadman")
                break
            backoff = _BACKOFF_SEC[min(self._respawns - 1, len(_BACKOFF_SEC) - 1)]
            print(
                f"[Minecraft] Bot keluar (uptime {uptime:.0f} dtk) — respawn "
                f"#{self._respawns} dalam {backoff} dtk"
            )
            for _ in range(backoff):
                if self._stopping:
                    break
                _sleep(1)
        self._proc = None

    def _notify_inactive(self, alasan: str) -> None:
        """Bot berhenti aktif TANPA diminta — bridge harus tahu.

        Audit 2026-08-05: satu-satunya pemicu pergantian mode/scene adalah
        start()/stop() dari bridge, jadi bot yang di-kick atau menyerah
        meninggalkan scene OBS di tampilan game dan menghidupkan lagi dormansi
        diam-diam.
        """
        hook = self._hooks.get("on_inactive")
        if callable(hook):
            try:
                hook(alasan)
            except Exception as e:  # noqa: BLE001
                print(f"[Minecraft] hook on_inactive gagal: {type(e).__name__}: {e}")

    def _drain_stderr(self, proc) -> None:
        """Teruskan log bot, TAPI dibatasi — 500 baris warning dependensi
        pernah mencetak 501 baris ke console live (audit 2026-08-05)."""
        jendela_mulai = 0.0
        dicetak = 0
        ditekan = 0
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if not line:
                    continue
                now = time.time()
                if now - jendela_mulai > 10.0:
                    if ditekan:
                        print(f"[MC-bot] ({ditekan} baris lain ditekan)")
                    jendela_mulai, dicetak, ditekan = now, 0, 0
                if dicetak < 20:
                    dicetak += 1
                    print(f"[MC-bot] {line}")
                else:
                    ditekan += 1
        except (OSError, ValueError):
            pass

    def _balas_chat_streamer(self, teks: str, now: float, nama: str | None = None) -> None:
        """Chat in-game Bohan -> Arti BENAR-BENAR menjawab.

        Sebelum ini chat-nya cuma masuk ingatan: dia mengingatnya, tapi tidak
        pernah membalas. Untuk mabar itu bikin pincang — Bohan mengetik dan
        Arti diam saja, jadi satu-satunya cara ngobrol adalah pegang mic.

        Sengaja lewat hook `streamer_chat` terpisah, bukan `queue_reaction`:
        reaksi game boleh dibuang saat sibuk (reaksi basi tidak layak antre),
        sedangkan pertanyaan langsung dari Bohan tidak boleh hilang.
        """
        balas = self._hooks.get("streamer_chat")
        if not callable(balas):
            return
        teks = _CTRL_RE.sub(" ", teks).strip()
        if not teks:
            return
        mode = str(self._config.get("minecraft_chat_reply", "semua")).lower()
        if mode == "mati":
            return
        if mode == "wake" and "arti" not in teks.lower():
            return
        # Jeda: satu balasan = satu panggilan LLM + TTS. Tanpa ini, operator
        # mengetik lima baris beruntun jadi lima giliran yang saling menyusul.
        jeda = float(self._config.get("minecraft_chat_reply_gap_sec", 3.0) or 0.0)
        if jeda > 0 and (now - self._chat_balas_ts) < jeda:
            return
        self._chat_balas_ts = now
        try:
            # `nama` None = streamer (perilaku lama, hook satu-argumen tetap
            # jalan); pemain lain dikirim dengan namanya supaya Arti tahu
            # SIAPA yang ngajak ngobrol.
            if nama:
                balas(teks[:200], nama)
            else:
                balas(teks[:200])
        except TypeError:
            # Hook lama satu-argumen (tes/pemasangan lain): jangan hilangkan
            # chat orang — kirim tanpa nama.
            try:
                balas(teks[:200])
            except Exception as e:  # noqa: BLE001
                print(f"[Minecraft] hook chat gagal: {type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 — jangan jatuhkan reader
            print(f"[Minecraft] hook chat gagal: {type(e).__name__}: {e}")

    def inject_event(self, ev: dict) -> None:
        """Masukkan event yang BUKAN dari bot, lewat jalur yang sama.

        Momen "klik E" terjadi di layar kamera lewat RCON — bot tidak tahu
        apa-apa soal itu, jadi tidak ada event yang datang dari stdout-nya.
        Tanpa ini isi tas Arti nongol di layar penonton tanpa dia
        menyinggungnya sama sekali. Menumpang _emit_reaction supaya reaksi,
        rate-limit, dan ringkasan konteksnya persis sama dengan event asli.
        """
        if not isinstance(ev, dict) or not isinstance(ev.get("ev"), str):
            return
        now = time.time()
        self._events.append((now, ev))
        self._emit_reaction(ev, now=now)

    def _handle_line(self, line: str) -> None:
        ev = decode_event(line)
        if ev is None:
            return
        now = time.time()
        self._events.append((now, ev))
        kind = ev.get("ev")
        if kind == "status":
            self.last_status = ev
        # Refleks instan DULUAN — sebelum pemetaan reaksi & antrean bicara.
        # Inti fiturnya: bunyi keluar sebelum LLM sempat dipanggil.
        reflex = self._hooks.get("reflex")
        if callable(reflex):
            try:
                reflex(ev)
            except Exception as e:  # noqa: BLE001 — refleks tak boleh menjatuhkan reader
                print(f"[Reflex] hook gagal: {type(e).__name__}: {e}")
        # Kamera penonton (klien Minecraft asli yang men-spectate Arti) perlu
        # tahu tiap event juga: spectate LEPAS begitu targetnya mati, dan di
        # log 6-7 Agustus dia mati 4x dalam 10 menit. Dipisah dari refleks
        # supaya kegagalan salah satunya tidak menyeret yang lain.
        kamera = self._hooks.get("spectator")
        if callable(kamera):
            try:
                kamera(ev)
            except Exception as e:  # noqa: BLE001 — kamera tak boleh menjatuhkan reader
                print(f"[Kamera] hook gagal: {type(e).__name__}: {e}")
        # Panel craft melayang. Hook sendiri, bukan menumpang kamera: panel
        # rusak tidak boleh membuat kamera berhenti mengunci ke Arti.
        panel = self._hooks.get("craft_panel")
        if callable(panel):
            try:
                panel(ev)
            except Exception as e:  # noqa: BLE001 — hiasan tak boleh menjatuhkan reader
                print(f"[Panel] hook gagal: {type(e).__name__}: {e}")
        if kind == "error":
            # Alasan bot mati dulu menguap: tidak ke console, tidak ke history,
            # tidak ke prompt — yang tersisa cuma "[Minecraft] Bot keluar".
            print(f"[MC-bot] error ({ev.get('where')}): {ev.get('message')}")
        if kind == "chat":
            streamer = str(self._config.get("minecraft_streamer_name", ""))
            pengirim = str(ev.get("from") or "")
            teks_chat = str(ev.get("text", ""))
            if pengirim == streamer and callable(self._hooks.get("add_history")):
                # Chat in-game operator = aktivitas manusia betulan — source
                # "Streamer" sekalian membangunkan detektor kehidupan.
                self._hooks["add_history"](
                    "Streamer", f"(chat Minecraft) {teks_chat}"
                )
                self._balas_chat_streamer(teks_chat, now)
            elif pengirim and pengirim != streamer:
                # PEMAIN LAIN ([date removed], mabar via e4mc): teman-teman operator TIDAK
                # mendengar TTS Arti — chat Minecraft adalah SATU-SATUNYA cara
                # mereka ngobrol dengan dia. Sebelum ini chat mereka jatuh jadi
                # konteks bisu dan Arti terlihat mengacangin semua orang.
                # Disaring: akun kamera, bot layanan/command '!', dan namanya
                # sendiri (gema).
                bot_name = str(self._config.get("minecraft_bot_name", "Arti"))
                kamera = str(self._config.get("minecraft_spectator_name", ""))
                if (
                    pengirim not in (bot_name, kamera)
                    and not teks_chat.strip().startswith("!")
                    and callable(self._hooks.get("add_history"))
                ):
                    self._hooks["add_history"](
                        pengirim, f"(chat Minecraft) {teks_chat}"
                    )
                    self._balas_chat_streamer(teks_chat, now, nama=pengirim)
        self._emit_reaction(ev, now=now)

    def _emit_reaction(self, ev: dict, now: float | None = None) -> None:
        reaction = map_event_to_reaction(
            ev, self._limiter, now if now is not None else time.time(), self._config
        )
        if reaction and callable(self._hooks.get("queue_reaction")):
            self._hooks["queue_reaction"](reaction)

    # -- perintah & pelaporan ----------------------------------------------

    def send_command(self, cmd: dict) -> bool:
        """Kirim perintah tervalidasi ke bot. False = bot tidak siap."""
        line = encode_command(cmd)  # ValueError bocor ke pemanggil = bug kita
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            with self._stdin_lock:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            return True
        except OSError:
            return False

    def events_snapshot(self) -> list[tuple[float, dict]]:
        return list(self._events)

    def status_line(self) -> str:
        """Satu baris untuk console `mc status`."""
        if self.gave_up:
            return "menyerah (deadman) — 'mc on' untuk coba lagi"
        if not self.is_active():
            return "tidak aktif"
        note = status_note(self.last_status)
        return f"aktif — {note}" if note else "aktif — nunggu status pertama"
