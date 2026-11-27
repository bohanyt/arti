"""Mabar: the owner plays HOST on a brand-new farm and talks to ARTI; she joins as the farmhand and plays.

    python -B scripts/livepath/mabar.py <step> --run RUN [options]

Steps (CT runs them in order; each writes its own log in RUN, nothing is scored):
  backup   copy the --continue save, startup_preferences, the farmhand mod folder and the rig prep files, verify SHA-256
  prepare  prepare-run with the booked DLL, deploy it to the farmhand Mods, write the trial config (HOST = owner)
  preflight  print one OK/BLOCKER/WARN line per bring-up check; non-zero on a blocker (also runs at voice/rejoin)
  voice    run ARTI's real bridge (mic, VTS, Supertone) until `stop`; blocks, so CT runs it in the background
  host     launch the HOST game; the owner makes the farm (Co-op, Host, new farm, >= 1 cabin) and watches the intro
  join     after the intro: join the free cabin, ARTI creates her own farmer, she hears her own choice as a moment
  play     wait in-world, start the Live7 cycle and chain days until STOP (blocks)
  rejoin   re-arm and start a new cycle as the same farmhand (needs --name or self_facts)
           With both games up, verifies the live farmer. --owner-ready starts the owner-hosted first-launch handoff.
  supervise  background recovery: classify cycle/helper stops, bounded rejoin, host --relaunch at morning
  solo     one background bring-up from cold; READY_TO_GO_LIVE only after real play (see mabar_solo)
  chat     hand a late YouTube video id to the running bridge (run/youtube-video-id.txt; no restart)
  stop     STOP the cycle, ask the bridge to drain and stop
  restore  with the games closed: put the farmhand Mods DLL/config and prep files back (the new farm is kept)
  status   one §4e line: cycle, play, voice, chat, helper, headroom, last_fault, auto_recoveries

Flags (play/rejoin): --auto-rejoin (default OFF) waits for a joinable HOST after host_left and rejoins
  by itself; --auto-rejoin-tries (12) and --auto-rejoin-minutes (30) bound the wait. Never starts HOST
  or writes the HOST save. play writes host_owner=human into the cycle so a HOST quit is host_left;
  solo free-play stays harness-owned and still rejoins. Default without --auto-rejoin: the co-op leg
  still ends with that named reason.

Owner present is the point: no owner-idle check, OBS is allowed and only logged (mabar never broadcasts). Still
refused: SMAPI already running at prepare/host, a second ARTI bridge, C: below 15 GiB. Never touches stream-folder except
reading the GROQ key name's value from its .env into the bridge's own process env, and the mic index from its
live_session.json (non-secret). Never starts or stops the owner's bridge.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import re
import os
from pathlib import Path, PureWindowsPath
import shutil
import socket
import subprocess
import sys
import time
import math

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.free_play import soak  # noqa: E402
from scripts.livepath import rehearse  # noqa: E402
from scripts.m1_operator.farmhand_focus import release_farmhand_focus  # noqa: E402

ARTI = ROOT.parent
RIG = ARTI / "lab-rig"
PREP = RIG / "operator-staging"
STREAM = ARTI / "stream-folder"
GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Stardew Valley")
APPDATA = Path(os.environ.get("APPDATA", "")) / "StardewValley"
MOD = GAME / "Mods/ArtiStardewBridge"
MIND = ARTI / "arti-workspace" / "mind"
BACKUPS = ARTI / "arti-workspace" / "backups"
VTS_PORT = 8002
# {owner} = the live streamer_name. Without a name here she would use a generic title on every line; with it she
# calls them by name.
SESSION_FACTS = (
    "Nama teman mainmu {owner}; sapaan namanya {owner}.",
    "Ini farm baru buatan {owner}; kamu ikut sebagai pemain kedua (farmhand) di co-op.",
    "{owner} ngobrol sama kamu lewat mic.",
)


# --continue: a carried-on co-op farm (owner 2026-11-11). Facts come from the HOST save (read only) and earlier runs.
CONTINUE_FACTS = (
    "Nama teman mainmu {owner}; sapaan namanya {owner}.",
    "Ini lanjutan farm {farm} Farm milik {owner} (HOST); kamu pemain kedua (farmhand) di co-op dan karaktermu sudah ada di sana.",
    "Ini malam main bareng ke-{evening} di farm ini; terakhir disimpan di {season} hari {day} tahun {year}.",
)
SEASONS = ("spring", "summer", "fall", "winter")


def saved_farm(save_dir):
    """Farm name, saved date and HOST name from the save's SaveGameInfo (read only; the save is never written)."""
    path = Path(save_dir) / "SaveGameInfo"
    if not path.is_file():
        raise RuntimeError("no SaveGameInfo in the save folder: %s" % Path(save_dir).name)
    text = path.read_text(encoding="utf-8", errors="replace")

    def tag(name):
        m = re.search(r"<%s>([^<]*)</%s>" % (name, name), text)
        return m.group(1).strip() if m else ""
    season = tag("seasonForSaveGame")
    season = SEASONS[int(season)] if season.isdigit() and int(season) < 4 else (season or "?")
    return dict(save=Path(save_dir).name, farm=tag("farmName"), host=tag("name"), season=season,
                day=int(tag("dayOfMonthForSaveGame") or 0), year=int(tag("yearForSaveGame") or 0))


def saved_farmhands(save_dir):
    """The farmhand names in the save (read only). The HOST player is <player>; farmhands are <Farmer> entries."""
    path = Path(save_dir) / Path(save_dir).name
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return list(dict.fromkeys(n.strip() for n in re.findall(r"<Farmer>\s*<name>([^<]+)</name>", text) if n.strip()))


def evening_number(farm, runs_root=None, exclude=None):
    """1 + earlier mabar runs whose verified self facts name this farm."""
    runs_root = Path(runs_root or RIG / "mabar-runs")
    needle = "Farm ini bernama %s Farm." % farm
    seen = 0
    for run in sorted(p for p in runs_root.glob("*") if p.is_dir()):
        if exclude and run.resolve() == Path(exclude).resolve():
            continue
        facts = run / "self_facts.json"
        if facts.is_file() and needle in " ".join(soak.read(facts).get("lines") or ()):
            seen += 1
    return seen + 1


def farm_scope_hashes(save_name, inbox_dir):
    """sha256("<save_id>:<player_id>") for every farmhand ring the mod wrote for this save (arti_mind._scope_hash)."""
    save_id = str(save_name).rsplit("_", 1)[-1]
    hashes = set()
    for ring in Path(inbox_dir).glob("occurrences.%s.*.json" % save_id):
        parts = ring.name.split(".")
        if len(parts) == 4:
            hashes.add(hashlib.sha256(("%s:%s" % (parts[1], parts[2])).encode()).hexdigest())
    return hashes


def previous_recap(save_name, *, mind=None, inbox_dir=None, limit=200):
    """One plain line of what she verifiably did on this farm before: people met, things obtained, places entered,
    from her own discoveries rows for THIS save only (other farms in the shared mind are excluded)."""
    hashes = farm_scope_hashes(save_name, inbox_dir or inbox())
    path = Path(mind or MIND) / "stardew" / "discoveries.jsonl"
    if not hashes or not path.is_file():
        return ""
    met, got, went = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("scope_hash") not in hashes:
            continue
        kind, text = row.get("kind"), str(row.get("text") or "")
        if kind == "npc_met":
            name = text.split(":")[0].strip()
            met.append(name)
        elif kind == "item_obtained":
            got.append(text.split(":")[0].strip())
        elif kind == "location_entered":
            went.append(str(row.get("place_name") or text).strip())
    uniq = lambda xs: list(dict.fromkeys(x for x in xs if x))
    parts = []
    if uniq(met):
        parts.append("kenal " + ", ".join(uniq(met)[:5]))
    if uniq(got):
        parts.append("dapat " + ", ".join(uniq(got)[:5]))
    if uniq(went):
        parts.append("ke " + ", ".join(uniq(went)[:5]))
    if not parts:
        return ""
    return ("Yang tercatat dari malam sebelumnya di farm ini: " + "; ".join(parts) + ".")[:limit]


def continued_state(run, save_name, saves=None):
    farm = saved_farm(Path(saves or APPDATA / "Saves") / save_name)
    farm["evening"] = evening_number(farm["farm"], exclude=run)
    farm["recap"] = previous_recap(save_name)
    return farm


def farm_facts(continued):
    """Read farm-scoped owner data; diagnostics contain no fact text or farm identity."""
    save = continued.get("save")
    if not isinstance(save, str) or not save:
        print("[FarmFacts] missing continued save folder")
        return []
    try:
        data = json.loads((MIND / "stardew" / "farm_facts.json").read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print("[FarmFacts] missing data file")
        return []
    except (OSError, ValueError, UnicodeError):
        print("[FarmFacts] malformed or unreadable data file")
        return []
    try:
        if not isinstance(data, dict) or data.get("schema") != "arti.farm_facts/1" or not isinstance(data.get("facts"), list):
            raise ValueError("schema")
        ordered = []
        for row in data["facts"]:
            if not isinstance(row, dict) or not isinstance(row.get("farm"), str):
                raise ValueError("fact")
            stamp = date.fromisoformat(row["date"])
            text = row["text"]
            if not isinstance(text, str) or not text.strip() or len(text.splitlines()) != 1:
                raise ValueError("text")
            if row["farm"] == save:
                ordered.append((stamp, text))
    except (KeyError, TypeError, ValueError):
        print("[FarmFacts] malformed data file")
        return []
    if not ordered:
        print("[FarmFacts] no matching farm facts")
    return [text for _, text in sorted(ordered, key=lambda pair: pair[0])]


def session_facts(owner, continued=None):
    if not continued:
        return [f.format(owner=owner) for f in SESSION_FACTS]
    facts = [f.format(owner=owner, **continued) for f in CONTINUE_FACTS]
    return facts + farm_facts(continued) + ([continued["recap"]] if continued.get("recap") else [])


def utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(run, event, *, message=None, stream=None, **fields):
    if message is not None:
        fields["message"] = message
    row = dict(utc=utc(), event=event, **fields)
    with (Path(run) / "mabar.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(message if message is not None else "[mabar] " + json.dumps(row, ensure_ascii=False),
          file=stream, flush=True)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_manifest(folder):
    folder = Path(folder)
    return {str(p.relative_to(folder)).replace("\\", "/"): sha(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def processes():
    raw = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, timeout=20)
    return {line.split('","')[0].strip('"').lower() for line in raw.splitlines() if line}


def bridge_pids(*, timeout=None):
    """Any arti_bridge python, ours or the owner's (two voices would collide)."""
    options = {} if timeout is None else {"timeout": timeout}
    out = soak.ps("Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                  "Where-Object { $_.CommandLine -match 'arti_bridge' } | Select-Object -ExpandProperty ProcessId",
                  **options)
    return [int(x) for x in out.split() if x.isdigit()]


def free_gib():
    return shutil.disk_usage("C:\\").free / 1024**3


def vts_reachable(port=VTS_PORT, timeout=1.5):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def env_for_tools(run):
    """The operator tools' environment (same names soak.py sets)."""
    env = dict(os.environ, ARTI_M1_PREP=str(PREP), ARTI_M1_RUN=str(Path(run) / "runtime"), ARTI_M1_WORKTREE=str(ROOT),
               ARTI_CHECKOUT=str(ROOT), ARTI_VENV_PYTHON=sys.executable, ARTI_STARDEW_GAME=str(GAME),
               ARTI_M1_TOOLS=str(ROOT / "scripts/m1_operator"),
               ARTI_HOST_SMAPI_LOG=str(APPDATA / "ErrorLogs/SMAPI-latest.txt"),
               ARTI_FARMHAND_SMAPI_LOG=str(APPDATA / "ErrorLogs/SMAPI-latest.player-2.txt"),
               ARTI_MIND_DIR=str(MIND),
               ARTI_M1_DLL=str(Path(run) / "template/host-mods/ArtiStardewBridge/ArtiStardewBridge.dll"))
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def tool(run, script, args, name, *, deadline=None, background=False):
    log_path = Path(run) / f"{name}-{time.strftime('%H%M%S', time.gmtime())}.log"
    if deadline is None and not background:
        soak.call(script, args, log_path, env_for_tools(run))
        return log_path
    from scripts.livepath.halt_authority import RecoveryTimedOut
    argv = [sys.executable, "-B", str(ROOT / "scripts/m1_operator" / script), *map(str, args)]
    env = env_for_tools(run)
    with log_path.open("xb") as out:
        remaining = deadline - time.time() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise RecoveryTimedOut("recovery_deadline")
        if background:
            subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            result = subprocess.run(argv, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=remaining)
            if result.returncode:
                raise RuntimeError(f"{script} returned {result.returncode}; see {log_path}")
    return log_path


# ------------------------------------------------------------------ backup / prepare / restore

def _save_backup_key(name):
    if (not name or name in (".", "..") or PureWindowsPath(name).name != name
            or name.endswith((".", " ")) or PureWindowsPath(name).is_reserved()
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)):
        raise RuntimeError("expected a save folder name")
    return "Saves/" + name


def _backup_covers_save(backup_record, save_name):
    save_key = _save_backup_key(save_name)
    manifest = soak.read(Path(soak.read(backup_record)["path"]) / "MANIFEST.json")["sha256"]
    if not isinstance(manifest, dict):
        raise ValueError("invalid backup manifest")
    return manifest.get(save_key) is not None or f"{save_name}/SaveGameInfo" in (manifest.get("Saves") or {})


def _log_host_save_backup_gap(run, save_name):
    if not save_name:
        return  # A new farm may have no on-disk save yet.
    try:
        covered = _backup_covers_save(run / "backup.json", save_name)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        log(run, "host_save_backup_unverified", save=save_name)
    else:
        if not covered:
            log(run, "host_save_not_backed_up", save=save_name)


def backup(a):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = BACKUPS / f"mabar-{stamp}"
    sources = {"startup_preferences": APPDATA / "startup_preferences",
               "farmhand-mod": MOD, "prep-trial-config.json": PREP / "trial-config.json",
               "prep-approval.json": PREP / "approval.json"}
    save_name = getattr(a, "continue_save", None)
    save_key = _save_backup_key(save_name) if save_name else None
    if save_key:
        save = APPDATA / save_key
        if not save.is_dir() or save.is_symlink() or _is_junction(save):
            raise RuntimeError("selected save is not a regular save folder: " + save_name)
        sources[save_key] = save
    dest.mkdir(parents=True)
    manifest = {}
    for name, src in sources.items():
        if not src.exists():
            if name == save_key:
                raise RuntimeError("selected save disappeared during backup: " + save_name)
            manifest[name] = None
            continue
        if src.is_dir():
            shutil.copytree(src, dest / name)
            manifest[name] = tree_manifest(src)
            if tree_manifest(dest / name) != manifest[name]:
                raise RuntimeError("backup copy differs: " + name)
        else:
            shutil.copy2(src, dest / name)
            manifest[name] = sha(src)
            if sha(dest / name) != manifest[name]:
                raise RuntimeError("backup copy differs: " + name)
    soak.write(dest / "MANIFEST.json", dict(utc=utc(), sources={k: str(v) for k, v in sources.items()}, sha256=manifest))
    Path(a.run).mkdir(parents=True, exist_ok=True)
    soak.write(Path(a.run) / "backup.json", dict(path=str(dest), utc=utc()))
    log(a.run, "backup_verified", path=str(dest), saves=len(manifest.get(save_key) or {}))


# --- her live self (2026-11-10 audit, an internal design note) --------------------------------
# A rehearsal bridge never loads the owner's private config, so mabar fell back to code defaults: no vault recall, no
# owner name/handles, no origin facts, no VTS expressions/idle motion, not the live reply routing. These allowlisted
# keys are copied from the live private config at voice start (values never printed). Peripherals that write live
# data (observer, scouter, vault summaries) stay off. Donation/mediashare flags and watch-along
# timings are copied so the classic alerts route runs; provider secrets are never overlay keys (process env only).
LIVE_CONFIG = STREAM / "config_local.json"
LIVE_OVERLAY_KEYS = (
    "streamer_name", "owner_yt_handles", "yt_default_viewer", "arti_debut_date", "arti_debut_label",
    "arti_archive_from",
    "vts_model_dir", "expression_emosi_fade_sec", "expression_emosi_linger_sec", "idle_motion_hotkeys",
    "idle_motion_sambung", "idle_motion_lanjut_saat_bicara",
    "codex_agent_enabled", "codex_model", "codex_effort", "codex_service_tier", "codex_timeout_sec",
    "codex_thread_max_turns", "codex_primary_voice", "codex_role_model", "codex_role_effort",
    "agy_agent_enabled", "agy_model", "agy_primary_voice", "agy_thread_max_turns",
    "cursor_agent_enabled", "cursor_scratch_dir", "cursor_trigger_types",
    "hotkey_key", "asr_pasif_silence_tail_sec", "asr_ptt_adaptif_enabled",
    "tts_jeda_antar_kalimat_sec", "speech_censor_enabled", "arti_reply_inisiatif_max_sentences",
    "initiative_enabled", "initiative_backoff_base_sec", "voice_queue_enabled", "renungan_enabled",
    "lmstudio_embedding_base_url", "lmstudio_embedding_fallback_base_url", "lmstudio_embedding_model",
    "lmstudio_embedding_query_prefix",
    "lmstudio_embedding_doc_prefix", "vault_rag_rerank_enabled", "vault_rag_rerank_base_url",
    "vault_rag_rerank_timeout_sec", "vault_rag_rerank_breaker_sec",
    "donation_enabled", "video_enabled", "video_max_duration_sec", "video_qa_window_sec",
    "video_rate_limit_sec", "video_queue_max", "video_gemini_model", "video_gemini_timeout_sec",
    "video_gemini_total_timeout_sec",
    "mediashare_hold_sec", "donation_alert_base_sec", "donation_alert_per_char_sec",
    "donation_alert_max_sec", "alerts_yield_after_sentence", "alerts_yield_max_sec",
)
# Never copied into the run-dir rehearsal config; injected into the bridge process env only.
ALERT_PROVIDER_SECRETS = (
    ("SAWERIA_STREAM_KEY", "saweria_stream_key"),
    ("STREAMLABS_SOCKET_TOKEN", "streamlabs_socket_token"),
    ("TRAKTEER_HASH", "trakteer_hash"),
    ("TRAKTEER_STREAM_KEY", "trakteer_stream_key"),
)
ALERT_SECRET_CONFIG_KEYS = frozenset(cfg_key for _, cfg_key in ALERT_PROVIDER_SECRETS)
# The mediashare watcher's Gemini key, under its own env name: putting it in the bridge env as
# GEMINI_API_KEY would also arm the gemini/gemini_live reply providers, which mabar does not use.
WATCHER_SECRET = ("GEMINI_API_KEY", "gemini_api_key", "ARTI_VIDEO_GEMINI_API_KEY")
# One index for every mabar run (incremental: unchanged notes are hash-skipped), outside the live folder.
VAULT_INDEX = ARTI / "arti-workspace" / "vault-index" / "vault_rag.db"
VAULT_ROOT = None  # None = the live stream-folder folder (STREAM); a rehearsal scratch points it elsewhere
REHEARSAL_SCRIPTED_PTT = False  # set only by --rehearsal-scratch (apply_rehearsal_scratch)
# Her viewer memory and carried-over mood: the bridge reads them next to its code; prepare copies the live files in.
LIVE_SELF_FILES = ("ARTI_VIEWERS.md", "ARTI_MOOD_STATE.json")


def live_overlay(path=None):
    """The allowlisted keys from the owner's live private config. Missing file = refuse (she would not be herself)."""
    path = Path(path or LIVE_CONFIG)
    if not path.is_file():
        raise RuntimeError("live private config not found; mabar would run without her live settings")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return {k: data[k] for k in LIVE_OVERLAY_KEYS if k in data}


def vault_config(overlay):
    """Vault recall for mabar: notes READ from the live stream-folder folder, db in the shared mabar index."""
    cfg = {k: v for k, v in overlay.items() if k.startswith(("lmstudio_", "vault_rag_rerank"))}
    cfg.update(vault_rag_enabled=True, vault_rag_root=str(VAULT_ROOT or STREAM), vault_rag_db_path=str(VAULT_INDEX))
    return cfg


def build_vault_index(run, overlay, *, reindex=None, clock=time.monotonic):
    """Index the live vault into the mabar db through the local LM Studio endpoint. Soft failure: she plays without
    recall, `vault_index.json` and her status line say why, the operator tells the owner."""
    if reindex is None:
        import arti_vault_rag
        reindex = arti_vault_rag.reindex_all
    cfg = vault_config(overlay)
    started = clock()
    try:
        stats = reindex(cfg, force=False, verbose=False)
        embedded = None
        try:
            import arti_vault_rag
            embedded = arti_vault_rag.index_stats(cfg).get("embedded")
        except Exception:
            pass
        ok = bool(stats.get("files")) and embedded != 0
        why = "" if ok else "index kosong"
        result = dict(ok=ok, why=why, files=stats.get("files", 0), chunks_new=stats.get("chunks_new", 0),
                      chunks_skipped=stats.get("chunks_skipped", 0), chunks_removed=stats.get("chunks_removed", 0),
                      embedded=embedded)
    except Exception as exc:
        reason = "LM Studio tidak menjawab" if "onnect" in type(exc).__name__ + str(exc)[:200] else type(exc).__name__
        result = dict(ok=False, why=reason)
    result.update(seconds=round(clock() - started, 1), utc=utc())
    soak.write(Path(run) / "vault_index.json", result)
    log(run, "vault_index", **{k: v for k, v in result.items() if k != "utc"})
    if not result["ok"]:
        print("!!! ingatan vault: mati (sebab %s); dia tetap main tanpa ingatan lama. Kabari owner. !!!" % result["why"])
    return result


def copy_live_self_files(run, dest=ROOT, src=STREAM):
    """Fresh copies of her viewer memory and mood next to the bridge; her writes go to these copies only."""
    copied = []
    for name in LIVE_SELF_FILES:
        if (Path(src) / name).is_file():
            shutil.copy2(Path(src) / name, Path(dest) / name)
            copied.append(name)
    log(run, "live_self_files", copied=copied)
    return copied


def vault_status(run):
    """'' when recall is up, else the plain reason from the last prepare."""
    path = Path(run) / "vault_index.json"
    if not path.is_file():
        return "index belum dibuat"
    data = soak.read(path)
    return "" if data.get("ok") else str(data.get("why") or "tidak diketahui")


def prepare(a):
    if getattr(a, "relay", False) and getattr(a, "step", "prepare") != "solo" and not getattr(a, "helper", False):
        raise RuntimeError("--relay is not allowed for a run a person hosts (solo relay only)")
    run = Path(a.run)
    backup_record = run / "backup.json"
    if not backup_record.is_file():
        raise RuntimeError("run `backup` first")
    if "stardewmoddingapi.exe" in processes():
        raise RuntimeError("SMAPI is running; prepare only with the games closed")
    if free_gib() < 15:
        raise RuntimeError("C: below 15 GiB")
    save_name = getattr(a, "continue_save", None)
    if save_name and not _backup_covers_save(backup_record, save_name):
        raise RuntimeError("run `backup --continue SAVE_FOLDER` for the selected save first")
    catch_up_notes(run)
    continued = None
    if save_name:
        continued = continued_state(run, save_name)
        if not getattr(a, "solo_worker", False):
            from scripts.livepath.owner_join import prepared_cabins
            continued.update(prepared_cabins(APPDATA / "Saves" / save_name))
        soak.write(run / "continued.json", continued)
        log(run, "continued", farm=continued["farm"], evening=continued["evening"],
            saved="%s %s Y%s" % (continued["season"], continued["day"], continued["year"]))
    booking = rehearse.read_booking(a.spec)
    dll, helper_sha = Path(booking["dll"]), booking["helperSha256"]
    if sha(dll) != booking["dllSha256"]:
        raise RuntimeError("DLL does not match the booking")
    provenance = soak.read(booking["provenance"])
    if provenance.get("dll_sha256") != booking["dllSha256"] or provenance.get("helper_sha256") != helper_sha:
        raise RuntimeError("DLL provenance mismatch")
    deps = soak.candidate_dependencies(dll, provenance)
    before = dict(dll=sha(MOD / "ArtiStardewBridge.dll"), config=sha(MOD / "config.json"),
                  deps={n: sha(MOD / n) if (MOD / n).is_file() else None for n in deps})
    soak.write(run / "deploy-before.json", before)
    shutil.copy2(dll, MOD / "ArtiStardewBridge.dll")
    for name, src in deps.items():
        shutil.copy2(src, MOD / name)
    mod_config = soak.read(MOD / "config.json")
    mod_config.update(LIVE_MOD_FLAGS)
    soak.write(MOD / "config.json", mod_config)
    template = run / "template"
    shutil.copytree(booking["hostTemplate"], template)
    host_mod = template / "host-mods/ArtiStardewBridge"
    shutil.copy2(dll, host_mod / "ArtiStardewBridge.dll")
    for name, src in deps.items():
        shutil.copy2(src, host_mod / name)
    if a.relay:
        # The HOST mod relays what the HOST watches (flag off by default; an older DLL ignores unknown keys).
        host_config = soak.read(host_mod / "config.json") if (host_mod / "config.json").is_file() else {}
        host_config.update(HostStoryRelayEnabled=True, HostStoryRelayDirectory=str(run))
        soak.write(host_mod / "config.json", host_config)
    trial = dict(soak.read(booking["trialConfig"]), stardew_host_controller="owner",
                 arti_mind_dir=str(MIND), arti_state_dir=str(run / "arti-state"),
                 live7_world_origin="continued_farm" if continued else "new_farm",
                 live7_moments_enabled=True, live7_activity_events_enabled=False)
    soak.write(PREP / "trial-config.json", trial)
    soak.write(run / "trial-config.json", trial)
    MIND.mkdir(parents=True, exist_ok=True)
    tool(run, "witness_run.py", ["prepare-run", "--run-dir", run / "runtime", "--template", template,
                                 "--sha256", booking["dllSha256"], "--row", "mabar", "--snapshot", "unused-no-restore"],
         "prepare-run")
    soak.staged_mod_preflight(((MOD, "ArtiStardewBridge.dll", booking["dllSha256"]),
                               (run / "runtime/batch-host/host-mods/ArtiStardewBridge", "ArtiStardewBridge.dll",
                                booking["dllSha256"])))
    # The run is the record: the usual Live7 journals, marked so no evaluator counts it toward a gate.
    soak.write(run / "MODE.json", dict(mode="mabar", gate_scored=False, owner_present=True, utc=utc(),
                                       owner_reactions="owner_reactions.jsonl"))
    log(run, "prepared", dll=booking["dllSha256"], relay=bool(a.relay), obs="obs64.exe" in processes())
    overlay = live_overlay()
    snapshot_checkout_state(run)  # V9b: before her self files are copied in, so those count as the run's
    copy_live_self_files(run)
    index = build_vault_index(run, overlay)
    print("[mabar] ingatan vault: %s; %s file, +%s chunk baru, %s sama, %.1f s" % (
        "hidup" if index["ok"] else "MATI (" + index["why"] + ")", index.get("files", 0), index.get("chunks_new", 0),
        index.get("chunks_skipped", 0), index["seconds"]))


def mirror_from_backup(src, mod, prep, startup, aside):
    """Put the farmhand mod folder, the prep files and startup_preferences back exactly as the backup recorded them.

    Files mabar added are moved to ``aside`` (never deleted); a prep file absent before mabar is moved aside too.
    Returns the verify report; saves are never touched (a new farm stays)."""
    manifest = json.loads((src / "MANIFEST.json").read_text(encoding="utf-8"))["sha256"]
    aside.mkdir(parents=True, exist_ok=True)
    want, moved, copied = manifest["farmhand-mod"] or {}, [], []
    for f in sorted(p for p in mod.rglob("*") if p.is_file()):
        rel = str(f.relative_to(mod)).replace("\\", "/")
        if rel not in want:
            dst = aside / "farmhand-mod" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), dst)
            moved.append(rel)
    for rel, digest in want.items():
        target = mod / rel
        if not target.is_file() or sha(target) != digest:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / "farmhand-mod" / rel, target)
            copied.append(rel)
    for key, target in (("prep-trial-config.json", prep / "trial-config.json"), ("prep-approval.json", prep / "approval.json")):
        if manifest.get(key) is None:
            if target.exists():
                shutil.move(str(target), aside / key)
        elif not target.is_file() or sha(target) != manifest[key]:
            shutil.copy2(src / key, target)
    if manifest.get("startup_preferences") and sha(startup) != manifest["startup_preferences"]:
        shutil.copy2(startup, aside / "startup_preferences.mabar")
        shutil.copy2(src / "startup_preferences", startup)
    bad = [rel for rel, digest in want.items() if sha(mod / rel) != digest]
    extra = [str(f.relative_to(mod)) for f in mod.rglob("*") if f.is_file()
             and str(f.relative_to(mod)).replace("\\", "/") not in want]
    return dict(moved_aside=moved, copied_back=copied, mismatch=bad, extra=extra,
                startup_ok=not manifest.get("startup_preferences") or sha(startup) == manifest["startup_preferences"])


def restore(a):
    run = Path(a.run)
    if "stardewmoddingapi.exe" in processes():
        raise RuntimeError("close both games before restore")
    # The night's FIRST backup is the pre-mabar state; a later run's own backup may already hold a mabar DLL.
    src = Path(a.from_backup) if a.from_backup else Path(soak.read(run / "backup.json")["path"])
    report = mirror_from_backup(src, MOD, PREP, APPDATA / "startup_preferences",
                                BACKUPS / (src.name + "-restore-aside-" + time.strftime("%H%M%S", time.gmtime())))
    if report["mismatch"] or report["extra"] or not report["startup_ok"]:
        raise RuntimeError("restore did not verify: " + json.dumps(report)[:400])
    log(run, "restored", backup=str(src), dll=sha(MOD / "ArtiStardewBridge.dll"), kept="new farm saves stay",
        moved_aside=len(report["moved_aside"]), copied_back=len(report["copied_back"]))
    move_checkout_state(run)
    unlink_runtime(a)


# ------------------------------------------------------------------ checkout state (V9b, 2026-11-18)
def snapshot_checkout_state(run, root=None):
    """At prepare: the private state already in the checkout, {path: sha256}, so restore can tell what the run
    wrote."""
    from scripts.livepath import bringup
    soak.write(Path(run) / "checkout-state-before.json", bringup.private_files(root or ROOT))


def move_checkout_state(run, root=None, live=None, token_store=None):
    """At restore: move the private state the run wrote (new or changed since prepare) out of the code checkout into
    <run>/checkout-state/, verified by hash; nothing is removed before its copy matches. Her VTS token goes to the
    token store outside every checkout instead (link-runtime puts it back). New vault session notes, and self files
    that differ from the live copies, are logged as what must be merged into her real stores (not merged here)."""
    from scripts.livepath import bringup
    import hashlib
    run, root = Path(run), Path(root or ROOT)
    live, token_store = Path(live or STREAM), Path(token_store or RUNTIME_LINKS)
    before_path = run / "checkout-state-before.json"
    before = soak.read(before_path) if before_path.is_file() else None
    now = bringup.private_files(root)
    moved, tokens, merge = [], [], []
    for rel, digest in sorted(now.items()):
        if before is not None and before.get(rel) == digest:
            continue  # there before the run and unchanged: not this run's
        src = root / rel
        is_token = "/" not in rel and any(Path(rel).match(g) for g in bringup.PRIVATE_GLOBS)
        dest = (token_store / rel) if is_token else (run / "checkout-state" / rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        if hashlib.sha256(dest.read_bytes()).hexdigest() != digest:
            raise RuntimeError("checkout state copy did not verify: %s" % rel)
        src.unlink()
        (tokens if is_token else moved).append(rel)
        if rel.startswith("vault/sessions/") and rel.endswith(".md") and not rel.endswith("/index.md"):
            merge.append(rel)
        elif rel in LIVE_SELF_FILES and (not (live / rel).is_file()
                                         or hashlib.sha256((live / rel).read_bytes()).hexdigest() != digest):
            merge.append(rel)
    for rel in bringup.PRIVATE_STATE:  # folders the move left empty
        p = root / rel
        if p.is_dir():
            for d in sorted((d for d in p.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)):
                if not any(d.iterdir()):
                    d.rmdir()
            if not any(p.iterdir()):
                p.rmdir()
    if moved:
        soak.write(run / "checkout-state" / "SHA256SUMS.json", {rel: now[rel] for rel in moved})
    log(run, "checkout_state_moved", moved=moved, tokens=tokens, to_merge=merge, manifest=before is not None)
    if merge:
        print("Merge into her real stores (not done here): %s (in %s)" % (", ".join(merge), run / "checkout-state"))
    return dict(moved=moved, tokens=tokens, to_merge=merge)


# ------------------------------------------------------------------ voice

def stream_env_value(name):
    """The value only; never printed, logged or written. Missing file/key = None."""
    path = STREAM / ".env"
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def secrets_env_value(name):
    """The secrets .env (ARTI_SECRETS_ENV; default: .env in this checkout). The value only; never printed or written."""
    path = Path(os.environ.get("ARTI_SECRETS_ENV") or ROOT / ".env")
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def groq_key():
    """The value only, into our own process env; never printed, logged or written."""
    return stream_env_value("GROQ_API_KEY")


def live_config_value(cfg_key):
    """A single live private-config value; never logged. Missing/non-string = None."""
    try:
        data = json.loads(Path(LIVE_CONFIG).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    val = data.get(cfg_key)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def provider_secret(env_name, cfg_key):
    """Saweria/Streamlabs/Trakteer secret; never logged.

    The secrets .env first (ARTI_SECRETS_ENV), then the
    live stream-folder .env, then the live config.
    """
    return secrets_env_value(env_name) or stream_env_value(env_name) or live_config_value(cfg_key)


def inject_alert_provider_env(env, *, enabled=True):
    """Copy provider secrets into the bridge process env. Returns present/absent/skipped only."""
    status = {}
    for env_name, cfg_key in ALERT_PROVIDER_SECRETS:
        if not enabled:
            status[env_name] = "skipped"
            continue
        val = provider_secret(env_name, cfg_key)
        if val:
            env[env_name] = val
            status[env_name] = "present"
        else:
            status[env_name] = "absent"
    src_env, src_cfg, dst_env = WATCHER_SECRET
    if not enabled:
        status[dst_env] = "skipped"
    else:
        val = secrets_env_value(src_env) or provider_secret(src_env, src_cfg)
        if val:
            env[dst_env] = val
        status[dst_env] = "present" if val else "absent"
    return status


def mic_index():
    try:
        value = json.loads((STREAM / "live_session.json").read_text(encoding="utf-8-sig")).get("asr_input_device")
        return value if type(value) is int else None
    except (OSError, ValueError):
        return None


# KSJACK form factors (PKEY_AudioEndpoint_FormFactor): 3 = Headphones, 5 = Headset.
EARPIECE_FORM_FACTORS = frozenset({3, 5})


def audio_route():
    """Where CABLE Output's "Listen to this device" really plays (read-only; scripts/livepath/audio_route.ps1)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                              str(ROOT / "scripts/livepath/audio_route.ps1")], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as exc:
        return dict(error=type(exc).__name__)


def passive_allowed(route):
    """Passive mic replies only when she cannot reach the mic through the air: Listen off, or Listen into an earpiece."""
    if not route or route.get("error") or not route.get("cable_found"):
        return False, "route unreadable"
    if not route.get("listen_on"):
        return True, "CABLE Output Listen is off"
    if route.get("form_factor") in EARPIECE_FORM_FACTORS:
        return True, "Listen plays into %s (%s, form factor %s)" % (route.get("name"), route.get("iface"), route.get("form_factor"))
    return False, "Listen plays into %s (%s, form factor %s): not an earpiece, her voice would reach the mic" % (
        route.get("name"), route.get("iface"), route.get("form_factor"))


def play_voice_trial_config(trial, voice_config):
    """The two processes share opt-ins; a classic trial remains byte-for-byte unchanged."""
    result = dict(trial)
    for key in ("live7_reactive_play_enabled", "stardew_initiative_game_first"):
        if voice_config.get(key) or key in result:
            result[key] = bool(voice_config.get(key, False))
    return result


def mabar_bridge_config(run, *, vts=True, mic=None, passive=False, vts_port=VTS_PORT, youtube_video_id=None,
                        ssn=False, live=None, vault_why=None, continued=None, classic=False, desk=False,
                        alerts=True, alert_yield=False, api_voice=None, own_play=False,
                        voice_cap_usd=0.50):
    run = Path(run)
    cfg = rehearse.bridge_config(run, run / "runtime")
    # Her live self first (allowlist above); the mabar rules below win where they overlap.
    cfg.update(live or {})
    facts = session_facts((live or {}).get("streamer_name") or "Bohan", continued)
    farm_count = len(facts) - len(CONTINUE_FACTS) - bool(continued.get("recap")) if continued else 0
    cfg.update(expression_nod_enabled=True, reflex_enabled=True, provider_probe_enabled=True)
    cfg.update(vault_config(live or {}), vault_rag_live_enabled=True, vault_rag_reindex_on_startup=False,
               vault_rag_reindex_on_shutdown=False)
    cfg.update(
        livepath_mode="mabar", arti_mind_dir=str(MIND),
        # Owner speech = PTT. Passive mic replies only with --passive: when her voice plays on speakers the open
        # mic hears it and she answers herself. Use it only when her voice is on headphones.
        asr_enabled=True, asr_input_device=mic, asr_pasif_reply_enabled=bool(passive),
        vts_enabled=bool(vts), vts_api_port=int(vts_port), vts_expected_model_name="MyModel",
        subtitle_enabled=True, subtitle_port=10002,
        live7_help_enabled=True, live7_moments_enabled=True, live7_status_facts_enabled=True,
        live7_activity_events_enabled=False,  # owner: activity events stay off live until heard
        live7_own_play_enabled=False, live7_moments_min_gap_sec=30.0,
        voice_queue_enabled=True, voice_queue_preserve_humans=False,
        attention_desk_enabled=False,  # stream default: 11-10 lane; --desk for closed rehearsals only
        # Reserve extra slots for every data-backed farm fact; the recap remains last.
        live7_session_facts=(lambda facts: facts[:3] + (
            ["Ingatan vault-mu (catatan sesi lama) lagi mati: %s. Jangan ngarang soal sesi lama; bilang belum ingat."
             % vault_why] if vault_why else []) + facts[3:])(
            facts),
        live7_session_facts_limit=4 + farm_count,
        live7_self_facts_file=str(run / "self_facts.json"),  # her verified look, written after creation
        host_story_relay_file=str(run / "host_story.jsonl"),
        stream_session_id=run.name, rehearsal_parent_pid=os.getpid(),
        vision_enabled=True, vision_runtime_on_start=False, vision_background_poll=False,
        # The voice model can request the isolated lookup role. Its own client has no search.
        codex_agent_enabled=True, codex_web_search="disabled", web_lookup_enabled=True,
        web_lookup_codex_enabled=True, web_lookup_model_decides=True,
        web_lookup_provider_chain=["codex"], web_lookup_regex_enabled=False,
        web_lookup_announce=True, host_web_topic_enabled=False,
        # Viewer chat (YouTube Live, innertube; the 10-24 reader) only when the owner gives the live video id.
        youtube_chat_enabled=bool(youtube_video_id), youtube_video_id=youtube_video_id or "",
        # Or YouTube + TikTok + Instagram through Social Stream Ninja (one door); never both (bridge.load_config).
        # ssn = "app" | "file": where the BRIDGE reads the session id; this config never holds it.
        ssn_chat_enabled=bool(ssn), ssn_session_source=ssn or "app", ssn_status_file=str(run / "ssn_status.json"),
        # Never drop chat silently: chat waits through her speech and the owner's talk, in the live queue (2 deep,
        # 60 s); a deeper queue answered stale chat.
        # Owner speech comes first, then paid events and viewer chat before game/story moments.
        voice_queue_priority_overrides={},
        # On a solo stream a viewer can wait long per message: a warm Codex thread grows each turn until it
        # times out, and an own-topic lane can talk about unrelated things while chat waits.
        # V13: that resend design is gone; codex_thread_max_turns now
        # governs only the single-string fallback.
        yt_viewer_cooldown_sec=15.0, codex_thread_max_turns=6, curious_interval_sec=180,
        chat_quiet_answer_all=True, chat_busy_messages=8, chat_busy_window_sec=180.0,
        chat_merge_window_sec=30.0, media_watch_mode=True, media_reaction_rank=0.25, media_summary_wait_sec=2.0, media_summary_timeout_sec=15.0,
        video_watcher_v2=True, video_gemini_enabled=True, video_keep_media=False,
        video_media_max_mb=200, video_vision_cap_usd=0.10,
        video_media_dir=str(run / "watcher-media"),
        video_vision_session_state_file=str(run / "watcher-vision-usd.json"),
        live_play_rules_enabled=True, passive_mic_hygiene=True, mic_priority_mark=True,
        # V5, V8: a superseded chat joins its viewer's newer waiting chat, a second passive goodbye in a row is not
        # spoken, and an owner line to a viewer says who it was for.
        live_voice_fixes_v1=True,
    )
    # --classic is a no-op: the stream default is already the 11-10 lane (desk off).
    if desk:
        cfg.update(attention_desk_enabled=True, live7_own_play_enabled=True, voice_queue_preserve_humans=True)
        types = list(cfg.get("cursor_trigger_types") or [])
        for kind in ("cut_in", "focus", "scene_ended"):
            if kind not in types:
                types.append(kind)
        cfg["cursor_trigger_types"] = types
    elif classic:
        pass
    if alert_yield:  # alerts wait for the end of her current sentence
        cfg["alerts_yield_after_sentence"] = True
    if own_play:  # own-play lines without the attention desk (cut-ins stay impossible)
        cfg.update(live7_own_play_enabled=True, own_play_min_gap_sec=25)
    if cfg.get("live7_own_play_enabled"):
        cfg.update(live7_reactive_play_enabled=True, stardew_initiative_game_first=True,
                   stardew_initiative_material_enabled=True,
                   live7_play_line_gap_sec=45, stardew_own_topic_gap_sec=180)
        # own_play was missing here, so every own-play turn fell through to the default provider chain, whose
        # request was larger than that provider's tokens-per-minute limit. Her own-play lines are spoken by her
        # voice like every other kind.
        types = list(cfg.get("cursor_trigger_types") or ["yt_chat"])
        if "own_play" not in types:
            types.append("own_play")
        cfg["cursor_trigger_types"] = types
    # The rehearsal parity and the live overlay set codex_effort "high" for every voice turn, and turns with long
    # reasoning were much slower. The bridge's own ladder: "low" by default, "high" only for the heavy reply classes
    # (deep, rant).
    cfg.update(codex_effort="low", codex_effort_berat="high", codex_effort_kelas_berat=["deep", "rant"])
    if api_voice:  # OpenAI Responses first; adapter falls back to Codex/Cursor on None
        from arti_runtime.openai_decisions import DEFAULT_ENV_FILE
        cfg.update(
            voice_api_enabled=True, voice_api_model=str(api_voice),
            voice_api_effort="low", voice_api_service_tier="default",
            voice_api_session_cap_usd=float(voice_cap_usd),
            voice_api_session_state_file=str(run / "voice-api-session.json"),
            voice_api_env_file=DEFAULT_ENV_FILE,  # path only; the bridge reads OPENAI_API_KEY in-process
        )
    cfg.update(LIVE_BRIDGE)  # the live profile (also for --rehearsal-scratch)
    from arti_runtime import vault_farm
    farm = run_farm_name(run, continued) if cfg.get(vault_farm.SETTING) else None
    if farm:  # ADR 238: the farm her session note is tagged with; a new farm's name comes later from her self facts
        cfg["session_farm_name"] = farm
    if REHEARSAL_SCRIPTED_PTT:  # --rehearsal-scratch: no audience; her session note goes to the scratch vault
        cfg.update(rehearsal_scripted_ptt=True, youtube_chat_enabled=False, ssn_chat_enabled=False,
                   vault_write_root=str(VAULT_ROOT))
    # Provider secrets never belong in the run-dir rehearsal config (process env only).
    for secret_key in ALERT_SECRET_CONFIG_KEYS:
        cfg.pop(secret_key, None)
    cfg.pop("OPENAI_API_KEY", None)
    cfg.pop("openai_api_key", None)
    if not alerts:
        cfg.update(donation_enabled=False, video_enabled=False)
    return cfg


def ssn_source(a):
    """Where the bridge reads the SSN session: "app" (the owner's SSN app here), "file" (a secret-card file), or None.
    This process never reads the id (CT 2026-11-10); the bridge does, and reports to ssn_status.json."""
    if getattr(a, "ssn_session_file", None) and getattr(a, "ssn_from_app", False):
        raise RuntimeError("use --ssn-from-app or --ssn-session-file, not both")
    source = "file" if getattr(a, "ssn_session_file", None) else ("app" if getattr(a, "ssn_from_app", False) else None)
    if source and a.youtube_video_id:
        raise RuntimeError("SSN already carries YouTube chat: use SSN or --youtube-video-id, not both")
    if source == "file" and not Path(a.ssn_session_file).is_file():
        raise RuntimeError("the SSN session file does not exist")
    return source


def _preflight_embed():
    """Probe the overlay's embedding hosts. Overlay missing = code defaults. No values printed."""
    import arti_vault_rag
    overlay = {}
    try:
        if LIVE_CONFIG.is_file():
            overlay = live_overlay()
    except (RuntimeError, OSError, ValueError):
        overlay = {}
    return arti_vault_rag.probe_embedding_endpoints(vault_config(overlay))


def _preflight_watcher_key():
    return os.environ.get("ARTI_VIDEO_GEMINI_API_KEY") or secrets_env_value("GEMINI_API_KEY") or provider_secret(
        "GEMINI_API_KEY", "gemini_api_key")


def _preflight_alert_keys():
    rows = []
    for env_name, cfg_key in ALERT_PROVIDER_SECRETS:
        val = provider_secret(env_name, cfg_key)
        rows.append((env_name, len(val) if val else 0))
    return rows


def _preflight_dll(a):
    try:
        booking = rehearse.read_booking(a.spec)
        expected = booking["dllSha256"]
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        return False, "booking spec unread (%s); need dllSha256" % type(exc).__name__
    path = MOD / "ArtiStardewBridge.dll"
    if not path.is_file():
        return False, "installed farmhand DLL missing; prepare the booked package"
    got = sha(path)
    if got != expected:
        return False, "installed %s != booking %s" % (got[:12], expected[:12])
    return True, "installed hash matches booking"


def _preflight_game_dir(a):
    run = getattr(a, "run", None)
    if run is not None:
        trial = Path(run) / "trial-config.json"
        if trial.is_file():
            try:
                configured = soak.read(trial).get("game_dir")
                if isinstance(configured, str) and configured.strip():
                    return Path(configured)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    return GAME


def preflight(a, *, skip_memory=False, launch=None, skip_disk_active=False):
    """Print one line per bring-up check; return 1 on BLOCKER so voice/rejoin never claim ready."""
    from scripts.livepath import bringup
    for line in live_profile_lines():
        print("OK " + line)
    for line in live_run_sheet_lines():
        print(line)
    return bringup.run_preflight(
        a, root=ROOT,
        vts_fn=lambda: vts_reachable(getattr(a, "vts_port", VTS_PORT)),
        embed_fn=_preflight_embed,
        watcher_fn=_preflight_watcher_key,
        alert_fn=_preflight_alert_keys,
        dll_fn=lambda: _preflight_dll(a),
        skip_memory=skip_memory,
        skip_disk_active=skip_disk_active,
        launch=launch or getattr(a, "launch", None),
        runtime_fn=lambda: bringup.runtime_missing(ROOT),
        vts_window1_fn=lambda: vts_reachable(8001),
        private_fn=lambda: [p for p in bringup.private_state_in(ROOT) if p not in LIVE_SELF_FILES],
        gpu_fn=lambda: bringup.probe_gpu(_preflight_game_dir(a)),
        disk_active_fn=bringup.probe_disk_active,
        ears_fn=lambda: bringup.probe_ears(ROOT / "venv"),
        save_date_fn=lambda: bringup.probe_save_date(getattr(a, "run", None)),
    )


# ------------------------------------------------------------------ release runtime links (livestream test 2026-11-17)
# A package checkout has no venv, venv312 or soul file; the bridge resolves all three next to its own code. These
# steps link them from the live checkout and remove exactly what they made (a manifest outside the tree).
RUNTIME_SOURCE = Path(os.environ.get("ARTI_RUNTIME_SOURCE") or ROOT)
RUNTIME_LINKS = ARTI / "arti-workspace" / "runtime-links"


def _is_junction(path):
    try:
        attrs = os.lstat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & 0x400) and Path(path).is_dir()  # FILE_ATTRIBUTE_REPARSE_POINT on a directory


def link_runtime(a, *, root=None, source=None, manifest_dir=None, mklink=None):
    """Link venv / venv312 (junctions) and ARTI_SOUL.md (hard link) into this checkout when missing."""
    root, source = Path(root or ROOT), Path(source or getattr(a, "runtime_from", None) or RUNTIME_SOURCE)
    manifest = Path(manifest_dir or RUNTIME_LINKS) / (root.name + ".json")
    made = soak.read(manifest).get("made", []) if manifest.is_file() else []
    mklink = mklink or (lambda link, target: subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True))
    for token in sorted(Path(manifest_dir or RUNTIME_LINKS).glob("vts_token_*.txt")):  # V9b: her VTS token, back
        if not (root / token.name).exists():
            shutil.copy2(token, root / token.name)
            print("COPIED %s from the token store" % token.name)
    for name, kind in (("venv", "junction"), ("venv312", "junction"), ("ARTI_SOUL.md", "hardlink")):
        link, target = root / name, source / name
        if link.exists():
            print("OK %s: already present" % name)
            continue
        if not target.exists():
            print("MISSING %s: not in %s either" % (name, source))
            continue
        if kind == "junction":
            mklink(link, target)
        else:
            os.link(target, link)
        made.append(dict(name=name, kind=kind, target=str(target)))
        print("LINKED %s -> %s (%s)" % (name, target, kind))
    manifest.parent.mkdir(parents=True, exist_ok=True)
    soak.write(manifest, dict(root=str(root), made=made))
    return 0


def unlink_runtime(a, *, root=None, manifest_dir=None):
    """Remove only the links link_runtime made (junction: the link, never its target)."""
    root = Path(root or ROOT)
    manifest = Path(manifest_dir or RUNTIME_LINKS) / (root.name + ".json")
    if not manifest.is_file():
        return 0
    kept = []
    for row in soak.read(manifest).get("made", []):
        link = root / row["name"]
        if row["kind"] == "junction" and _is_junction(link):
            os.rmdir(link)
        elif row["kind"] == "hardlink" and link.is_file() and Path(row["target"]).is_file()                 and os.path.samefile(link, row["target"]):
            os.remove(link)
        elif link.exists():
            kept.append(row)
            print("KEPT %s: not the link this step made" % row["name"])
            continue
        print("UNLINKED %s" % row["name"])
    if kept:
        soak.write(manifest, dict(root=str(root), made=kept))
    else:
        manifest.unlink()
    return 0


# ------------------------------------------------------------------ memory report (livestream test 2026-11-17)
def memory_report_lines(snapshot, procs, top=12):
    """Pure: the commit line plus the biggest processes, each tagged ours / the owner's. Nothing is closed."""
    lines = []
    if snapshot:
        limit = snapshot.get("limit_now_gb") or 0
        pct = 100.0 * float(snapshot.get("committed_gb") or 0) / float(limit) if limit else None
        lines.append("commit %.1f/%.1f GB%s" % (float(snapshot.get("committed_gb") or 0), float(limit or 0),
                                                 (" (%.0f%%)" % pct) if pct is not None else ""))
    rows = sorted(procs, key=lambda r: -float(r.get("gb") or 0))[:top]
    for r in rows:
        lines.append("%5.2f GB  %-6s %s%s" % (float(r.get("gb") or 0), r.get("owner", "owner"), r.get("name", "?"),
                                              (" [%s]" % r["note"]) if r.get("note") else ""))
    ours = sum(float(r.get("gb") or 0) for r in procs if r.get("owner") == "ours")
    lines.append("ours in total %.1f GB; close only ours (never the owner's) unless the owner says so" % ours)
    return lines


def _process_rows():
    script = (
        "Get-CimInstance Win32_Process | ForEach-Object { $p = Get-Process -Id $_.ProcessId -EA SilentlyContinue; "
        "if ($p) { [pscustomobject]@{name=$_.Name; gb=[math]::Round($p.PrivateMemorySize64/1GB,2); "
        "path=[string]$_.ExecutablePath; cmd=[string]$_.CommandLine; title=[string]$p.MainWindowTitle} } } "
        "| Sort-Object gb -Descending | Select-Object -First 40 | ConvertTo-Json -Compress")
    rows = json.loads(soak.ps(script) or "[]")
    out = []
    for r in rows if isinstance(rows, list) else [rows]:
        text = (str(r.get("path") or "") + " " + str(r.get("cmd") or "")).lower()
        name, note = str(r.get("name") or ""), ""
        if name.lower().startswith("stardewmoddingapi"):
            ours = "[farmhand]" in str(r.get("title") or "")
            note = "her farmhand" if ours else "the HOST game"
        else:
            # a process whose path or command sits under the workspace folder (ARTI) is ours
            ours = os.path.join(str(ARTI), "").lower() in text
        out.append(dict(name=name, gb=r.get("gb"), owner="ours" if ours else "owner", note=note))
    return out


def memory_report(a):
    """T-30 before a stream: what holds memory, ours vs the owner's. Read-only."""
    from scripts.livepath import bringup
    for line in memory_report_lines(bringup.memory_snapshot(), _process_rows()):
        print(line)
    return 0


def voice(a):
    from scripts.livepath.mabar_supervise import _deliberate_voice_stop

    run = Path(a.run)
    if _deliberate_voice_stop(run):
        log(run, "voice_start_refused", reason="stop_present")
        return 1
    if preflight(a, launch="voice"):
        return 1
    if bridge_pids():
        raise RuntimeError("an arti_bridge process is already running (ours or the owner's); one voice only")
    if _deliberate_voice_stop(run):
        log(run, "voice_start_refused", reason="stop_present")
        return 1
    if free_gib() < 15:
        raise RuntimeError("C: below 15 GiB")
    vts = vts_reachable(a.vts_port)
    log(run, "vts_check", port=a.vts_port, reachable=vts, note="not reachable = logged only; bridge retries")
    mic = a.mic if a.mic is not None else mic_index()
    passive = False
    if a.passive:
        route = audio_route()
        passive, why = passive_allowed(route)
        log(run, "audio_route", route=route, passive_requested=True, passive=passive, why=why)
    ssn = ssn_source(a)
    why = vault_status(run)
    if why:
        print("!!! ingatan vault: mati (sebab %s); dia main tanpa ingatan lama. Kabari owner. !!!" % why)
    continued = soak.read(run / "continued.json") if (run / "continued.json").is_file() else None
    alerts = not bool(getattr(a, "no_alerts", False))
    cfg = mabar_bridge_config(run, vts=not a.no_vts, mic=mic, passive=passive, vts_port=a.vts_port,
                              youtube_video_id=a.youtube_video_id, ssn=ssn, live=live_overlay(), vault_why=why,
                              continued=continued, classic=bool(getattr(a, "classic", False)),
                              desk=bool(getattr(a, "desk", False)), alerts=alerts,
                              alert_yield=bool(getattr(a, "alert_yield", False)),
                              api_voice=getattr(a, "api_voice", None),
                              own_play=bool(getattr(a, "own_play", False)),
                              voice_cap_usd=float(getattr(a, "voice_cap_usd", 0.50)))
    path = run / "rehearsal-config.json"
    soak.write(path, cfg)
    # The controller is a separate process: enable the same projections at its source.
    trial_path = run / "trial-config.json"
    if trial_path.is_file():
        trial = soak.read(trial_path)
        updated = play_voice_trial_config(trial, cfg)
        if updated != trial:
            soak.write(trial_path, updated)
    # The bridge loads the run's live_session.json AFTER this config and lets it win for these keys (live 2026-11-10:
    # an old file with chat off and no id kept the YouTube reader off). Keep them equal to this config.
    session_file = run / "live_session.json"
    if session_file.is_file():
        session = soak.read(session_file)
        session.update({k: cfg[k] for k in ("youtube_chat_enabled", "youtube_video_id", "vts_api_port") if k in cfg})
        soak.write(session_file, session)
    from scripts.livepath.bridge import load_config
    load_config(path)
    env = rehearse.child_env(dict(os.environ), run)
    if REHEARSAL_SCRIPTED_PTT:  # rehearsal only: the bridge accepts scripted owner lines as PTT
        env["ARTI_REHEARSAL_SCRIPTED_PTT"] = "1"
    else:
        env.pop("ARTI_REHEARSAL_SCRIPTED_PTT", None)
    env.update(ARTI_REHEARSAL_CONFIG=str(path), ARTI_MIND_DIR=str(MIND), PYTHONPATH=str(ROOT),
               ARTI_MABAR_REACTIONS=str(run / "owner_reactions.jsonl"))
    env.pop("ARTI_SSN_SESSION_FILE", None)
    if ssn == "file":
        env["ARTI_SSN_SESSION_FILE"] = str(Path(a.ssn_session_file).resolve())   # a path; the bridge reads it
    key = groq_key()
    if key:
        env["GROQ_API_KEY"] = key
    alert_secrets = inject_alert_provider_env(env, enabled=alerts)
    rehearse.scratch_path(run, os.environ).mkdir(parents=True, exist_ok=True)
    (run / "bridge").mkdir(exist_ok=True)
    log(run, "voice_start", mic=mic, vts_enabled=not a.no_vts, passive=passive, ssn_chat=ssn or "off", groq_key="GROQ_API_KEY present" if key else "absent (local whisper)",
        saweria=alert_secrets.get("SAWERIA_STREAM_KEY"), streamlabs=alert_secrets.get("STREAMLABS_SOCKET_TOKEN"),
        trakteer_hash=alert_secrets.get("TRAKTEER_HASH"),
        trakteer_stream_key=alert_secrets.get("TRAKTEER_STREAM_KEY"),
        mic_source="arg" if a.mic is not None else "stream-folder live_session.json asr_input_device")
    with (run / "bridge" / "console.log").open("ab") as console:
        if _deliberate_voice_stop(run):
            log(run, "voice_start_refused", reason="stop_present")
            return 1
        proc = subprocess.Popen([str(ROOT / "venv/Scripts/python.exe"), "-B", "-u", str(ROOT / "arti_bridge.py"),
                                 "--no-wizard", "--no-save-bridge"], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                stdout=console, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        soak.write(run / "voice.json", dict(pid=proc.pid, utc=utc()))
        # `supervise` restarts a dead voice with exactly these arguments (RELIABILITY_ANALYSIS 4b).
        soak.write(run / "voice_args.json", dict(argv=list(getattr(a, "argv", None) or []), utc=utc()))
        said = run / "owner-say.txt"   # `mabar.py say`: a typed owner line on the bridge's own stdin (smoke only)
        offset = said.stat().st_size if said.is_file() else 0
        try:
            while proc.poll() is None:
                if said.is_file() and said.stat().st_size > offset:
                    with said.open("rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                    offset += len(chunk)
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                time.sleep(1)
        except KeyboardInterrupt:
            (run / "bridge-stop-request").touch()
            proc.wait(timeout=60)
    log(run, "voice_exit", rc=proc.returncode)
    return proc.returncode


# ------------------------------------------------------------------ host / join / play / stop

def helper_save_ids(save_dir):
    """uniqueIDForThisGame, HOST id and the one established farmhand id, read only from the save
    (the same rule as soak.stage_template; nothing is written)."""
    import xml.etree.ElementTree as ET
    save_dir = Path(save_dir)
    root = ET.parse(save_dir / save_dir.name).getroot()
    host_id = int(root.find("player").findtext("UniqueMultiplayerID", "0"))
    peers = root.findall("./farmhands/Farmer")
    peers += [e for e in root.findall(".//farmhand") if e.find("UniqueMultiplayerID") is not None]
    unique = {int(e.findtext("UniqueMultiplayerID", "0")): e for e in peers}
    unique.pop(host_id, None); unique.pop(0, None)
    if len(unique) != 1:
        raise RuntimeError("the save must hold exactly one established farmhand for a helper HOST")
    (peer_id, peer), = unique.items()
    return dict(save_folder=save_dir.name, save_id=int(root.findtext("uniqueIDForThisGame")),
                host_id=host_id, farmhand_id=peer_id, farmhand_name=peer.findtext("name") or "")


def helper_host_config(run, ids, *, host_delay=10, startup_pause=300, sleep_at=2400):
    """Solo stream (owner 2026-11-15): the program does the HOST's plain chores (load, host, answer the
    sleep ready check, go to bed). No gifts, no staged items: this is the owner's real farm."""
    return dict(Enabled=True, RunId=Path(run).name, OutputDirectory=str(Path(run) / "host"),
                SaveFolder=ids["save_folder"], SaveId=ids["save_id"], HostId=ids["host_id"],
                FarmhandId=ids["farmhand_id"], HostDelaySeconds=host_delay, StartupPauseSeconds=startup_pause,
                SleepAt=sleep_at, GiftTimes=[], StageHostGiftCount=0, GiftWhen="idle", GiftIdleSeconds=20)


def install_host_helper(run, booking, save_folder):
    """Copy the booked ArtiHostHelper (bytes checked) into this run's HOST mods with a stream config."""
    run = Path(run)
    ids = helper_save_ids(APPDATA / "Saves" / save_folder)
    target = run / "runtime/batch-host/host-mods/ArtiHostHelper"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(booking["helper"], target)
    soak.write(target / "config.json", helper_host_config(run, ids))
    soak.staged_mod_preflight(((target, "ArtiHostHelper.dll", booking["helperSha256"]),))
    return ids


def remove_host_helper(run):
    target = Path(run) / "runtime/batch-host/host-mods/ArtiHostHelper"
    if target.exists():
        shutil.rmtree(target)


def set_host_controller(run, value):
    """What her native facts call the HOST farmer's controller: the owner, or the helper program."""
    for path in (PREP / "trial-config.json", Path(run) / "trial-config.json"):
        if path.is_file():
            trial = soak.read(path)
            trial["stardew_host_controller"] = value
            soak.write(path, trial)


def prepare_host_relaunch(run, booking, *, stamp=None, tool_fn=None, deadline=None):
    """Move used runtime/batch-host and host aside, prepare a fresh batch-host (11-15 morning procedure)."""
    run = Path(run)
    from scripts.livepath.halt_authority import RecoveryTimedOut

    def check_time():
        if deadline is not None and time.time() >= deadline:
            raise RecoveryTimedOut("recovery_deadline")

    stamp = stamp or time.strftime("%H%M%S")
    moved = []
    batch = run / "runtime" / "batch-host"
    if batch.exists():
        check_time()
        dest = run / "runtime" / ("batch-host.used-%s" % stamp)
        shutil.move(str(batch), str(dest))
        moved.append(dest)
    host_dir = run / "host"
    if host_dir.exists():
        check_time()
        dest = run / ("host.used-%s" % stamp)
        shutil.move(str(host_dir), str(dest))
        moved.append(dest)
    tmp = run / ("runtime-host-%s" % stamp)
    options = {"deadline": deadline} if deadline is not None else {}
    check_time()
    (tool_fn or tool)(run, "witness_run.py",
                      ["prepare-run", "--run-dir", tmp, "--template", run / "template",
                       "--sha256", booking["dllSha256"], "--row", "mabar",
                       "--snapshot", "unused-no-restore"],
                      "prepare-host", **options)
    dest = run / "runtime" / "batch-host"
    dest.parent.mkdir(parents=True, exist_ok=True)
    check_time()
    shutil.move(str(tmp / "batch-host"), str(dest))
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    log(run, "host_relaunch_prepared", stamp=stamp, moved=[str(p) for p in moved])
    return dict(stamp=stamp, moved=moved, batch_host=dest)


def host(a):
    run = Path(a.run)
    deadline = getattr(a, "recovery_deadline", None)
    opts = {"deadline": deadline} if deadline is not None else {}
    _check_recovery_deadline(run, a)
    running = processes()
    _check_recovery_deadline(run, a)
    if "stardewmoddingapi.exe" in running:
        raise RuntimeError("a game is already running")
    if getattr(a, "relaunch", False):
        if getattr(a, "recovery", False):
            rearm_halt(run, a, "launch")  # ADR 254 (R6 B3): the supervisor's relaunch reads every stop first
        booking = rehearse.read_booking(a.spec)
        _check_recovery_deadline(run, a)
        prepare_host_relaunch(run, booking, **opts)
    _check_recovery_deadline(run, a)
    if getattr(a, "helper", False):
        if not a.continue_save:
            raise RuntimeError("host --helper needs --continue SAVE_FOLDER (the farm the program hosts)")
        ids = install_host_helper(run, rehearse.read_booking(a.spec), a.continue_save)
        _log_host_save_backup_gap(run, ids["save_folder"])
        set_host_controller(run, "harness")
        log(run, "host_helper_installed", save=ids["save_folder"], farmhand=ids["farmhand_name"])
    else:
        remove_host_helper(run)
        set_host_controller(run, "owner")
    _check_recovery_deadline(run, a)
    tool(run, "witness_run.py", ["launch-host", "--recovery", run / "runtime/batch-host"], "launch-host", **opts)
    log(run, "host_launched", helper=bool(getattr(a, "helper", False)),
        next=("the helper loads and hosts the save; then `rejoin --name <her name>`" if getattr(a, "helper", False)
              else "owner: Co-op > Host > new farm (>= 1 cabin), windowed; watch the intro; then `join`"))


def inbox():
    return Path(soak.read(MOD / "config.json")["CommandInboxDirectory"])


def find_scope(value):
    """save_id:player_id from the farmhand's native occurrence ring, as native_story.record_scope builds it."""
    if isinstance(value, dict):
        ring = value.get("occurrences")
        if isinstance(ring, dict) and ring.get("save_id") and ring.get("player_id") is not None:
            return f"{ring['save_id']}:{ring['player_id']}"
        for v in value.values():
            found = find_scope(v)
            if found:
                return found
    return None


def occurrence_scope(folder):
    """save_id:player_id of the newest native occurrence ring file the farmhand mod writes
    (`occurrences.<save_id>.<player_id>.json`), the identity native_story.record_scope uses."""
    rings = sorted(Path(folder).glob("occurrences.*.*.json"), key=lambda p: p.stat().st_mtime)
    if not rings:
        return None
    parts = rings[-1].name.split(".")
    return f"{parts[1]}:{parts[2]}" if len(parts) == 4 and parts[1].isdigit() else None


def color_word(hex_code):
    """A plain Indonesian colour name for a native #RRGGBB, by fixed hue/value bands (no lookup, no guess)."""
    import colorsys
    try:
        r, g, b = (int(str(hex_code).lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except (ValueError, TypeError):
        return None
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if v < 0.18:
        return "hitam"
    if s < 0.15:
        return "putih" if v > 0.85 else "abu-abu"
    deg = h * 360
    bands = ((15, "merah"), (40, "oranye"), (65, "kuning"), (165, "hijau"), (195, "toska"),
             (255, "biru"), (290, "ungu"), (340, "pink"), (361, "merah"))
    name = next(n for limit, n in bands if deg < limit)
    if name in ("merah", "oranye") and v < 0.6 and s < 0.7:
        name = "cokelat"
    return name + (" gelap" if v < 0.45 else "")


def _ui_number(value):
    return value + 1 if isinstance(value, int) else "?"


def readback_lines(profile, options, mismatches=()):
    """Her look as facts from the native farmer_profile read back after OK. A field the verify flagged, or the
    profile lacks, is stated as not verified: never filled from her intent or a guess."""
    if not isinstance(profile, dict) or not profile.get("is_customized"):
        return ["Karaktermu belum terverifikasi dari profil native; rinciannya belum diketahui."]
    bad = {str(m).split(":", 1)[0].strip() for m in mismatches or ()}
    labels = {kind: {str(o.get("id")): o.get("label") for o in (options or {}).get(kind, ()) if isinstance(o, dict)}
              for kind in ("shirt", "pants")}
    gender = {"female": "perempuan", "male": "laki-laki"}.get(profile.get("gender"), profile.get("gender"))

    def known(field, text):
        if field in bad or profile.get(field) in (None, ""):
            return "%s belum terverifikasi." % field
        return text

    shirt = labels["shirt"].get(str(profile.get("shirt_id"))) or "id %s, namanya belum diketahui" % profile.get("shirt_id")
    pants = labels["pants"].get(str(profile.get("pants_id"))) or "id %s" % profile.get("pants_id")
    lines = [
        known("name", "Namamu %s." % profile.get("name")),
        known("gender", "Gendermu %s." % gender),
        known("favorite_thing", "Hal favoritmu: %s." % profile.get("favorite_thing")),
        known("hair_color", "Rambutmu %s (kode warna %s), model rambut nomor %s." % (
            color_word(profile.get("hair_color")) or "warnanya belum diketahui", profile.get("hair_color"),
            _ui_number(profile.get("hair")))),
        known("shirt_id", "Bajumu %s." % shirt),
        known("pants_id", "Bawahanmu %s, warna %s (kode %s)." % (
            pants, color_word(profile.get("pants_color")) or "belum diketahui", profile.get("pants_color"))),
        known("eye_color", "Matamu %s (kode %s)." % (color_word(profile.get("eye_color")) or "belum diketahui",
                                                       profile.get("eye_color"))),
        known("skin", "Warna kulit nomor %s." % _ui_number(profile.get("skin"))),
        known("accessory", "Tanpa aksesori." if profile.get("accessory") == -1
              else "Aksesori nomor %s." % _ui_number(profile.get("accessory"))),
    ]
    if profile.get("farm_name"):
        lines.append("Farm ini bernama %s Farm." % profile["farm_name"])
    return lines


def creation_facts(karakter_log):
    """The last verify row of buat_karakter and the options she saw: (profile or None, mismatches, options)."""
    profile, mismatches, options = None, [], {}
    path = Path(karakter_log)
    if not path.is_file():
        return profile, mismatches, options
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("event") == "options_seen":
            options = (row.get("options") or {}).get("options") or {}
        elif row.get("event") == "verify":
            profile, mismatches = row.get("farmer_profile"), list(row.get("mismatches") or ())
    return profile, mismatches, options


def created_moment(run):
    """Short game moment after OK; the details are in her status line, read back from the native profile."""
    situation = "[Stardew] Karaktermu sudah jadi. Rinciannya dibaca ulang dari profil native game dan ada di statusmu."
    folder = Path(run) / "runtime"
    now = time.time()
    payload = dict(schema="arti.stardew_moment/1", kind="other", situation=situation, ts=now, ttl_s=300)
    tmp = folder / (".ev-%d-0.tmp" % int(now * 1000))
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / ("ev-%d-0.json" % int(now * 1000)))


def choice_moment(run, choice):
    """She hears her own choice while the presses run: her summary and name, as facts (the moment line is 200 chars
    at most; every field and reason is in the ledger and in the choice file)."""
    situation = ("[Stardew] Kamu barusan memilih karaktermu sendiri: %s. Nama: %s. "
                 "Tombolnya lagi ditekan satu-satu, OK paling akhir." % (
                     str(choice.get("summary") or "").strip()[:70], str(choice.get("name") or "")[:12]))
    folder = Path(run) / "runtime"
    now = time.time()
    payload = dict(schema="arti.stardew_moment/1", kind="other", situation=situation[:1800], ts=now, ttl_s=300)
    tmp = folder / f".ev-{int(now * 1000)}-0.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / f"ev-{int(now * 1000)}-0.json")


def remember_look(run, choice, profile, *, lines=None, mismatches=()):
    """Her own look in the mind ledger (permanent `own_look` row + a dated story row she reads for a week)."""
    import arti_mind
    from arti_runtime.native_story import day_of
    scope = occurrence_scope(inbox()) or "mabar:self"
    # What she knows of her look = the native readback; her model's choice and summary are kept only as intent.
    readback = list(lines or readback_lines(profile, {}, mismatches))
    facts = dict(intent_choice=choice, intent_summary=str(choice.get("summary") or "")[:240], farmer_profile=profile,
                 verified=profile is not None and not mismatches, mismatches=list(mismatches or ()),
                 chosen_by="ARTI (CharacterChoice via her model)", source="native farmer_profile readback after OK",
                 date="1:spring:1", event_id="UNKNOWN", event_type="CharacterCreation", location="UNKNOWN",
                 lines=[dict(speaker="profil native", text=line[:240]) for line in readback], changes=[],
                 complete=True, bounded=True, unknown=profile is None)
    arti_mind.record_items(MIND, "stardew", scope=scope, kind="own_look",
                           items=[dict(id="farmer_look", status="created", facts=facts)], day=1, day_label="1:spring:1")
    arti_mind.record_items(MIND, "stardew", scope=scope, kind="native_story",
                           items=[dict(id="own-character-creation", status="ended", facts=facts)],
                           day=day_of("1:spring:1"), day_label="1:spring:1")
    log(run, "look_remembered", scope_known=scope != "mabar:self")


def join(a):
    from scripts.livepath import halt_authority as HA
    run = Path(a.run)
    if HA.prepared_owner_run(run):
        log(run, "hands_held", reason="owner_join_requires_human_input",
            message="Join the selected farm from her window using the rejoin handoff.")
        return 2
    games = soak.ps("@(Get-Process StardewModdingAPI -ErrorAction SilentlyContinue).Count")
    decision = rearm_halt(run, a, "launch" if games.strip() == "1" else "observe")
    if decision.halt is not None:
        report_hold(run, HA.Decision("hold", "character_creation_requires_operator_rearm",
                                      halt=decision.halt, held=decision.held), a)
    if games.strip() == "1":
        try:
            tool(run, "witness_run.py", ["launch-farmhand", "--join-slot", "new"], "launch-farmhand")
        except RuntimeError as exc:
            # Exit 6 = joined, creation menu open but checked while her window was still active (the mod only
            # publishes it for an inactive farmhand window). wait-charcreate below is the real check (runbook 10-24).
            log(run, "launch_farmhand_nonzero", detail=str(exc)[:200], next="HOST to front, then wait-charcreate")
    else:
        log(run, "farmhand_already_running", games=games.strip())
    tool(run, "ui_op.py", ["fg", (run / "runtime/host-hwnd.txt").read_text(encoding="utf-8").strip()], "host-fg-pre")
    tool(run, "witness_run.py", ["wait-charcreate"], "wait-charcreate")
    # buat_karakter needs the farmhand window inactive: the HOST (the owner's window) in front, as soak.py does.
    tool(run, "ui_op.py", ["fg", (run / "runtime/host-hwnd.txt").read_text(encoding="utf-8").strip()], "host-fg")
    choice_file, karakter_log = run / "character-choice.json", run / "buat-karakter.jsonl"
    common = ["--folder", inbox(), "--config", run / "trial-config.json", "--log", karakter_log]
    if a.owner_hint:
        common += ["--owner-hint", a.owner_hint]
    # 1) Her model chooses every control (nothing pressed). 2) The same choice is pressed, OK last.
    # 3) Only the native readback after OK becomes what she knows about her look (owner 2026-11-10: no guesses).
    tool(run, "buat_karakter.py", [*common, "--save-choice", choice_file], "buat-karakter-choose")
    choice = soak.read(choice_file)
    try:
        tool(run, "buat_karakter.py", ["--folder", inbox(), "--config", run / "trial-config.json", "--log",
                                        karakter_log, "--choice-file", choice_file, "--yes"], "buat-karakter-press")
    finally:
        profile, mismatches, options = creation_facts(karakter_log)
        verified = isinstance(profile, dict) and bool(profile.get("is_customized"))
        lines = readback_lines(profile, options, mismatches)
        soak.write(run / "self_facts.json", dict(source="native farmer_profile readback after OK", utc=utc(),
                                                 verified=verified, mismatches=mismatches, lines=lines))
        created_moment(run)
        remember_look(run, choice, profile if verified else None, lines=lines, mismatches=mismatches)
    log(run, "joined", name=(profile or {}).get("name"), verified=verified and not mismatches, mismatches=mismatches)


def cycle_dirs(run):
    root = Path(run) / "runtime/live7"
    return sorted((p for p in root.glob("c*") if p.is_dir() and p.name[1:].isdigit()), key=lambda p: int(p.name[1:]))


def current_cycle(run):
    cycles = cycle_dirs(run)
    return cycles[-1] if cycles else Path(run) / "runtime/live7/c1"


def next_cycle(run):
    cycles = cycle_dirs(run)
    return Path(run) / "runtime/live7" / ("c%d" % (int(cycles[-1].name[1:]) + 1 if cycles else 1))


# ------------------------------------------------------------------ the live profile
# What a livestream session runs with lives here, not in a wrapper outside the repo, so a rehearsal and a live run
# cannot drift apart. The play config, the bridge config and the farmhand mod flags each get their part;
# `--rehearsal-scratch` runs the same profile. mabar status and preflight print it.
LIVE_PROFILE_NAME = "live-default"
LIVE_PLAY = dict(
    stardew_decision_provider_kind="openai_api", stardew_decision_model="gpt-6-luna",
    stardew_decision_effort="medium", stardew_decision_service_tier="priority",
    stream_game_notes=True, live7_fact_line_enabled=True, stardew_page_budget_scale=2,
    stardew_game_text_delivery_enabled=True, stardew_live7_place_time_page=True, stardew_live7_goal_ladder=True,
    live7_mind_scope=True,
    stardew_live7_plan_on_page=True,
    stardew_live7_ship_on_the_way=True, stardew_live7_mail_on_the_way=True, stardew_live7_menu_visit_bound=True,
    stardew_live7_success_stays=True,
    stardew_live7_press_results=True,
    stardew_offer_cache_enabled=False, live7_own_play_enabled=True,
    # The game lane writes her experience digest only with its own copy of this key (experience_digest.SETTING,
    # arti_stardew_live7). It was only in LIVE_BRIDGE, so the voice read a digest that was never written.
    voice_game_memory_enabled=True,
    # ADR 243 (2026-11-19): her raw hands (step, face, the action button, the held tool; a menu's own keys) on every page.
    stardew_live7_raw_hands=True,
    # Open-floor local walks, one-tile step, and sampling-time quest delta filter.
    stardew_live7_local_walks=True,
)
LIVE_BRIDGE = dict(
    owner_chat_as_owner=True,  # the channel's own account is the owner, not a viewer
    stream_game_notes=True, live_voice_fixes_v1=True, thanks_once_enabled=True, live_play_rules_enabled=True, mic_priority_mark=True,
    voice_game_memory_enabled=True,  # the owner's "[recorded remark omitted]"
    live7_mind_scope=True,  # farm-scoped mind stores and active-cycle gated voice state
    # V13 (2026-11-18), ADR 200 "[recorded remark omitted]": her voice child carries no Codex harness
    # (`codex debug prompt-input` showed ~65k chars of tools, skills, MCP and multi-agent text).
    codex_voice_lean_harness=True,
    # V13: ONE voice thread per session (ADR 200 append context, "V13"): stable once, each line once, no turn
    # cap. It ends only at a privacy boundary, an unusable provider state, or near the ceiling, where the
    # replacement carries every session line verbatim while they fit (a summary only beyond that). "auto" = the
    # window her Codex child really gets (set in the operator's Codex config). Timeout 20 s: well above the slowest long-thread turn measured
    # on a test; a turn that overruns it stops and the thread stays.
    voice_append_context=True, codex_context_window="auto", voice_context_timeout_sec=20.0,
    voice_context_rollover_keeps_lines=True,
    # V14 (2026-11-18), ADR 238: her vault recall says which farm a session note is from ("farm ini", "farm <Name>",
    # "catatan lama, farm tidak tercatat"); her session notes carry the farm tag. The farm name rides in
    # `session_farm_name` (mabar_bridge_config, from the run).
    vault_farm_labels=True,
)
# ADR 243: GenericWorldInputEnabled lets the mod publish its generic ACTION/USE target for any faced tile.
LIVE_MOD_FLAGS = dict(CharacterCreationEnabled=True, UiPadMenusEnabled=True, GenericWorldInputEnabled=True,
                      FestivalReflexEnabled=True, ReflexFishingEnabled=True,
                      # The mod's half of the text delivery. Free play stages it from the trial setting; without it here
                      # only the Python half would be on, and no game text would become hers on this path.
                      GameTextDeliveryEnabled=LIVE_PLAY["stardew_game_text_delivery_enabled"])


def live_profile_lines():
    """One line per part, for `status` and preflight: nobody runs a live on defaults without seeing it."""
    fmt = lambda d: ", ".join("%s=%s" % (k, d[k]) for k in sorted(d))
    return ["profile %s play: %s" % (LIVE_PROFILE_NAME, fmt(LIVE_PLAY)),
            "profile %s bridge: %s" % (LIVE_PROFILE_NAME, fmt(LIVE_BRIDGE)),
            "profile %s mod: %s" % (LIVE_PROFILE_NAME, fmt(LIVE_MOD_FLAGS))]


# The live run sheet: the operator's steps before every live, printed by preflight right under the profile so it
# cannot be skipped. They are steps, not checks: nothing here is verified.
LIVE_RUN_SHEET = (
    "audio check before the games open",
    "paste the three profile lines above into the run note before going live",
    "long voice thread: plan its rollover to a fresh thread that is already warm",
)


def live_run_sheet_lines():
    return ["DO " + step for step in LIVE_RUN_SHEET]


DECISION_CAP_USD = 2.0  # ADR 164: the game API session budget when `--decision-cap-usd` is not given


def play_trial_config(trial, run, cap=DECISION_CAP_USD):
    """ADR 164: API decisions, existing latched Codex fallback; one budget across days; the live profile."""
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or cap <= 0:
        raise ValueError("positive finite decision cap required")
    config = dict(trial, **LIVE_PLAY, stardew_openai_session_cap_usd=float(cap),
                  stardew_openai_session_state_file=str(Path(run) / "decision-api-session.json"))
    if trial.get('stardew_live7_plan_on_page') is False:
        config['stardew_live7_plan_on_page'] = False  # a matched control can opt out.
    if trial.get('live7_mind_scope') is False:
        config['live7_mind_scope'] = False  # a trial config may switch mind scoping off.
    if trial.get('stardew_live7_local_walks') is False:
        config['stardew_live7_local_walks'] = False  # a trial config may switch local walks off.
    return config


def play_once(a, cycle=None):
    """One Live7 cycle until STOP, host_left, or refusal. Does not wait for the HOST to return."""
    from scripts.livepath import halt_authority as HA, owner_join
    run = Path(a.run)
    bound = getattr(a, "recovery_expected", None) or {}
    owner_play = bool(bound.get("owner_host")) or HA.prepared_owner_run(run)
    origin = getattr(a, "owner_origin", None)
    if owner_play and not bound.get("owner_host"):
        origin = tuple(HA.cycle_dirs(run))
        a.recovery_expected = verify_owner_play(a, run)
    background = getattr(a, "background_cycle", False)
    start_opts = {"deadline": a.recovery_deadline} if background else {}
    recovery = getattr(a, "recovery", False) or getattr(a, "recovery_expected", None)
    if recovery and not owner_play:
        rearm_halt(run, a)
    trial = play_trial_config(soak.read(run / "trial-config.json"), run,
                              getattr(a, "decision_cap_usd", DECISION_CAP_USD))
    _check_recovery_deadline(run, a)
    soak.write(PREP / "trial-config.json", trial)
    soak.write(run / "trial-config.json", trial)
    # ADR 241 C6: whatever is on screen is her first page (a ShippingMenu at a rejoin, a QuestLog, a question)
    tool(run, "witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", "23", "--timeout", "120",
                                 "--any-screen"], "wait-ready", **start_opts)
    _check_recovery_deadline(run, a)
    if not owner_play:
        released = release_farmhand_focus(run)  # no-op when every hand path is focus-independent
        log(run, "focus_release", step="play", ok=bool(released), cause=getattr(released, "cause", None))
        if not released:
            raise RuntimeError("farmhand_window_active")
    cycle = cycle or next_cycle(run)
    if recovery and not owner_play:
        rearm_halt(run, a)
    enroll = ["start-cycle", "--cycle-dir", cycle, "--host-owner", "human"]
    if getattr(a, "recovery_expected", None):
        enroll += ["--expected-native", json.dumps(a.recovery_expected)]
    _check_recovery_deadline(run, a)
    if owner_play:
        try:
            with owner_join.spawn_lock(inbox()):
                if tuple(HA.cycle_dirs(run)) != origin or cycle != next_cycle(run):
                    owner_join.refuse("owner_join_cycle_changed")
                if owner_join.running_cycle(run) is not None:  # `play` beside a cycle that waits for her: use `rejoin`
                    owner_join.refuse("owner_join_cycle_running")
                owner_join.enrollment_session(run, inbox(), a.recovery_expected)
                tool(run, "live_cycle.py", enroll, "start-cycle", **start_opts)
        except HA.HandsHeld as exc:
            report_hold(run, exc.decision, a)
        except RuntimeError:
            owner_join.stop_notice(run, inbox, log, episode="start-cycle")
            raise
    else:
        tool(run, "live_cycle.py", enroll, "start-cycle", **start_opts)
    if trial.get("stardew_host_controller") != "harness":
        try:
            save_name = cycle_status(cycle).get("save_name")
        except (OSError, ValueError, AttributeError):
            save_name = None
        _log_host_save_backup_gap(run, save_name)
    log(run, "cycle_started", cycle=str(cycle))
    if background:
        start_opts["background"] = True
    try:
        tool(run, "live_cycle.py", ["start-day", "--cycle-dir", cycle], "start-day", **start_opts)
    except RuntimeError:  # her cycle's process ended on a fault; in an owner-hosted run she may still stand in his world
        owner_join.cycle_notice(run, inbox, log, cycle)
        raise
    return cycle


def verify_owner_play(a, run):
    from scripts.livepath import halt_authority as HA, owner_join

    folder = inbox()
    try:
        host = owner_join.host_binding(run)
        return owner_join.verify_joined(run, folder, host, owner_join.selected(run, farmhand_name(a, run)),
                                        held=getattr(a, "hands_held", None))
    except HA.HandsHeld as exc:
        report_hold(run, exc.decision, a)


def cycle_status(cycle):
    path = Path(cycle) / "cycle_state.json"
    return soak.read(path) if path.is_file() else {}


def smapi_count(*, deadline=None):
    from scripts.livepath.halt_authority import RecoveryTimedOut
    query = "@(Get-Process StardewModdingAPI -ErrorAction SilentlyContinue).Count"
    if deadline is None:
        return soak.ps(query).strip()
    remaining = deadline - time.time()
    if remaining <= 0:
        raise RecoveryTimedOut("recovery_deadline")
    return soak.ps(query, timeout=remaining).strip()


def _recovery_game_count(a, count_fn):
    run = Path(a.run)
    _check_recovery_deadline(run, a)
    deadline = getattr(a, "recovery_deadline", None)
    if count_fn is not None:
        games = count_fn()
    elif deadline is not None and getattr(a, "recovery_clock", None) is None:
        games = smapi_count(deadline=deadline)
    else:
        games = smapi_count()
    _check_recovery_deadline(run, a)
    return str(games)


def host_joinable(*, count_fn=None):
    """HOST is up and the farmhand is not: one SMAPI process. Never starts HOST."""
    games = str((count_fn or smapi_count)())
    return games == "1"


def auto_rejoin_after_host_left(a, *, joinable_fn=None, sleep_fn=None, clock_fn=None,
                                rejoin_fn=None, print_fn=print, play_fn=None):
    """After host_left: wait until HOST is joinable and rejoin, bounded. Default OFF is a no-op.

    Does not touch the HOST save or process. One status line per attempt.
    """
    run = Path(a.run)
    if not getattr(a, "auto_rejoin", False):
        return 0
    tries = int(getattr(a, "auto_rejoin_tries", 12) or 0)
    minutes = float(getattr(a, "auto_rejoin_minutes", 30) or 0)
    if tries < 1 or minutes <= 0:
        log(run, "auto_rejoin_gave_up", reason="bound_zero", tries=tries, minutes=minutes)
        return 0
    after_join = joinable_fn or (lambda: _joined_host_alive(run))
    joinable_fn = joinable_fn or host_joinable
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.time
    rejoin_fn = rejoin_fn or rejoin_farmhand
    play_fn = play_fn or play_once
    started = clock_fn()
    deadline = started + minutes * 60
    from scripts.livepath.halt_authority import HandsHeld, RecoveryTimedOut
    a.recovery, holding = True, False  # ADR 254: a recovery; the stops that outrank it are read at every boundary
    n = 0
    while n < tries and clock_fn() < deadline:
        n += 1
        elapsed_min = max(0, (clock_fn() - started) / 60)
        joinable = bool(joinable_fn())
        print_fn("auto-rejoin %d/%d joinable=%d elapsed_min=%.1f" % (n, tries, int(joinable), elapsed_min))
        log(run, "auto_rejoin_attempt", attempt=n, tries=tries, joinable=joinable,
            elapsed_min=round(elapsed_min, 1))
        if holding and not joinable:
            from scripts.livepath import halt_authority as HA
            decision = HA.check(inbox(), run, act="observe", recovery=True, held=getattr(a, "hands_held", None))
            if decision.action == "hold" and HA.operator_stop(decision):
                report_hold(run, decision, a)
            n -= 1
        elif joinable and clock_fn() < deadline:
            try:
                a.recovery_deadline = deadline
                a.recovery_clock = clock_fn
                try:
                    rejoin_fn(a)
                finally:
                    a.recovery_deadline = None
                    a.recovery_clock = None
            except RecoveryTimedOut:
                if clock_fn() >= deadline:
                    break
            except HandsHeld as exc:  # ADR 254 (R6 B4): a hold is waited out, never a spent try; nothing was started
                from scripts.livepath import halt_authority as HA
                if HA.operator_stop(exc.decision):
                    raise
                holding, a.hands_held = True, exc.decision.held or getattr(a, "hands_held", None)
                n -= 1
            else:
                holding, a.hands_held = False, None
                if clock_fn() < deadline and after_join():
                    cycle = play_fn(a, cycle=next_cycle(run))
                    status = cycle_status(cycle).get("status")
                    log(run, "auto_rejoined", attempt=n, cycle=str(cycle), status=status)
                    if status == "not_recovered":
                        return 1
                    if status != "host_left":
                        return 0
        if n >= tries or clock_fn() >= deadline:
            break
        sleep_fn(min(15, max(0, deadline - clock_fn())))
    log(run, "auto_rejoin_gave_up", tries=tries, minutes=minutes)
    return 0


def _joined_host_alive(run):
    from scripts.m1_operator import win_facts
    from scripts.livepath import mabar_supervise
    if smapi_count() != "2":
        return False
    try:
        hwnd = int((Path(run) / "runtime/host-hwnd.txt").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    facts = win_facts.window(hwnd) or {}
    farmhand = next(iter(mabar_supervise.session_games(run, "owner")))
    return bool(facts.get("exists") and facts.get("pid") != farmhand[2])


def play(a, cycle=None):
    cycle = play_once(a, cycle=cycle)
    state = cycle_status(cycle)
    if state.get("status") == "host_left":
        left = state.get("host_left") or {}
        log(Path(a.run), "host_left", observed=left.get("observed"), day_saved=left.get("day_saved"),
            cycle=str(cycle))
        return auto_rejoin_after_host_left(a)
    return int(state.get("status") == "not_recovered")


def halt_latched():
    halt = inbox() / "halt.json"
    try:
        return json.loads(halt.read_text(encoding="utf-8")).get("state") == "latched"
    except (OSError, ValueError):
        return False


def stop_sentinels(run):
    """Live7 STOP sentinels a stopped/crashed cycle left (RUN and per cycle); only the operator tool archives them."""
    found = [Path(run) / "runtime/LIVE7_STOP"] + [c / "LIVE7_STOP" for c in cycle_dirs(run)]
    return [p for p in found if p.is_file()]


def rearm_halt(run, a=None, act="start", *, sleep_fn=None, clock_fn=None, allow_watchdog=False, owner_word=None):
    """Apply the table at a recovery boundary and archive only its own ended-cycle markers."""
    from scripts.livepath import halt_authority as HA, owner_join
    from arti_runtime import ipc
    from arti_stardew_stop import StopRefused
    from tools.arti_rearm import archive_retry_marker
    from uuid import uuid4

    sleep_fn, clock_fn = sleep_fn or time.sleep, clock_fn or time.monotonic
    deadline = clock_fn() + HA.REARM_PROOF_S
    folder = inbox()
    proof_path = Path(run) / "runtime/marker-archive-proof.json"
    proof_id = None

    def read():
        _check_recovery_deadline(run, a)
        decision = HA.check(folder, run, act=act, recovery=True, held=getattr(a, "hands_held", None),
                            expected=getattr(a, "recovery_expected", None), proof_id=proof_id,
                            allow_watchdog=allow_watchdog,
                            **({"owner_launch": owner_join.require_mode(owner_word, run)} if owner_word else {}))
        _check_recovery_deadline(run, a)
        return decision

    retry_marker_proof(run, folder, held=getattr(a, "hands_held", None), clock_fn=clock_fn)
    decision = read()
    log(run, "rearm_decision", act=act, action=decision.action, reason=decision.reason,
        sentinels=sorted(decision.sentinels))
    if decision.action == "hold":
        report_hold(run, decision, a)
    if decision.action != "archive_sentinels":
        return decision

    proof_id = uuid4().hex
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof = dict(id=proof_id, state="pending", identity=list(HA.identity(decision.session)),
                 markers={str(Path(path).resolve()): HA.sha256(raw) for path, raw in decision.sentinels.items()})
    ipc.write_json(proof_path, proof, fsync=True)
    pending = dict(decision.sentinels)
    outcome = "sentinels_archived"
    try:
        for path, raw in decision.sentinels.items():
            fresh = read()
            if fresh.action == "hold":
                report_hold(run, fresh, a)
            if (fresh.sentinels != pending or HA.identity(fresh.session) != HA.identity(decision.session)
                    or clock_fn() >= deadline):
                outcome = "declined"
                break
            archive_retry_marker(path, raw, reason=f"rearm:{decision.reason}:{path}", current=decision.session)
            del pending[path]
    except (OSError, StopRefused) as exc:
        outcome = "refused:%s" % type(exc).__name__
    log(run, "rearm", outcome=outcome)
    while True:
        sentinels_gone = not any(HA.present(path) for path in decision.sentinels)
        fresh = read()
        if fresh.action == "hold":
            report_hold(run, fresh, a)
        gap = HA.proof_gap(decision, decision.session, fresh.session, sentinels_gone and not fresh.sentinels, outcome)
        late = clock_fn() >= deadline
        if gap is None and not late:
            ipc.write_json(proof_path, dict(proof, state="proven"), fsync=True)
            log(run, "rearm_proven", action=decision.action)
            return decision
        if late or gap.startswith(("tool:", "foreign_session", "no_session")):
            why = gap or "late"
            ipc.write_json(proof_path, dict(proof, state="failed", reason=why), fsync=True)
            log(run, "rearm_unproven", why=why)
            report_hold(run, HA.Decision("hold", "rearm_unproven:" + why, session=fresh.session,
                                         paths=decision.paths), a)
        sleep_fn(1)


def retry_marker_proof(run, folder, *, held=None, clock_fn=None):
    """An expired or interrupted move cannot authorize play without a fresh, checked audit proof."""
    from arti_runtime import ipc
    from scripts.livepath import halt_authority as HA

    clock_fn = clock_fn or time.monotonic
    deadline = clock_fn() + HA.REARM_PROOF_S
    proof_path = Path(run) / "runtime/marker-archive-proof.json"
    _, proof = HA.read_record(proof_path)
    if not isinstance(proof, dict) or proof.get("state") not in ("pending", "failed"):
        return
    markers = proof.get("markers")
    if not isinstance(markers, dict) or not markers or not proof.get("id"):
        return
    decision = HA.check(folder, run, act="start", recovery=True, held=held, proof_id=proof["id"])
    if (decision.action != "nothing" or decision.sentinels
            or list(HA.identity(decision.session)) != proof.get("identity")):
        return
    for source, digest in markers.items():
        marker = Path(source)
        archive = marker.parent / "stop-archive"
        if HA.present(marker) or clock_fn() >= deadline:
            return
        for audit in archive.glob("*.audit.json"):
            target = audit.with_suffix("").with_suffix(".stop")
            _, info = HA.read_record(audit)
            if (isinstance(info, dict) and info.get("source") == source and info.get("sha256") == digest
                    and info.get("archive") == str(target.resolve())):
                try:
                    if HA.sha256(ipc.read_bytes(target)) == digest:
                        break
                except OSError:
                    continue
        else:
            return
    fresh = HA.check(folder, run, act="start", recovery=True, held=held, proof_id=proof["id"])
    if (fresh.action == "nothing" and not fresh.sentinels and HA.identity(fresh.session) == HA.identity(decision.session)
            and HA.read_record(proof_path)[1] == proof and clock_fn() < deadline):
        ipc.write_json(proof_path, dict(proof, state="proven"), fsync=True)


def _check_recovery_deadline(run, a):
    limit = getattr(a, "recovery_deadline", None)
    if limit is not None and (getattr(a, "recovery_clock", None) or time.time)() >= limit:
        from scripts.livepath.halt_authority import RecoveryTimedOut
        raise RecoveryTimedOut("recovery_deadline")


def report_hold(run, decision, a=None):
    """One `hands_held` row with the operator's command (once per held episode, or reason, for a caller that waits),
    then `HandsHeld`: nothing more is done."""
    from scripts.livepath import halt_authority as HA
    said = (decision.held or {}).get("episode_id") or decision.reason
    if a is None or getattr(a, "hands_said", None) != said:
        from scripts.livepath import owner_join
        owner_join.stop_notice(run, getattr(a, "command_dir", None) or inbox, log,  # owner-hosted and connected only
                               episode=said if not HA.operator_stop(decision) else "stop")
        # A refusal of the owner-hosted join is no lock; the re-arm tool has nothing to clear there.
        owner_join_hold = decision.reason.startswith("owner_join_")
        log(run, "hands_held", alert=HA.claim_hold_alert(run, decision), reason=decision.reason,
            halt=decision.halt, held=decision.held, clears="a_person" if owner_join_hold else "operator_rearm",
            command=None if owner_join_hold else HA.operator_command(
                getattr(a, "command_dir", None) or inbox(), decision.paths))
        if a is not None:
            a.hands_said = said
    if a is not None and decision.held is not None:
        a.hands_held = decision.held
    raise HA.HandsHeld(decision)


def recover_farmhand(run, slot, *, held=None, expected=None, stop_file=None, env=None, deadline=None, owner_word=None):
    """Run the recovery in the operator tools' run-scoped environment."""
    from scripts.livepath import halt_authority as HA
    from scripts.m1_operator.witness_run import RESTART_TIMEOUT_S

    deadline = min(deadline if deadline is not None else float("inf"), time.time() + RESTART_TIMEOUT_S)

    cycles = HA.cycle_dirs(run)
    args = dict(run=str(run), slot=slot, held=held, expected=expected, origin_cycle=cycles[-1].name if cycles else None,
                stop_file=str(stop_file) if stop_file is not None else None, deadline=deadline)
    child_env = env if env is not None else dict(env_for_tools(run), ARTI_M1_COMMAND_DIR=str(inbox()))
    try:
        command = [sys.executable, "-B", "-m", "scripts.livepath.mabar", "_recover-farmhand"]
        if owner_word is None:
            transfer = dict(capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            command.append(json.dumps(args))
        else:
            # Stderr stays attached so the person sees the handoff as soon as the worker pins the window.
            transfer = dict(input=json.dumps(dict(args, owner_word=owner_word)), stdout=subprocess.PIPE)
        result = subprocess.run(command, cwd=ROOT, env=child_env, text=True, encoding="utf-8", errors="replace",
                                timeout=max(0, deadline - time.time()), **transfer)
    except subprocess.TimeoutExpired as exc:
        raise HA.RecoveryTimedOut("farmhand recovery exceeded its restart budget") from exc
    try:
        outcome = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("farmhand recovery did not report an outcome") from None
    if "hold" in outcome:
        raise HA.HandsHeld(HA.Decision(**outcome["hold"]))
    if outcome.get("timeout"):
        raise HA.RecoveryTimedOut(outcome["timeout"])
    if result.returncode or "expected" not in outcome:
        raise RuntimeError("farmhand recovery failed: " + outcome.get("error", "unknown outcome"))
    return outcome["expected"]


OWNER_TRIAL_WAIT_S = 45  # how long a word waits for the cycle that lost her game to end its trial
HOST_ACCEPT_WAIT_S = 60  # a watchdog launch waits this long at most for a HOST that can accept her


def _recover_farmhand(run, slot, *, held, expected, stop_file, deadline, origin_cycle=None,
                      restart_module=None, save_identity_fn=None, owner_word=None):
    """Own the close, launch and join until the same successor can safely play."""
    from scripts.livepath import halt_authority as HA, mabar_supervise as sup, owner_join
    from scripts.m1_operator.witness_run import RESTART_TIMEOUT_S
    deadline = min(deadline if deadline is not None else float("inf"), time.time() + RESTART_TIMEOUT_S)
    if restart_module is None:
        from scripts.m1_operator import restart_farmhand as restart_module
    R = restart_module
    run = Path(run)
    folder = Path(R.paths.COMMAND_DIR)
    record = Path(R.paths.RUN) / ("recovery-" + str(time.time_ns()) + ".json")
    options = R.build_parser().parse_args(["--no-close", "--join-slot", slot])
    refs = R.load_refs(options.refs)
    state = argparse.Namespace(hands_held=held, command_dir=folder)
    startup = None
    restored = None
    prior = None
    launch_started_ms = None
    launcher = None
    her_window = False  # owner word: her window is known, so her own startup lock can be named with it
    previous = {}
    unread = {}
    cabin = False
    watchdog_attempted = False
    _, helper_cfg = HA.read_record(run / sup.HELPER_CONFIG)
    expected_save = (expected or {}).get("save_id")
    if expected_save is None and isinstance(helper_cfg, dict):
        expected_save = helper_cfg.get("SaveId")

    def read():
        return HA.read_evidence(folder, run, recovery=True, expected=expected, startup=startup)

    def stop_check(ev):
        if (ev.newest.name if ev.newest is not None else None) != origin_cycle:
            raise HA.HandsHeld(HA.Decision("hold", "stop_outranks_recovery:origin_cycle_changed", held=state.hands_held))
        if time.time() >= deadline:
            raise HA.RecoveryTimedOut("farmhand recovery exceeded its restart budget")
        if stop_file and HA.present(stop_file):
            raise HA.HandsHeld(HA.Decision("hold", "stop_outranks_recovery:cycle_marker", held=state.hands_held))
        if ev.outranking or ev.sentinels or ev.host_inert:
            decision = HA.decide(ev, act="join" if startup else "launch", held=state.hands_held)
            if HA.operator_stop(decision):
                raise HA.HandsHeld(decision)

    def gate(act):
        nonlocal prior, restored
        ev = read()
        stop_check(ev)
        proof = HA.watchdog_launch_proof(ev) if act == "launch" and owner_word is None else None
        decision = HA.decide(ev, act=act, held=state.hands_held, watchdog=proof, owner_launch=(
            owner_join.require_mode(owner_word, run) if owner_word is not None else False))
        if decision.action == "hold" and owner_word is not None:
            raise HA.HandsHeld(decision)
        if decision.action == "hold":
            report_hold(run, decision, state)
        if act == "launch":
            if decision.sentinels and owner_word is None:
                report_hold(run, HA.Decision("hold", "launch_waits_for_clear_end_markers", held=state.hands_held,
                                              paths=decision.paths), state)
            if prior is not None and HA.identity(prior.session) != HA.identity(decision.session):
                report_hold(run, HA.Decision("hold", "foreign_launch_session", held=state.hands_held), state)
            prior = decision
            restored = decision.halt["key"] if decision.halt else None
            if restored is not None:
                state.hands_held = decision.held
        return decision

    class GuardedAdapter(R.LiveAdapter):
        def alive(self, pid):
            if previous.get("pid") == pid:  # owner word: the recorded process, not whatever reuses its PID
                return not owner_join.wf.process_gone(pid, previous.get("process_start"))
            return super().alive(pid)

        def check_stop(self):
            ev = read()
            stop_check(ev)
            if owner_word is not None:
                launch = ev.launch[1] if isinstance(ev.launch[1], dict) else {}
                if launch.get("owner_attempt") == owner_word["id"] and launch.get("pid"):
                    owner_join.check_wait(ev, state.hands_held, restored=restored, window=her_window, unread=unread)
                owner_join.require_attempt(owner_word, run, slot=slot, parent="worker")
                if launcher is not None and launcher.poll() is not None:
                    try:
                        result = json.loads(launcher.stdout.read())
                    except ValueError:
                        owner_join.refuse("owner_join_launcher_failed")
                    if "hold" in result:
                        raise HA.HandsHeld(HA.Decision(**result["hold"]))
                    owner_join.refuse("owner_join_launcher_failed")

        def run(self, tool, *args, timeout=300):
            nonlocal launch_started_ms, launcher
            if owner_word is not None:
                if tool != "prep_trial.py" or args != ("launch", "farmhand"):
                    owner_join.refuse("owner_join_requires_human_input")
                self.check_stop()
                gate("launch")
                from scripts.m1_operator.prep_trial import start_owner_launcher
                guard = dict(run=str(run), inbox=str(folder), act="launch", deadline=deadline, origin_cycle=origin_cycle,
                             expected=expected, held=state.hands_held)
                launcher = start_owner_launcher(guard, owner_word)
                return 0, "", ""
            act = None
            if tool == "ui_op.py" and args[0] == "close":
                raise RuntimeError("farmhand recovery must use the in-game exit request")
            if tool == "prep_trial.py" and args[:2] == ("launch", "farmhand"):
                if gate("launch").watchdog and not host_accepts():  # after worker start, right before the launch
                    wait_for_host_accepting()
                    gate("launch")  # authority read again right after the wait
                launch_started_ms = int(time.time() * 1000)
                act = "launch"
            if tool == "ui_op.py" and args[0] != "shot" and startup is not None:
                wait_for_join()
                if args[0] in ("click", "move", "pos", "fg") and str(args[1]) != str(startup["hwnd"]):
                    raise RuntimeError("farmhand recovery input target changed")
                if args[0] == "type" and args[-2:] != ("--hwnd", startup["hwnd"]):
                    raise RuntimeError("farmhand recovery input target changed")
                act = "join"
            options = {}
            if act is not None:
                config = dict(run=str(run), inbox=str(folder), act=act, origin_cycle=origin_cycle,
                              deadline=deadline, expected=expected, startup=startup, held=state.hands_held,
                              stop_file=str(stop_file) if stop_file else None, cabin=cabin,
                              host_hwnd=worker.host_hwnd, save_id=expected_save, allow_watchdog=act == "launch")
                options["env"] = dict(os.environ, **{HA.RECOVERY_GUARD_ENV: json.dumps(config)})
            rc, out, err = super().run(tool, *args, timeout=min(timeout, max(0.01, deadline - time.time())), **options)
            if rc == 2 and out:
                for line in out.splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and "recovery_hold" in row:
                        report_hold(run, HA.Decision(**row["recovery_hold"]), state)
            return rc, out, err

    class GuardedRestart(R.Restart):
        def _slot_click(self, facts, slot):
            nonlocal cabin
            if not HA.host_save_matches(run, expected_save, self.host_hwnd, window_fn=adapter.window):
                report_hold(run, HA.Decision("hold", "host_save_identity_unconfirmed"), state)
            cabin = True
            return super()._slot_click(facts, slot)

        def separate_window(self, hwnd):
            nonlocal startup
            if owner_word is not None:
                return
            launch = adapter.read_json(R.paths.FARMHAND_LAUNCH) or {}
            pid = (adapter.window(hwnd) or {}).get("pid")
            if (not pid or launch.get("pid") != pid or not launch.get("launch_id") or launch_started_ms is None
                    or launch["launch_id"] == previous_launch.get("launch_id")):
                report_hold(run, HA.Decision("hold", "farmhand_launch_unconfirmed", held=state.hands_held), state)
            startup = dict(pid=pid, hwnd=hwnd, launch_id=launch["launch_id"], issued_ms=launch_started_ms)
            wait_for_join()
            return super().separate_window(hwnd)

    adapter = GuardedAdapter(refs, record.with_suffix(""))
    worker = GuardedRestart(options, adapter, refs, record)

    def host_accepts():
        return HA.host_save_matches(run, expected_save, worker.host_hwnd, window_fn=adapter.window, accepting=True)

    def wait_for_host_accepting():
        """A watchdog launch waits, bounded, for a HOST that can accept her (one that still lists the
        crashed game's farmer cannot); it never launches into a HOST that cannot. Every stop outranks the wait."""
        until = min(deadline, time.time() + HOST_ACCEPT_WAIT_S)
        while not host_accepts():
            if time.time() >= until:
                report_hold(run, HA.Decision("hold", "host_not_accepting", held=state.hands_held), state)
            adapter.sleep(1)
            stop_check(read())

    def wait_for_join():
        nonlocal restored, watchdog_attempted
        while True:
            ev = read()
            stop_check(ev)
            launch = adapter.read_json(R.paths.FARMHAND_LAUNCH) or {}
            window = adapter.window(startup["hwnd"]) or {}
            if (launch.get("launch_id") != startup["launch_id"] or launch.get("pid") != startup["pid"]
                    or window.get("pid") != startup["pid"] or not window.get("exists")):
                raise HA.HandsHeld(HA.Decision("hold", "farmhand_launch_identity_changed", held=state.hands_held))
            candidate = ev.session[1] or {}
            if not startup.get("runtime_instance_id"):
                if (isinstance(candidate, dict) and candidate.get("world_ready") is False
                        and candidate.get("player_id") in (0, "0") and candidate.get("player_name") == ""
                        and type(candidate.get("native_ms")) is int and candidate["native_ms"] >= startup["issued_ms"]
                        and isinstance(candidate.get("runtime_instance_id"), str)
                        and HA.launch_published(launch, candidate)):
                    startup["runtime_instance_id"] = candidate["runtime_instance_id"]
                else:
                    adapter.sleep(1)
                    continue
            decision = HA.decide(ev, act="join", held=state.hands_held, clear_watchdog=not watchdog_attempted)
            if decision.action == "rearm" and not host_accepts():
                # A HOST that cannot accept her yet spends no attempt; the next pass reads every stop and
                # decides again, bounded by the recovery deadline in `stop_check`.
                adapter.sleep(1)
                continue
            if decision.action == "rearm":
                watchdog_attempted = True
                state.hands_held = HA.bind(decision.session, decision.halt, state.hands_held)
                from arti_stardew_stop import StopRefused
                from tools import arti_rearm

                def confirm(native, _notes):
                    fresh = read()
                    stop_check(fresh)
                    authorization = HA.decide(fresh, act="join", held=state.hands_held, clear_watchdog=True)
                    return (native is not None and authorization.action == "rearm"
                            and authorization.expect == decision.expect == tuple(native[k] for k in HA.KEY_FIELDS)
                            and HA.host_save_matches(run, expected_save, worker.host_hwnd, window_fn=adapter.window,
                                                     accepting=True))  # never into a HOST that cannot accept her

                def watchdog_stop_check():
                    fresh = read()
                    stop_check(fresh)
                    if fresh.outranking or fresh.host_inert or fresh.sentinels or fresh.stops:
                        names = [*fresh.outranking, *map(str, fresh.sentinels), *fresh.stops]
                        if fresh.host_inert:
                            names.append("host_inert")
                        report_hold(run, HA.Decision("hold", "stop_outranks_recovery:" + ",".join(names),
                                                      held=state.hands_held, paths=list(map(str, fresh.sentinels))), state)
                    if not HA.host_save_matches(run, expected_save, worker.host_hwnd, window_fn=adapter.window,
                                                accepting=True):  # the tool withdraws its token
                        report_hold(run, HA.Decision("hold", "host_not_accepting", held=state.hands_held), state)

                try:
                    arti_rearm.rearm(folder, "rig_operator", confirm, automatic=True, out=lambda _: None,
                                     sleep=adapter.sleep, wait_s=max(0, min(15, deadline - time.time())),
                                     watchdog_stop_check=watchdog_stop_check,
                                     stop_guards=HA.stop_guards(run, stop_file=stop_file))  # the token names every stop read here
                except (OSError, StopRefused):
                    pass  # the following read continues to hold unless the native receipt actually arrived
                continue
            if restored is not None:
                key = (decision.halt or {}).get("key")
                if HA.restored_receipt(ev, restored):
                    state.hands_held = None
                    restored = None
                    decision = HA.decide(ev, act="join")
                elif (key and key[0] == restored[0] and decision.held
                      and (key[1:] == restored[1:] or key[1] > restored[1])):
                    restored = None  # preserve the native hold, including a newly grown key
                elif key and decision.held and ev.stops:
                    restored = None  # a new native stop became authoritative, so keep its receipt binding
                else:
                    decision = HA.Decision("hold", "restoration_unconfirmed", held=state.hands_held)
            try:
                if decision.action == "hold":
                    report_hold(run, decision, state)
            except HA.HandsHeld:
                adapter.sleep(1)
                continue
            state.hands_held = None
            return decision.session

    if owner_word is not None:
        # Reuse the restart tool's process checks and window wait, with geometry and input disabled.
        owner_join.require_attempt(owner_word, run, slot=slot, parent="worker")
        gate("launch")
        # The cycle that lost her game ends its own trial first; a word never runs beside one.
        until = time.time() + OWNER_TRIAL_WAIT_S
        while adapter.trial_running() is not False:
            if time.time() >= until:
                owner_join.refuse("owner_join_trial_running")
            adapter.sleep(2)
        owner_word = dict(owner_word, worker_pid=os.getpid(), worker_start=owner_join.wf.process_start(os.getpid()))
        previous = owner_join.replaced(HA.read_launch_record(run / owner_join.FARMHAND_LAUNCH)[1]) or {}
        try:
            old_pid, _, host, _ = worker.identify()
            worker.host_hwnd = host
            worker.check_prefs("after_exit", repair=False)
            previous = {}
            pid, hwnd, _ = worker.launch(old_pid, attempt_id=owner_word["id"])
            her_window = True
            return owner_join.wait_for_person(run, folder, owner_word, adapter, pid, hwnd, held=state.hands_held,
                                              save_reader=save_identity_fn, tell=log)
        except HA.HandsHeld as exc:
            owner_join.handoff_held(run, folder, owner_word, exc.decision, tell=log)
            raise
        except R.Exit as exc:
            log(run, "owner_join_launch_failed", error=str(exc))
            owner_join.refuse("owner_join_launch_failed")
        finally:
            if launcher is not None:
                launcher.stdout.close()

    if HA.session_kind(run) == "owner":
        gate("launch")
        owner_join.refuse("owner_join_waits_for_operator")
    previous_launch = adapter.read_json(R.paths.FARMHAND_LAUNCH) or {}
    old_pid = previous_launch.get("pid")
    if old_pid and adapter.alive(old_pid):
        gate("close")
        if adapter.trial_running() is not False:
            report_hold(run, HA.Decision("hold", "farmhand_trial_still_running", held=state.hands_held), state)
        if not expected or not expected.get("runtime_instance_id") or not expected.get("player_id"):
            report_hold(run, HA.Decision("hold", "enrolled_farmhand_identity_unknown", held=state.hands_held), state)
        host = adapter.read_text(R.paths.HOST_HWND_FILE)
        host_window = adapter.window(host) if host else None
        if not host_window or not host_window.get("exists") or host_window.get("pid") == old_pid:
            report_hold(run, HA.Decision("hold", "host_not_joinable", held=state.hands_held), state)

        def ask_bound(*args, **kwargs):
            return sup.ask_game_exit(*args, **kwargs, target_fn=lambda r, n, k: sup.game_exit_target(
                r, n, k, inbox_fn=lambda: folder))

        closed = sup.close_session_games(run, "owner", gate_fn=gate, ask_fn=ask_bound)
        if "farmhand" not in closed:
            report_hold(run, HA.Decision("hold", "farmhand_exit_unconfirmed", held=state.hands_held), state)
    gate("launch")
    old_pid, _, host, _ = worker.identify()
    worker.host_hwnd = host
    worker.check_prefs("after_exit", repair=False)
    pid, hwnd, started = worker.launch(old_pid)
    if not startup or startup["pid"] != pid or startup["hwnd"] != hwnd:
        report_hold(run, HA.Decision("hold", "farmhand_launch_unconfirmed", held=state.hands_held), state)
    wait_for_join()
    worker.join(hwnd, started)
    worker.verify(hwnd, prior.session)
    fresh = wait_for_join()
    if fresh.get("world_ready") is not True or str(fresh.get("player_name")) != slot.removeprefix("name:"):
        report_hold(run, HA.Decision("hold", "joined_farmhand_identity_unknown"), state)
    if expected and expected.get("save_id") is not None:
        if save_identity_fn is None:
            from scripts.m1_operator.live_save import scene_save_identity
            import poll_state

            def save_identity_fn(current):
                diagnostics = {}
                poll_state.sample(5.0, diagnostics=diagnostics)
                return scene_save_identity(diagnostics.get("scene_identity"), current)

        while True:
            try:
                good_save = str(save_identity_fn(fresh)["save_id"]) == str(expected["save_id"])
            except (OSError, ValueError, KeyError, TypeError):
                good_save = False
            after = wait_for_join()
            if good_save and HA.identity(fresh) == HA.identity(after):
                fresh = after
                break
            try:
                report_hold(run, HA.Decision("hold", "native_save_identity_unconfirmed"), state)
            except HA.HandsHeld:
                adapter.sleep(1)
            fresh = after
    bound = {key: fresh[key] for key in (*HA.IDENTITY_FIELDS, "player_id", "player_name")}
    if expected and expected.get("save_id") is not None:
        bound["save_id"] = expected["save_id"]
    origin = read()
    stop_check(origin)
    if origin.newest is not None:
        from arti_runtime import ipc
        ipc.write_json(run / "runtime/farmhand-recovery.json",
                       dict(cycle=origin.newest.name, previous=HA.expected_identity(origin.newest_state[1]),
                            successor=bound, pid=pid, launch_id=startup["launch_id"]), fsync=True)
    return bound


def _recovery_main(raw):
    from dataclasses import asdict
    from scripts.livepath.halt_authority import HandsHeld, RecoveryTimedOut
    try:
        print(json.dumps({"expected": _recover_farmhand(**json.loads(raw))}))
        return 0
    except HandsHeld as exc:
        print(json.dumps({"hold": asdict(exc.decision)}))
        return 2
    except (RecoveryTimedOut, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"timeout": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


def farmhand_name(a, run):
    name = getattr(a, "name", None)
    if not name and (run / "self_facts.json").is_file():
        name = soak.read(run / "self_facts.json").get("name")
    if not name:
        profile, _, _ = creation_facts(run / "buat-karakter.jsonl")
        name = (profile or {}).get("name")
    if not name:
        raise RuntimeError("her farmhand name is unknown: pass --name")
    return name


def rejoin_farmhand(a, *, count_fn=None, sleep_fn=None, clock_fn=None, halt_fn=None, focus_host=None):
    """Re-arm a stale halt and join the SAME farmhand by name. Never starts HOST or writes the save.

    With both games up, hand off focus from the farmhand to the operator console or shell desktop
    if needed. The focus_host argument is retained for existing operator callers.
    """
    run = Path(a.run)
    _check_recovery_deadline(run, a)
    name = farmhand_name(a, run)
    games = _recovery_game_count(a, count_fn)
    from scripts.livepath import halt_authority as HA
    if HA.prepared_owner_run(run):
        return rejoin_owner(a, run, name, games)
    if getattr(a, "owner_ready", False):
        a.owner_ready = False
        report_hold(run, HA.Decision("hold", "owner_join_host_role_unknown"), a)
    if games == "1":
        decision = rearm_halt(run, a, "launch", allow_watchdog=True)
        if decision.sentinels:
            rearm_halt(run, a)
        keep_smapi_logs(run, "before-relaunch")  # ADR 241 C3: the launch rewrites her SMAPI log
        try:
            a.recovery_expected = recover_farmhand(
                run, "name:" + name, held=getattr(a, "hands_held", None),
                expected=HA.read_evidence(inbox(), run).expected, deadline=getattr(a, "recovery_deadline", None))
        except HA.HandsHeld as exc:
            report_hold(run, exc.decision, a)
        a.hands_held = None  # the worker already observed any required receipt on the bound successor
    if games == "2":
        rearm_halt(run, a, "observe")
        released = release_farmhand_focus(run)  # ADR 250: a no-op report when every hand path is focus-independent
        log(run, "focus_release", step="rejoin", ok=bool(released), cause=getattr(released, "cause", None))
        if not released:
            raise RuntimeError("farmhand_window_active")
    sleep_fn = sleep_fn or time.sleep
    clock_fn = clock_fn or time.time
    halt_fn = halt_fn or halt_latched
    deadline = clock_fn() + 30
    while clock_fn() < deadline and not halt_fn():
        _check_recovery_deadline(run, a)
        sleep_fn(2)
    # ADR 254: the table decides every rejoin, also with no file trigger (a HALT only session.json reports holds).
    rearm_halt(run, a)
    log(run, "rejoined", name=name, games_before=games)
    return name


def rejoin_owner(a, run, name, games):
    from scripts.livepath import halt_authority as HA, owner_join

    folder = inbox()
    a.owner_origin = tuple(HA.cycle_dirs(run))
    flagged, issued = getattr(a, "owner_ready", False), getattr(a, "owner_issued", None)
    a.owner_ready, a.owner_issued = False, None  # one word, one attempt: nothing later in this process may use it again
    a.owner_cycle = None
    try:
        a.recovery_expected = None
        if games == "1":
            if not flagged:
                rearm_halt(run, a, "launch", allow_watchdog=True)  # preserve the table's existing reason
                owner_join.refuse("owner_join_waits_for_operator")
            word = owner_join.new_attempt(run, name, issued=issued)
            with owner_join.live_attempt(word["lease"]):
                log(run, "owner_ready", attempt_id=word["id"], run_path=word["run"], mode=word["mode"],
                    selected=word["selected"], host=word["host"])
                owner_join.require_attempt(word, run)
                rearm_halt(run, a, "launch", owner_word=word)
                keep_smapi_logs(run, "before-relaunch")
                a.recovery_expected = recover_farmhand(
                    run, "name:" + name, held=getattr(a, "hands_held", None),
                    expected=HA.read_evidence(folder, run).expected,
                    deadline=getattr(a, "recovery_deadline", None), owner_word=word)
                owner_join.require_attempt(word, run)
        elif games == "2":
            host = owner_join.host_binding(run)
            binding = owner_join.selected(run, name)
            a.recovery_expected = owner_join.verify_joined(run, folder, host, binding,
                                                           held=getattr(a, "hands_held", None))
        else:
            owner_join.refuse("owner_join_games_unknown")
        rearm_halt(run, a)  # apply main's audited end-marker handling to the connected farmhand
        a.owner_cycle = owner_join.running_cycle(run)
        if a.owner_cycle is not None:
            owner_join.hand_over(run, a.owner_cycle, a.recovery_expected)
    except HA.HandsHeld as exc:
        report_hold(run, exc.decision, a)
    except HA.RecoveryTimedOut:
        report_hold(run, HA.Decision("hold", "owner_join_attempt_timed_out"), a)
    a.hands_held = None
    log(run, "rejoined", name=name, games_before=games,
        running_cycle=a.owner_cycle.name if a.owner_cycle is not None else None)
    return name


def release_helper_pause(run):
    """A helper HOST holds the world paused at load (ADR 65 startup gate) so nothing moves the HOST farmer during
    her join; release it once she is in, exactly as soak does after launch-farmhand. No-op without a gated helper."""
    run = Path(run)
    cfg_path = run / "runtime/batch-host/host-mods/ArtiHostHelper/config.json"
    if not cfg_path.is_file() or int(soak.read(cfg_path).get("StartupPauseSeconds") or 0) <= 0:
        return None
    state = (soak.read(run / "host/startup.json") if (run / "host/startup.json").is_file() else {}).get("state")
    if state != "held":
        # 11-15 14:08: a second rejoin on the same HOST found the gate already released and failed
        # (startup_morning_window_expired). Only a held gate is released.
        log(run, "helper_pause_not_held", state=state)
        return None
    row = soak.startup_pause(run / "host", soak.read(cfg_path)["RunId"], release=True, timeout_s=30)
    log(run, "helper_pause_released", state=row.get("state"), time=row.get("time"))
    return row


def rejoin(a, *, count_fn=None):
    """Her game died or left: re-arm a stale native halt (typed confirm, as the operator tool requires), rejoin the
    owner's farm as the SAME farmhand by name, then a fresh Live7 cycle. Her mind dir and the farm save are kept."""
    games = _recovery_game_count(a, count_fn)
    # Both games up: rejoin launches nothing and is never memory-gated (11-15: 90% blocked a no-launch rejoin).
    # Any game up during rejoin skips disk-active sampling so preflight adds no load.
    missing = object()
    prev_skip_disk = getattr(a, "skip_disk_active", missing)
    a.skip_disk_active = bool(getattr(a, "skip_disk_active", False) or games != "0")
    try:
        blocked_preflight = preflight(a, skip_memory=(games == "2"), launch="farmhand")
    finally:
        if prev_skip_disk is missing:
            delattr(a, "skip_disk_active")
        else:
            a.skip_disk_active = prev_skip_disk
    if blocked_preflight:
        return 1
    _check_recovery_deadline(a.run, a)
    a.recovery = True
    rejoin_farmhand(a)
    _check_recovery_deadline(a.run, a)
    if getattr(a, "owner_cycle", None) is not None:
        log(a.run, "rejoined_running_cycle", cycle=a.owner_cycle.name,
            message="Her running cycle (%s) goes on with her; no second cycle is started." % a.owner_cycle.name)
        return 0
    if not (getattr(a, "recovery_expected", None) or {}).get("owner_host"):
        release_helper_pause(a.run)
    _check_recovery_deadline(a.run, a)
    return play(a, cycle=next_cycle(Path(a.run)))


def keep_smapi_logs(run, label):
    """ADR 241 C3: copy both live SMAPI logs into the run before a game is closed or relaunched, and at a stop. SMAPI
    rewrites SMAPI-latest*.txt at every launch, and the cycle copies them only when an attempt starts, so the tail of a
    session (a stuck window, the last night) is otherwise lost at the next launch. Read through a shared handle (SMAPI
    keeps the file open). Never raises: returns the folder, or None when neither log could be read."""
    from arti_runtime import ipc
    dest = Path(run) / "evidence" / ("smapi-%s-%s" % (label, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())))
    kept = []
    for name in ("SMAPI-latest.player-2.txt", "SMAPI-latest.txt"):  # her farmhand, then the HOST
        try:
            with ipc.open_shared(APPDATA / "ErrorLogs" / name, "rb") as source:
                data = source.read()
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(data)
            kept.append(name)
        except OSError:
            continue
    return dest if kept else None


def leave(a):
    from scripts.livepath.she_leaves import leave as run_leave
    return run_leave(a)


def stop(a):
    run = Path(a.run)
    keep_smapi_logs(run, "stop")  # ADR 241 C3: the session's last stretch of both logs stays with the run
    from scripts.livepath import owner_join
    owner_join.stop_notice(run, inbox, log)
    cycle = current_cycle(run)
    if cycle.exists():
        subprocess.run([sys.executable, "-B", str(ROOT / "scripts/m1_operator/live_cycle.py"), "stop",
                        "--cycle-dir", str(cycle), "--yes"], cwd=ROOT, env=env_for_tools(run), timeout=120)
    (run / "bridge-stop-request").touch()
    try:
        # Owner 2026-11-11: every graceful end is remembered without a command. The note goes into the live vault
        # (create-if-absent); the Discord side is ARTI-DiaryAuto's, so no diary rows are written here.
        remember(a)
        auto_export_note(run)
    except Exception as exc:  # noqa: BLE001 - stopping must not fail on the memory note
        log(run, "remember_failed", error=type(exc).__name__, detail=str(exc)[:200])
    log(run, "stop_requested", next="bridge drains then exits; games stay up for the owner; `restore` after closing them")


def say(a):
    """Smoke only: one typed owner line to the running bridge's stdin (the rehearsal's scripted-owner path).
    ``--bungkam``: the owner's double toggle as a scripted line, only into a rehearsal run (the bridge acts on it
    only under the scripted-PTT conditions; on any other run it would be a typed line she answers)."""
    if getattr(a, "bungkam", False):
        cfg_path = Path(a.run) / "rehearsal-config.json"
        try:
            cfg = soak.read(cfg_path) if cfg_path.is_file() else {}
        except (OSError, ValueError):
            cfg = {}
        if cfg.get("rehearsal_scripted_ptt") is not True:
            raise SystemExit("say --bungkam: only into a --rehearsal-scratch run (rehearsal_scripted_ptt in its config)")
        from arti_runtime.rehearsal_bungkam import COMMAND
        with (Path(a.run) / "owner-say.txt").open("a", encoding="utf-8") as f:
            f.write(COMMAND + "\n")
        log(a.run, "owner_bungkam_sent")
        return
    with (Path(a.run) / "owner-say.txt").open("a", encoding="utf-8") as f:
        f.write(a.text.replace("\n", " ").strip() + "\n")
    log(a.run, "owner_line_sent", chars=len(a.text))


def run_save(run, saves=None):
    """This run's save folder name: the continued save, else the save whose farm name her verified facts name."""
    run = Path(run)
    continued = soak.read(run / "continued.json") if (run / "continued.json").is_file() else None
    save = (continued or {}).get("save")
    if not save and (run / "self_facts.json").is_file():
        m = re.search(r"Farm ini bernama (.+?) Farm\.", " ".join(soak.read(run / "self_facts.json").get("lines") or ()))
        if m:
            for folder in sorted((Path(saves) if saves else APPDATA / "Saves").glob("*_*")):
                try:
                    if saved_farm(folder)["farm"] == m.group(1):
                        save = folder.name
                        break
                except (RuntimeError, OSError, ValueError):
                    continue
    return save


def run_scope_hashes(run, *, current=True, saves=None, inbox_dir=None):
    """This run's farm: the continued save's rings, else the save whose farm name her verified facts name; the
    newest occurrence ring only for the run being stopped now. ``saves`` / ``inbox_dir``: the save folder and the
    mod inbox (default: this machine's), for a tool that reads another set."""
    hashes = set()
    save = run_save(run, saves)
    if save:
        hashes |= farm_scope_hashes(save, inbox_dir or inbox())
    scope = occurrence_scope(inbox_dir or inbox()) if current else None
    if scope:
        hashes.add(hashlib.sha256(scope.encode()).hexdigest())
    return hashes


def run_farm_name(run, continued=None):
    """This run's farm display name: the continued save's farm, else the farm her verified self facts name; None
    before either exists (a new farm is named only after her farmer is made)."""
    from arti_runtime import vault_farm
    run = Path(run)
    if continued is None and (run / "continued.json").is_file():
        try:
            continued = soak.read(run / "continued.json")
        except (OSError, ValueError):
            continued = None
    name = vault_farm.clean_name((continued or {}).get("farm")) if isinstance(continued, dict) else None
    return name or vault_farm.self_facts_farm(run / "self_facts.json")


def remember(a, *, current=True):
    """Verified facts of this run -> vault note + diary drafts, in the run folder only (scripts/livepath/mabar_memory)."""
    from arti_runtime import vault_farm
    from scripts.livepath import mabar_memory
    run = Path(a.run)
    hashes = run_scope_hashes(run, current=current)
    # ADR 238: the note says which farm it is from (the live profile's setting; no farm known = no tag).
    tag = vault_farm.tag_line(run_farm_name(run), hashes) if LIVE_BRIDGE.get(vault_farm.SETTING) else None
    result = mabar_memory.build(run, MIND, scope_hashes=hashes or None, farm_tag=tag)
    log(a.run, "remembered", farm_scoped=bool(hashes), farm_tagged=bool(tag), **result)
    return 0


def auto_export_note(run, stream=None):
    """The run's vault note into the live vault (create-if-absent), no diary rows; logged once per run."""
    from scripts.livepath import mabar_memory
    result = mabar_memory.export_to_live(Path(run), Path(stream or STREAM), diary=False)
    log(run, "auto_exported", note=result.get("note"), note_target=Path(result.get("note_target", "")).name)
    return result


# Owner 2026-11-11 rule start: older runs (smokes, test farms, 11-09/11-10) are never back-filled automatically.
CATCH_UP_FROM = "2026-11-11T00:00:00Z"


def catch_up_notes(current_run, runs_root=None, stream=None):
    """A session that did not end gracefully (crash, machine hang) has no note: build it from that run's verified
    records and put it into the live vault. Only real mabar runs (mabar-*) since the rule start, never this run."""
    runs_root = Path(runs_root or RIG / "mabar-runs")
    done = []
    for run in sorted(p for p in runs_root.glob("mabar-*") if p.is_dir()):
        if run.resolve() == Path(current_run).resolve() or not (run / "MODE.json").is_file():
            continue
        mode = soak.read(run / "MODE.json")
        if mode.get("mode") != "mabar" or str(mode.get("utc") or "") < CATCH_UP_FROM:
            continue
        events = []
        if (run / "mabar.jsonl").is_file():
            for line in (run / "mabar.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line).get("event"))
                except ValueError:
                    continue
        if "auto_exported" in events or "exported_to_live" in events:
            continue
        try:
            remember(argparse.Namespace(run=run), current=False)
            result = auto_export_note(run, stream)
            log(current_run, "caught_up_note", session_run=run.name, note=result.get("note"))
            done.append(run.name)
        except Exception as exc:  # noqa: BLE001 - catch-up must never block prepare
            log(current_run, "catch_up_failed", session_run=run.name, error=type(exc).__name__)
    return done


def export(a):
    """Owner OK only: the remembered note/diary into live ARTI's vault and Discord diary (create-if-absent / append)."""
    from scripts.livepath import mabar_memory
    if not a.to_live:
        raise RuntimeError("export writes into stream-folder: pass --to-live (owner OK)")
    result = mabar_memory.export_to_live(Path(a.run), STREAM)
    log(a.run, "exported_to_live", **result)
    return 0


def _event_counts(run):
    host_left = rejoins = 0
    started = None
    path = Path(run) / "mabar.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ev = row.get("event")
            if ev == "host_left":
                host_left += 1
            elif ev in ("rejoined", "auto_rejoined"):
                rejoins += 1
            elif ev == "cycle_started" and started is None:
                started = row.get("utc")
    return host_left, rejoins, started


def _leg_minutes(state, started_utc, now=None):
    now = time.time() if now is None else now
    at = state.get("started_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return max(0, int((now - at) / 60))
    if started_utc:
        try:
            from datetime import datetime, timezone
            t = datetime.fromisoformat(str(started_utc).replace("Z", "+00:00"))
            return max(0, int((now - t.timestamp()) / 60))
        except (TypeError, ValueError):
            return None
    return None


def _game_day(state):
    days = state.get("days") or []
    if not days:
        return None
    current = days[-1]
    date = current.get("next_date") or current.get("date")
    if isinstance(date, dict):
        return dict(year=date.get("year"), season=date.get("season"), day=date.get("day"))
    return None


def _voice_lane_status(run):
    """Counts only: API vs fallback replies and USD spent vs cap. No reply text."""
    cfg_path = Path(run) / "rehearsal-config.json"
    if not cfg_path.is_file():
        return None
    try:
        cfg = soak.read(cfg_path)
    except (OSError, ValueError):
        return None
    if not cfg.get("voice_api_enabled"):
        return None
    session_path = Path(cfg.get("voice_api_session_state_file") or (Path(run) / "voice-api-session.json"))
    spent, cap = None, cfg.get("voice_api_session_cap_usd")
    if session_path.is_file():
        try:
            sess = json.loads(session_path.read_text(encoding="utf-8"))
            spent = sess.get("spent_usd")
        except (OSError, ValueError):
            spent = None
    api = fallback = 0
    tel = Path(run) / "telemetry" / ("%s.jsonl" % Path(run).name)
    if tel.is_file():
        try:
            for line in tel.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                src = row.get("source") or row.get("reply_source")
                if src in ("openai", "openai_api", "api"):
                    api += 1
                elif src in ("codex", "cursor"):
                    fallback += 1
        except OSError:
            pass
    return dict(api=api, fallback=fallback, spent_usd=spent, cap_usd=cap)


def status_payload(a, *, memory_fn=None, now=None):
    run = Path(a.run)
    cycle = current_cycle(run) / "cycle_state.json"
    state = soak.read(cycle) if cycle.is_file() else {}
    relay = run / "host_story.jsonl"
    host_left, rejoins, started = _event_counts(run)
    from scripts.livepath.bringup import memory_commit_percent
    try:
        mem = (memory_fn or memory_commit_percent)()
    except Exception:
        mem = None
    row = dict(cycle=state.get("status"), bridge=bool(bridge_pids()), vts=vts_reachable(),
               relay_lines=sum(1 for _ in relay.open(encoding="utf-8")) if relay.is_file() else 0,
               drained=(run / "bridge-drained").is_file(), c_free_gib=round(free_gib(), 1),
               ssn_chat=soak.read(run / "ssn_status.json") if (run / "ssn_status.json").is_file() else None,
               leg_minutes=_leg_minutes(state, started, now=now),
               game_day=_game_day(state),
               host_left=host_left, rejoins=rejoins,
               memory_commit_pct=None if mem is None else round(float(mem), 1))
    voice = _voice_lane_status(run)
    if voice:
        row["voice_lanes"] = voice
    return row


def status(a, **hooks):
    from scripts.livepath.mabar_supervise import gather_status
    for line in live_profile_lines():  # the profile in force, every time; stderr: stdout stays the one operator line
        print(line, file=sys.stderr)
    print(gather_status(Path(a.run), **hooks))
    from scripts.livepath.mabar_solo import status_extras
    extra = status_extras(Path(a.run))
    if extra:
        print(extra)


def supervise(a):
    from scripts.livepath.mabar_supervise import run_supervise
    return run_supervise(a)


def solo(a):
    from scripts.livepath.mabar_solo import start_solo, run_solo
    if getattr(a, "solo_worker", False):
        return run_solo(a)
    return start_solo(a)


def chat(a):
    from scripts.livepath.mabar_solo import hand_chat_id
    return hand_chat_id(a)


STEPS = dict(backup=backup, prepare=prepare, preflight=preflight, voice=voice, host=host, join=join, play=play,
             leave=leave, stop=stop, restore=restore, status=status, say=say, remember=remember, export=export,
             rejoin=rejoin, supervise=supervise, solo=solo, chat=chat)
STEPS.update({"link-runtime": link_runtime, "unlink-runtime": unlink_runtime, "memory-report": memory_report})


def apply_rehearsal_scratch(a):
    """Rehearsal only (2026-11-18): her mind, her vault recall and its index live in a scratch folder, and scripted
    owner lines (`say`) enter as PTT. Refused with any live chat (a rehearsal has no audience) and for a folder that
    is, holds or sits inside her real stores. Every step of the run must carry the same --rehearsal-scratch."""
    global MIND, VAULT_ROOT, VAULT_INDEX, REHEARSAL_SCRIPTED_PTT
    if (getattr(a, "youtube_video_id", None) or getattr(a, "video_id", None) or getattr(a, "ssn_from_app", False)
            or getattr(a, "ssn_session_file", None)):
        raise RuntimeError("--rehearsal-scratch refuses live chat: a rehearsal has no audience")
    root = Path(a.rehearsal_scratch).resolve()
    for real in (MIND, STREAM, VAULT_INDEX.parent):
        real = Path(real).resolve()
        if root == real or real in root.parents or root in real.parents:
            raise RuntimeError("--rehearsal-scratch must be outside her real stores: %s" % real)
    MIND, VAULT_ROOT, VAULT_INDEX = root / "mind", root / "vault", root / "vault-index" / "vault_rag.db"
    for d in (MIND, VAULT_ROOT, VAULT_INDEX.parent):
        d.mkdir(parents=True, exist_ok=True)
    REHEARSAL_SCRIPTED_PTT = True
    return root


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "_recover-farmhand":
        return _recovery_main(args[1])
    if args == ["_recover-farmhand"]:
        return _recovery_main(sys.stdin.read())
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=sorted(STEPS))
    p.add_argument("--run", type=Path, default=None, help="e.g. ARTI\\operator-staging\\run\\mabar-20261109")
    p.add_argument("--spec", type=Path, default=RIG / "livepath-booking.json")
    p.add_argument("--relay", action="store_true", help="enable the HOST story relay in the HOST mod config")
    p.add_argument("--no-vts", action="store_true")
    p.add_argument("--youtube-video-id", default=None,
                   help="voice: read this YouTube live chat into her (unproven live on this path)")
    p.add_argument("--video-id", dest="video_id", default=None,
                   help="solo/chat: YouTube live video id (chat hands it to the running bridge)")
    p.add_argument("--farm", dest="continue_save", default=argparse.SUPPRESS, metavar="SAVE_FOLDER",
                   help="solo: the co-op save folder (alias of --continue)")
    p.add_argument("--owner-vts", choices=("keep", "off"), default="keep",
                   help="solo: never close the owner's VTS; off = report the window count only")
    p.add_argument("--solo-worker", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--ssn-session-file", default=None,
                   help="voice: file holding the Social Stream Ninja session id (YouTube+TikTok+Instagram chat)")
    p.add_argument("--ssn-from-app", action="store_true",
                   help="voice: use the session id of the SSN desktop app on this machine")
    p.add_argument("--helper", action="store_true",
                   help="host: the ArtiHostHelper runs the HOST (load the --continue save, host, go to bed): solo stream")
    p.add_argument("--relaunch", action="store_true",
                   help="host: move used batch-host/host aside, prepare a fresh batch-host, then launch")
    p.add_argument("--launch", choices=("farmhand", "host", "voice"), default=None,
                   help="preflight: next-launch budget for the GB headroom gate")
    p.add_argument("--continue", dest="continue_save", default=None, metavar="SAVE_FOLDER",
                   help="backup/prepare: the existing co-op save folder to protect and use for this session")
    p.add_argument("--classic", action="store_true",
                   help="voice: accepted no-op; desk is off by default (the 11-10 lane)")
    p.add_argument("--desk", action="store_true",
                   help="voice: closed rehearsal only; attention desk, own-play speech, human-preserving queue")
    p.add_argument("--alert-yield", action="store_true",
                   help="voice: an alert waits for the end of her current sentence instead of a hard TTS cut")
    p.add_argument("--api-voice", default=None, metavar="MODEL",
                   help="voice: OpenAI Responses API first (Codex/Cursor fallback); key from the secrets .env in-process")
    p.add_argument("--voice-cap-usd", type=float, default=0.50,
                   help="voice: --api-voice session budget in USD; Codex/Cursor fallback at the cap")
    p.add_argument("--own-play", action="store_true",
                   help="voice: own-play commentary without the attention desk; 45s gap, never over owner/alerts/chat")
    p.add_argument("--no-alerts", action="store_true",
                   help="voice: keep donation/mediashare off (closed rehearsal)")
    p.add_argument("--vts-port", type=int, default=VTS_PORT,
                   help="ARTI's VTS API port: 8002 = second instance ('Window 2'); 8001 when hers is the only VTS")
    p.add_argument("--passive", action="store_true", help="passive mic replies (only with her voice on headphones)")
    p.add_argument("--mic", type=int, default=None)
    p.add_argument("--decision-cap-usd", type=float, default=DECISION_CAP_USD,
                   help="game API session budget; Codex fallback at cap")
    p.add_argument("--owner-hint", default=None, help="what the owner said about her look, if anything")
    p.add_argument("--text", default="", help="say: the typed owner line")
    p.add_argument("--bungkam", action="store_true",
                   help="say, rehearsal runs only: the owner's double toggle (stops her turn) as a scripted line")
    p.add_argument("--to-live", action="store_true", help="export: write into stream-folder vault/diary (owner OK)")
    p.add_argument("--name", default=None, help="rejoin: her farmhand name (default: her verified readback name)")
    p.add_argument("--owner-ready", action="store_true",
                   help="rejoin: pass once when the owner says his selected farm is hosted and she may join")
    p.add_argument("--rehearsal-scratch", type=Path, default=None,
                   help="rehearsal only: scratch mind + vault + index here; scripted `say` lines enter as PTT; "
                        "refused with live chat")
    p.add_argument("--runtime-from", type=Path, default=None,
                   help="link-runtime: the checkout that has venv, venv312 and ARTI_SOUL.md (default: this checkout)")
    p.add_argument("--auto-rejoin", action="store_true",
                   help="play/rejoin: after host_left, wait for a joinable HOST and rejoin (default OFF)")
    p.add_argument("--auto-rejoin-tries", type=int, default=12,
                   help="play/rejoin: max auto-rejoin attempts (default 12)")
    p.add_argument("--auto-rejoin-minutes", type=float, default=30,
                   help="play/rejoin: max minutes to wait for a joinable HOST (default 30)")
    p.add_argument("--expected-native", dest="recovery_expected", type=json.loads, help=argparse.SUPPRESS)
    p.add_argument("--from-backup", default=None,
                   help="restore: the pre-mabar backup folder (the night's FIRST mabar backup), default this run's own")
    a = p.parse_args(argv)
    if a.owner_ready:
        if a.step != "rejoin":
            p.error("--owner-ready is for rejoin only")
        from scripts.livepath.owner_join import time as owner_clock
        a.owner_issued = owner_clock.monotonic()
    a.argv = list(sys.argv[1:] if argv is None else argv)
    if getattr(a, "rehearsal_scratch", None):
        apply_rehearsal_scratch(a)
    if a.step == "solo" and not a.solo_worker:
        return STEPS["solo"](a) or 0
    if a.run is None:
        p.error("--run is required")
    a.run = a.run.resolve()
    a.run.mkdir(parents=True, exist_ok=True)
    if a.step in ("rejoin", "play"):
        from scripts.livepath.halt_authority import HandsHeld, halt_hold, operator_stop, prepared_owner_run
        flagged = a.owner_ready
        if argv is None and a.step == "rejoin" and prepared_owner_run(a.run):
            from scripts.livepath.owner_join import quiet_console
            if (rc := quiet_console(["-B", str(Path(__file__).resolve()), *a.argv])) is not None:
                return rc
        while True:
            try:
                return STEPS[a.step](a) or 0
            except HandsHeld as exc:
                # A word is one attempt, and a refused join needs a person. A lock is waited out as anywhere else.
                if (operator_stop(exc.decision) or flagged or exc.decision.reason.startswith("owner_join_")
                        or (prepared_owner_run(a.run) and not halt_hold(exc.decision))):
                    return 2
                time.sleep(1)
    return STEPS[a.step](a) or 0


if __name__ == "__main__":
    sys.exit(main())
