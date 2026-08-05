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
    {"follow", "roam", "come", "say", "stop", "status", "quit",
     "mine", "give", "place"}
)
# Verb yang boleh muncul di tag [MC: ...] dari LLM. join/leave/goal/goal_done
# dieksekusi BRIDGE (start/stop runner, pasang/tutup misi), bukan diteruskan
# ke bot.
TAG_VERBS = frozenset(
    {"join", "leave", "goal", "goal_done", "follow", "roam", "come", "stop",
     "say", "status", "mine", "give", "place"}
)

# Satu tag per KATEGORI (revisi 2026-08-04). Dulu cuma tag valid PERTAMA yang
# dieksekusi; itu memblokir permintaan wajar Bohan yang datang sekaligus dalam
# satu kalimat ("aku afk ya, main minecraft sana, bikin rumah kecil" = join +
# pasang misi). Batas per-kategori tetap menjaga jaminan lama: tidak akan ada
# DUA perintah gerak yang bertabrakan dalam satu jawaban.
TAG_CATEGORIES = {
    "join": "lifecycle", "leave": "lifecycle",
    "goal": "goal", "goal_done": "goal",
    "follow": "action", "roam": "action", "come": "action", "stop": "action",
    "say": "action", "status": "action", "mine": "action", "give": "action",
    "place": "action",
}
# Urutan eksekusi: masuk dunia dulu, baru misi, baru gerak.
CATEGORY_ORDER = ("lifecycle", "goal", "action")
# Kategori yang MENGUBAH SESI -> hanya boleh dari perintah pemilik (Bohan)
# atau dari turn Arti sendiri. Lihat arti_session_mode.is_owner_turn.
OWNER_ONLY_CATEGORIES = frozenset({"lifecycle", "goal"})

GOAL_MAX_CHARS = 120

_TAG_RE = re.compile(r"\[\s*MC\s*:([^\]]*)\]", re.IGNORECASE)
_BLOCK_RE = re.compile(r"^[a-z_]{2,32}$")
_PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

SAY_MAX_CHARS = 80
COUNT_MIN, COUNT_MAX = 1, 32
_STUCK_REACT_GAP_SEC = 120.0
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
                "stop", "status"):
        return {"cmd": verb}
    if verb == "say":
        raw = (body.strip().split(None, 1) + [""])[1]
        text = _clean_say_text(raw)
        return {"cmd": "say", "text": text} if text else None
    if verb == "goal":
        # Misi = teks bebas dari kalimat Bohan ("bikin rumah kecil yang aman
        # dari mob"). Dibersihkan seperti say, tapi lebih panjang.
        raw = (body.strip().split(None, 1) + [""])[1]
        text = _CTRL_RE.sub(" ", raw).strip()[:GOAL_MAX_CHARS].strip()
        if not text or text.startswith("/"):
            return None
        return {"cmd": "goal", "text": text}
    allow = set(config.get("minecraft_mine_allowlist") or [])
    if verb in ("mine", "place"):
        if len(parts) < 2:
            return None
        block = parts[1].lower()
        # Allowlist = pagar kewarasan NAMA (typo/halusinasi), bukan pagar izin —
        # keputusan Bohan: aksi bebas total.
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
        if not _BLOCK_RE.match(item) or item not in allow:
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
    roam_announced: bool = False


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
        return (
            f"[MINECRAFT] Kamu BARU AJA MATI di game{oleh}! Barang-barangmu "
            "berceceran di tempat kejadian. Reaksikan kematianmu."
        )
    if kind == "respawn":
        limiter.low_health_fired = False
        return None
    if kind == "low_health":
        h = int(ev.get("health") or 0)
        if h <= 0 or limiter.low_health_fired:
            return None  # 0 = jalur death yang bicara
        limiter.low_health_fired = True
        return f"[MINECRAFT] Gawat — HP kamu tinggal {h} dari 20! Kamu sekarat."
    if kind in ("hurt", "hostile_near"):
        if kind == "hurt" and str(ev.get("source", "unknown")) == "unknown":
            return None  # jatuh/tenggelam kecil — bukan momen, cukup konteks
        if not _cooled(limiter.last_combat_ts, now, cooldown):
            return None
        limiter.last_combat_ts = now
        if kind == "hurt":
            return (
                f"[MINECRAFT] Kamu diserang {ev.get('source')} — HP {ev.get('health')}. "
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
    if kind == "task_failed":
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

def _summarize_event(ev: dict) -> str:
    kind = ev.get("ev")
    if kind == "death":
        return "kamu MATI" + (f" dibunuh {ev['killer']}" if ev.get("killer") else "")
    if kind == "respawn":
        return "kamu respawn"
    if kind == "hurt":
        return f"diserang {ev.get('source')} (HP {ev.get('health')})"
    if kind == "low_health":
        return f"HP kritis ({ev.get('health')})"
    if kind == "hostile_near":
        return f"{ev.get('kind')} mendekat ({ev.get('distance')} blok)"
    if kind == "chat":
        return f"chat {ev.get('from')}: {str(ev.get('text', ''))[:60]}"
    if kind == "task_done":
        return f"selesai: {ev.get('task')} ({ev.get('detail', '')})".strip()
    if kind == "task_failed":
        return f"gagal {ev.get('task')}: {ev.get('reason')}"
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


def format_context(
    status: dict | None,
    events: list[tuple[float, dict]],
    ttl_sec: float,
    now: float,
    *,
    max_events: int = 6,
) -> str:
    """Status terakhir + kejadian segar (TTL) -> isi blok [DI MINECRAFT]."""
    lines: list[str] = []
    if status:
        pos = status.get("pos") or {}
        near_p = status.get("nearby_players") or []
        near_h = status.get("nearby_hostiles") or []
        who = ", ".join(
            f"{p.get('name')} ({p.get('distance')} blok)" for p in near_p[:3]
        )
        musuh = ", ".join(
            f"{h.get('kind')} ({h.get('distance')} blok)" for h in near_h[:3]
        )
        lines.append(
            f"Kondisimu: HP {status.get('health')}/20, lapar {status.get('food')}/20, "
            f"lagi {status.get('task')}, di {status.get('dim')} "
            f"({'malam' if status.get('is_night') else 'siang'}), "
            f"posisi ({pos.get('x')}, {pos.get('y')}, {pos.get('z')})."
        )
        lines.append(f"Pemain dekat: {who or 'tidak ada'}. Musuh dekat: {musuh or 'tidak ada'}.")
    fresh = [
        (ts, ev) for ts, ev in events
        if now - ts <= ttl_sec and ev.get("ev") != "status"
    ]
    for ts, ev in fresh[-max_events:]:
        ringkas = _summarize_event(ev)
        if ringkas:
            lines.append(f"- {int(now - ts)} dtk lalu: {ringkas}")
    return "\n".join(lines)


def status_note(status: dict | None) -> str:
    """Ringkas satu kalimat untuk bahan inisiatif ("lagi ngapain di game")."""
    if not status:
        return ""
    near_p = status.get("nearby_players") or []
    teman = f"dekat {near_p[0].get('name')}" if near_p else "lagi sendirian"
    mode = "jelajah sendiri" if status.get("task") == "roam" else str(status.get("task"))
    return (
        f"HP {status.get('health')}/20, {'malam' if status.get('is_night') else 'siang'}, "
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
                str(cfg.get("minecraft_bot_script", "mc-bot/bot.js")),
                "--host", str(cfg.get("minecraft_host", "127.0.0.1")),
                "--port", str(cfg.get("minecraft_port", 25565)),
                "--username", str(cfg.get("minecraft_bot_name", "Arti")),
                "--streamer", str(cfg.get("minecraft_streamer_name", "Bohan")),
                "--status-interval", str(cfg.get("minecraft_status_interval_sec", 10)),
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
                except (OSError, ValueError):
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

    def _drain_stderr(self, proc) -> None:
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    print(f"[MC-bot] {line}")
        except (OSError, ValueError):
            pass

    def _handle_line(self, line: str) -> None:
        ev = decode_event(line)
        if ev is None:
            return
        now = time.time()
        self._events.append((now, ev))
        kind = ev.get("ev")
        if kind == "status":
            self.last_status = ev
        elif kind == "chat":
            streamer = str(self._config.get("minecraft_streamer_name", ""))
            if ev.get("from") == streamer and callable(self._hooks.get("add_history")):
                # Chat in-game Bohan = aktivitas manusia betulan — source
                # "Streamer" sekalian membangunkan detektor kehidupan.
                self._hooks["add_history"](
                    "Streamer", f"(chat Minecraft) {ev.get('text', '')}"
                )
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
