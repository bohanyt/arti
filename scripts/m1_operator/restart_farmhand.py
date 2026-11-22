"""restart_farmhand.py: A6, one command to close -> (deploy) -> launch -> join -> verify the operator's farmhand.

  restart_farmhand.py [--deploy [PATH_TO_DLL]] [--preset v1|v2-pad|v2-probe] [--dismiss-levelup] [--no-close]
                      [--dry-run] [--timeout-exit 120] [--timeout-join 180] [--refs DIR] [--click NAME=X,Y ...]
                      [--no-neutral-cursor] [--repair-startup-prefs] [--stage SCRIPT [--stage-args "..."]]

Steps (each logged to stdout and RUN/restart-<utc>.json; screenshots in RUN/restart-<utc>/):
  1 identify  RUN/fh-hwnd.txt + RUN/farmhand-launch.json: IsWindow, window pid == launch pid, exe StardewModdingAPI.exe,
              title contains "running SMAPI"; the HOST hwnd/pid differ. Refuses (exit 2) while a trial runner
              (`prep_trial.py run` / `stardew_m1_trial.py`) is running, or when anything does not match.
              Pre-flight (PLAN §1.5 a): startup_preferences (m1_paths.STARTUP_PREFS) clientOptions/gamepadMode (the
              farmhand's Game1.options, written by SaveGameMenu.cs:161, reloaded by FarmhandMenu.cs:302) must not be
              ForceOn: refused (exit 2) unless --repair-startup-prefs (owner-approved): after the farmhand has exited,
              back up the file to PREP/startup-prefs-backup-<utc>-<ms> and, inside clientOptions ONLY, set gamepadMode
              ForceOn -> Auto, gamepadControls -> false, snappyMenus -> --repair-snappy-menus (default true, the game
              default, Options.cs:715); BOM kept, re-read. The top-level gamepadMode is the owner's own options-page
              choice (Options.cs:1112): reported, never refused, never edited. Checked again after the exit.
  2 close     WM_CLOSE to that hwnd only (`ui_op.py close --expect-pid`). Never TerminateProcess; there is no kill flag.
  3 wait      OpenProcess(SYNCHRONIZE) + WaitForSingleObject up to --timeout-exit, then farmhand-exit.json must name the
              old pid. SMAPI keeps the mod DLL locked until the process is gone (live 2026-10-07 03:43Z).
  4 deploy    (--deploy) refused while any StardewModdingAPI.exe runs; waits until the installed DLL opens for write
              (5 x 2 s), runs `prep_trial.py deploy --dll PATH` (retried on a sharing violation) and checks the new
              deploy-record: after.ArtiStardewBridge.dll.sha256 == sha256(PATH).
  5 launch    `prep_trial.py launch farmhand`; new pid in farmhand-launch.json (<= 30 s); its "running SMAPI" window
              (<= 90 s, or up to 240 s while its own SMAPI log keeps growing: ADR 242); RUN/fh-hwnd.txt is rewritten at once (old value kept in the record); the window is moved off
              the HOST's rect (`ui_op.py pos`, default HOST rect + 40,40; --rect X,Y,W,H or --rect none).
  6 join      every click is gated on a PrintWindow screen match (restart_refs/screens.json): title (stable over two
              shots) -> Co-op -> co-op menu -> "Join LAN Game..." -> IP screen (field empty: click it, type 127.0.0.1)
              -> OK -> slot list -> artihand. After each click the next screen must appear within 20 s, else the same
              click is retried once, else exit 5 (screenshots kept).
              Title hover (probe run 2026-10-08: LOAD hovered, score 0.072 > 0.06): before title shots (at most every
              10 s) the farmhand is foregrounded (verified) and the cursor moved to screens.json `neutral_cursor`
              (off every button; --no-neutral-cursor skips it), and a second spec `title_hover` scores the same
              button row with the worst of its 4 button columns dropped (hovered button + cursor), restart_refs/README.md.
  6b join-slot (--join-slot new | name:NAME, Live7) the slot list is confirmed from the MOD's own join facts instead of a
              reference crop (the crop shows a save-specific name; a new world shows "New Farmhand"): after IP OK the
              tool waits for COMMAND_DIR/join_screen.json (written pre-world by the mod with CharacterCreationEnabled) to
              show the FarmhandMenu with its slots loaded (not connecting / approving / timed out), picks the first
              unclaimed slot (new: not customized, not another player's) or ARTI's named one, and clicks the centre of
              that slot's native button (UI rect x ui_scale); refused unless slot 0's scaled button contains the known
              screens.json "slot" click (geometry sanity). A host still in its intro keeps the list waiting
              (Client_WaitForHostAvailability): bounded by --timeout-slots.
  7 verify    title gets "[farmhand]"; session.json has a NEW session_id and world_ready; a pose read (port, allowed:
              no trial runs) gives location / is_player_free / active_menu. LevelUpMenu: with --dismiss-levelup
              screenshot -> find the Level Up banner AND its OK button (template search; the profession chooser has
              no OK button and is never clicked) -> click OK -> re-read (repeat for queued level-ups), else exit 6.
  7b charcreate (--join-slot new) no pose exists yet (the hello waits for a name): one inspect_context through the
              command inbox must show the menu CharacterCustomization in ui_pad context char_custom, i.e. the game
              opened the farmhand character creation for the unclaimed cabin (Client.setUpGame); exit 0 = ready for
              buat_karakter.py. Anything else is exit 6 (left for the CT).
  8 stage     (--stage SCRIPT) after a verified join: run SCRIPT (.py with this python, .sh with Git Bash) with
              --stage-args (shlex-split); rc != 0 -> exit 8 (e.g. `--stage stage_save.py --stage-args "run-script
              FILE --target farmhand"`).
  finally     `ui_op.py fg <HOST>` (also on failure; not in --dry-run) and the record is written.

Exit codes: 0 rejoined | 2 refused (provenance/precondition, startup_preferences ForceOn) | 3 launch/window not found |
            4 exit timeout | 5 join UI timeout | 6 post-join menu left for the CT | 7 deploy failed | 8 stage failed.
--dry-run: identify only (read-only: files, user32, process names, no port), print every command; sends nothing.
Screen matching needs Pillow (the project venv). Never run during a trial.
"""
from __future__ import annotations

import sys as _ipc_sys
from pathlib import Path as _IpcPath
_ipc_sys.path.insert(0, str(_IpcPath(__file__).resolve().parents[2]))
from arti_runtime import ipc
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import m1_paths as paths  # noqa: E402
import win_facts as wf  # noqa: E402

SMAPI_EXE = "StardewModdingAPI.exe"
REFS = Path(HERE) / "restart_refs"
SEQ = (("title", "coop"), ("coop", "join_lan"), ("ip", "ip_ok"), ("select", "slot"))
NEXT_WAIT_S = 20.0
SLOTS_WAIT_S = 120.0          # --join-slot: a host still in its intro keeps the FarmhandMenu waiting (GameServer.cs:452-463)
JOIN_FACTS_FRESH_S = 5.0      # join_screen.json is rewritten every 30 ticks while the world is not ready
JOIN_SLOT_NEW = "new"
WORLD_WAIT_S = 60.0
POLL_S = 1.5


# The ceiling leaves room to join inside the HOST's 300 s startup pause (soak.py helper StartupPauseSeconds=300).
WINDOW_BASE_S, WINDOW_GROWTH_S, WINDOW_CEILING_S = 90, 20, 240


def window_wait_state(elapsed, alive, log_grew_ago):
    """ADR 242 follow-up: wait (inside the base), growing (past it, log grew lately), dead, stalled or ceiling."""
    if not alive:
        return "dead"
    if elapsed < WINDOW_BASE_S:
        return "wait"
    if elapsed >= WINDOW_CEILING_S:
        return "ceiling"
    if log_grew_ago is not None and log_grew_ago <= WINDOW_GROWTH_S:
        return "growing"
    return "stalled"


class Exit(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- screen matching (Pillow)
def load_refs(ref_dir=REFS):
    cfg = json.loads(ipc.read_text(Path(ref_dir) / "screens.json", encoding="utf-8"))
    cfg["dir"] = str(ref_dir)
    return cfg


def _mad(a, b):
    from PIL import ImageChops, ImageStat
    if a.size != b.size:
        return 1.0
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3 / 255


def _crop(img, client, box):
    ox, oy = client[0], client[1]
    return img.crop((box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy))


def _spec_score(crop, ref, spec):
    """Plain MAD, or (spec `cols` + `drop_worst`) the mean MAD of the columns left after dropping the worst ones: a
    hovered title button (brighter, enlarged) and the cursor drawn on it change one column only."""
    cols = spec.get("cols")
    if not cols:
        return _mad(crop, ref)
    if crop.size != ref.size:
        return 1.0
    h = crop.size[1]
    per = sorted(_mad(crop.crop((x0, 0, x1, h)), ref.crop((x0, 0, x1, h))) for x0, x1 in cols)
    keep = per[:max(1, len(per) - int(spec.get("drop_worst", 1)))]
    return sum(keep) / len(keep)


def classify(png, client, refs):
    """(best screen or 'unknown', {spec: score}, ip_field 'filled'|'empty'|None). client = [x0,y0,x1,y1] of the
    client area inside the PNG (as printed by `ui_op.py shot`). A spec may name its screen (`"screen": "title"` for
    the hover-tolerant `title_hover`), so several specs can vote for one screen."""
    from PIL import Image
    img = Image.open(png).convert("RGB")
    scores = {}
    for name, s in refs["screens"].items():
        ref = Image.open(Path(refs["dir"]) / s["ref"]).convert("RGB")
        scores[name] = round(_spec_score(_crop(img, client, s["box"]), ref, s), 4)
    ok = {n: v for n, v in scores.items() if v <= refs["screens"][n]["max_mad"]}
    top = min(ok, key=ok.get) if ok else None
    best = refs["screens"][top].get("screen", top) if top else "unknown"
    field = None
    if best == "ip":
        f = refs["ip_field"]
        crop = _crop(img, client, f["box"])
        full = _mad(crop, Image.open(Path(refs["dir"]) / f["filled"]).convert("RGB"))
        empty = _mad(crop, Image.open(Path(refs["dir"]) / f["empty"]).convert("RGB"))
        field = "filled" if full <= f["max_mad"] and full < empty else "empty"
    return best, scores, field


def find_template(png, client, tpl_path, region, max_mad):
    """Template search (numpy; coarse step 4, then +-3 px) of tpl inside region (client coords) of a window PNG.
    Returns {found, score, center:[x,y] client}."""
    import numpy as np
    from PIL import Image
    tpl = np.asarray(Image.open(tpl_path).convert("RGB"), dtype=np.int16)
    img = np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)
    x0, y0, x1, y1 = region
    reg = img[y0 + client[1]: y1 + client[1], x0 + client[0]: x1 + client[0]]
    th, tw = tpl.shape[:2]
    if reg.shape[0] < th or reg.shape[1] < tw:
        return dict(found=False, score=None, center=None)

    def score(y, x):
        return float(np.abs(reg[y:y + th, x:x + tw] - tpl).mean()) / 255

    _, by, bx = min((score(y, x), y, x) for y in range(0, reg.shape[0] - th + 1, 4)
                    for x in range(0, reg.shape[1] - tw + 1, 4))
    s, y, x = min((score(y, x), y, x) for y in range(max(0, by - 3), min(reg.shape[0] - th, by + 3) + 1)
                  for x in range(max(0, bx - 3), min(reg.shape[1] - tw, bx + 3) + 1))
    return dict(found=s <= max_mad, score=round(s, 4), center=[x0 + x + tw // 2, y0 + y + th // 2])


def find_levelup(png, client, refs):
    """LevelUp check by template search: the "Level Up" banner AND the OK button. The OK button is drawn only on a
    plain level-up, never on the level 5/10 profession chooser (LevelUpMenu.draw), and both move with the menu height
    (fh-lv1 OK at client (1156,504), fh-b07 at (1156,527)), so a fixed click is unsafe.
    The OK must also sit at the expected offset from the banner (screens.json ok_offset_x/y), so an OK-like sprite
    elsewhere (e.g. inside the profession chooser) is never taken for it.
    Returns {levelup, dismissable, ok_center, heading, ok, ok_offset_ok}."""
    spec, d = refs["levelup"], Path(refs["dir"])
    head = find_template(png, client, d / spec["heading"], spec["heading_region"], spec["max_mad"])
    ok = dict(found=False, score=None, center=None)
    offset_ok = False
    if head["found"]:
        ok = find_template(png, client, d / spec["ok"], spec["ok_region"], spec.get("ok_max_mad", spec["max_mad"]))
        if ok["found"]:
            dx, dy = ok["center"][0] - head["center"][0], ok["center"][1] - head["center"][1]
            ok["offset"] = [dx, dy]
            ox, oy = spec.get("ok_offset_x", [-9999, 9999]), spec.get("ok_offset_y", [-9999, 9999])
            offset_ok = ox[0] <= dx <= ox[1] and oy[0] <= dy <= oy[1]
    dismissable = head["found"] and ok["found"] and offset_ok
    return dict(levelup=head["found"], dismissable=dismissable, ok_center=ok["center"] if dismissable else None,
                heading=head, ok=ok, ok_offset_ok=offset_ok)


class LevelUpRefused(Exception):
    pass


def dismiss_levelups(ad, hwnd, log, first_pose=None, max_n=4):
    """Dismiss plain LevelUp screens one by one (several skills can level up in one night). Each OK click needs the
    pose to say LevelUpMenu (or no pose) AND the screen to show the banner with an OK button (find_levelup).
    ad: levelup(hwnd, tag), ui(...), pose(), sleep(s). Returns (clicks, last pose). Raises LevelUpRefused for a
    profession chooser, a non-LevelUp menu or anything unexpected: those are the owner's / CT's decision."""
    pose, n = first_pose, 0
    while True:
        menu = (pose or {}).get("active_menu")
        if pose is not None and not menu:
            return n, pose
        if menu and "LevelUp" not in str(menu):
            raise LevelUpRefused(f"menu {menu!r} is not a LevelUp: never auto-click")
        lv = ad.levelup(hwnd, f"levelup-{n + 1}")
        log("levelup_screen", **{k: lv.get(k) for k in ("path", "levelup", "dismissable", "ok_center")})
        if not lv.get("levelup"):
            if pose is None:
                return n, None                  # no pose and no banner on screen: nothing to dismiss
            raise LevelUpRefused(f"pose says {menu!r} but the Level Up banner is not on screen ({lv.get('path')})")
        if not lv.get("dismissable"):
            raise LevelUpRefused(f"Level Up banner without an OK button: profession choice (level 5/10) or unknown; "
                                 f"the owner decides ({lv.get('path')})")
        if n >= max_n:
            raise LevelUpRefused(f"still a LevelUp after {n} OK clicks")
        x, y = lv["ok_center"]
        rc, so, se = ad.ui("click", hwnd, x, y)
        n += 1
        log("levelup_ok_click", client=[x, y], rc=rc, intervention="operator click (count it in the receipt)")
        if rc != 0:
            raise LevelUpRefused(f"OK click failed: {(so or se).strip()[-200:]}")
        ad.sleep(1.5)
        pose = ad.pose()


# ---------------------------------------------------------------- startup_preferences (PLAN §1.5 a, B2)
FORCE_ON = "ForceOn"
BOM = b"\xef\xbb\xbf"


def parse_startup_prefs(raw):
    """{gamepadMode, client_gamepadMode, client_gamepadControls} from startup_preferences bytes (None = element absent)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw.decode("utf-8-sig"))

    def text(path):
        el = root.find(path)
        return el.text.strip() if el is not None and el.text else None
    return dict(gamepadMode=text("gamepadMode"), client_gamepadMode=text("clientOptions/gamepadMode"),
                client_gamepadControls=text("clientOptions/gamepadControls"),
                client_snappyMenus=text("clientOptions/snappyMenus"))


def prefs_force_on(facts):
    """Where an ARTI-left ForceOn persisted: clientOptions only. [] when the farmhand will load Auto / ForceOff.
    The top-level gamepadMode is the owner's options-page choice (Options.cs:1112) and never counts here."""
    return ["client_gamepadMode"] if (facts or {}).get("client_gamepadMode") == FORCE_ON else []


def repair_startup_prefs(raw, snappy="true"):
    """Inside <clientOptions> only: gamepadMode ForceOn -> Auto, gamepadControls -> false, snappyMenus -> snappy.
    The top level (the owner's own options) and every other byte, the BOM included, are kept."""
    s = raw.decode("utf-8-sig")
    m = re.search(r"<clientOptions>.*?</clientOptions>", s, re.S)
    if not m:
        return raw
    block = m.group(0).replace("<gamepadMode>ForceOn</gamepadMode>", "<gamepadMode>Auto</gamepadMode>")
    block = block.replace("<gamepadControls>true</gamepadControls>", "<gamepadControls>false</gamepadControls>")
    block = re.sub(r"<snappyMenus>(true|false)</snappyMenus>", f"<snappyMenus>{snappy}</snappyMenus>", block)
    return (BOM if raw.startswith(BOM) else b"") + (s[:m.start()] + block + s[m.end():]).encode("utf-8")


# ---------------------------------------------------------------- live adapter (never used by the offline tests)
class LiveAdapter:
    def __init__(self, refs, shot_dir):
        self.refs, self.shot_dir, self.n = refs, Path(shot_dir), 0
        self.stop_files = []

    def check_stop(self):
        if any(Path(p).exists() for p in self.stop_files):
            raise Exit(2, "owner_stop")

    def now(self):
        return time.time()

    def sleep(self, s):
        end = time.monotonic() + s
        while time.monotonic() < end:
            self.check_stop()
            time.sleep(min(0.1, max(0, end - time.monotonic())))

    def read_json(self, path):
        try:
            return json.loads(ipc.read_text(Path(path), encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    def read_text(self, path):
        try:
            return ipc.read_text(Path(path), encoding="utf-8-sig").strip()
        except OSError:
            return None

    def write_text(self, path, text):
        ipc.publish(Path(path), text, critical=True)

    def window(self, h):
        return wf.window(h)

    def windows(self):
        return wf.enum_windows("SMAPI")

    def exe(self, pid):
        return wf.exe_name(pid)

    def alive(self, pid):
        return wf.pid_alive(pid)

    def log_stat(self, path):
        """(size, mtime) of a SMAPI log, or None; the window wait reads its growth (ADR 242 follow-up)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return st.st_size, st.st_mtime

    def wait_exit(self, pid, timeout):
        if self.stop_files:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                self.check_stop()
                result = wf.wait_pid_exit(pid, min(0.1, end - time.monotonic()))
                if result is not False:
                    return result
            return False
        return wf.wait_pid_exit(pid, timeout)

    def trial_running(self):
        return wf.trial_running()

    def smapi_pids(self):
        return wf.image_pids(SMAPI_EXE)

    def dll_locked(self):
        p = paths.MOD / "ArtiStardewBridge.dll"
        if not p.exists():
            return False
        try:
            fd = os.open(str(p), os.O_RDWR | getattr(os, "O_BINARY", 0))   # opened, never written
        except PermissionError:
            return True
        except OSError:
            return True
        os.close(fd)
        return False

    def sha256(self, path):
        return hashlib.sha256(ipc.read_bytes(Path(path))).hexdigest()

    def deploy_records(self):
        return sorted(paths.PREP.glob("deploy-record-*.json"))

    def run(self, tool, *args, timeout=300, env=None):
        self.check_stop()
        p = subprocess.run([sys.executable, os.path.join(HERE, tool), *map(str, args)], capture_output=True,
                           text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr

    def ui(self, *args):
        return self.run("ui_op.py", *args, timeout=60)

    def prep(self, *args):
        return self.run("prep_trial.py", *args, timeout=300)

    def screen(self, hwnd, tag):
        self.n += 1
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        out = self.shot_dir / f"{self.n:02d}-{tag}.png"
        rc, so, se = self.ui("shot", hwnd, out)
        try:
            info = json.loads(so.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return dict(screen="unknown", path=None, error=(se or so)[-300:])
        best, scores, field = classify(out, info["client"], self.refs)
        return dict(screen=best, scores=scores, ip_field=field, path=str(out))

    def levelup(self, hwnd, tag):
        self.n += 1
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        out = self.shot_dir / f"{self.n:02d}-{tag}.png"
        rc, so, se = self.ui("shot", hwnd, out)
        try:
            info = json.loads(so.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return dict(path=None, levelup=False, dismissable=False, ok_center=None, error=(se or so)[-300:])
        return dict(find_levelup(out, info["client"], self.refs), path=str(out))

    def pose(self):
        import poll_state
        try:
            return poll_state.sample(5.0)
        except OSError:
            return None

    def startup_prefs(self):
        try:
            return ipc.read_bytes(paths.STARTUP_PREFS)
        except FileNotFoundError:
            return None

    def write_startup_prefs(self, new):
        """Back up the current file (PREP/startup-prefs-backup-<utc>), then replace it atomically. Returns the backup."""
        paths.PREP.mkdir(parents=True, exist_ok=True)
        now = time.time()
        bk = paths.PREP / f"startup-prefs-backup-{wf.stamp(now)}-{int(now * 1000) % 1000:03d}"
        i = 1
        while bk.exists():
            i += 1
            bk = bk.with_name(f"{bk.name.split('~')[0]}~{i}")
        shutil.copy2(paths.STARTUP_PREFS, bk)
        tmp = paths.STARTUP_PREFS.with_name(paths.STARTUP_PREFS.name + ".arti-tmp")
        tmp.write_bytes(new)
        os.replace(tmp, paths.STARTUP_PREFS)
        return str(bk)

    def run_stage(self, cmd, timeout):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def join_facts(self):
        """The mod's pre-world join_screen.json (Live7, CharacterCreationEnabled), or None."""
        return self.read_json(paths.COMMAND_DIR / "join_screen.json")

    def inspect(self):
        """One inspect_context through the command inbox (no port, no input): the ui.context dict or None."""
        sys.path.insert(0, str(Path(HERE).parents[1]))
        import arti_stardew_command as command
        try:
            sesi = command.baca_sesi(paths.COMMAND_DIR, retry_detik=0.25)
            res = command.minta_inspect_context(folder=paths.COMMAND_DIR, sesi=sesi)
        except (OSError, ValueError, command.KesalahanPerintah):
            return None
        ctx = (res.mentah or {}).get("context") if res.sukses else None
        return ctx if isinstance(ctx, dict) else None


# ---------------------------------------------------------------- the procedure
class Restart:
    def __init__(self, a, ad, refs, record_path):
        self.a, self.ad, self.refs, self.record_path = a, ad, refs, Path(record_path)
        self.rec = dict(schema="arti_restart_farmhand.v1", utc_start=wf.utc(ad.now()), args=vars(a).copy(), steps=[],
                        label="UNIT_TESTED tool; each live run is its own evidence")
        self.t0 = ad.now()
        self.last_neutral = None
        self.clicks = dict(refs["clicks"])
        for spec in a.click or []:
            name, _, xy = spec.partition("=")
            x, y = (int(v) for v in xy.split(","))
            self.clicks[name.strip()] = [x, y]

    def log(self, step, **kw):
        ev = dict(t=round(self.ad.now() - self.t0, 2), step=step, **kw)
        self.rec["steps"].append(ev)
        print(json.dumps(ev, ensure_ascii=False, default=str), flush=True)
        self.save()

    def save(self):
        if not self.a.dry_run:
            ipc.publish(self.record_path, json.dumps(self.rec, indent=1, ensure_ascii=False, default=str), critical=True)

    # 1
    def identify(self):
        ad, a = self.ad, self.a
        launch = ad.read_json(paths.FARMHAND_LAUNCH) or {}
        old_pid = launch.get("pid")
        fh = ad.read_text(paths.FH_HWND_FILE)
        host = ad.read_text(paths.HOST_HWND_FILE)
        hw = ad.window(host) if host else {"exists": False}
        facts = dict(old_pid=old_pid, fh_hwnd=fh, host_hwnd=host, host_window=hw)
        self.rec.update(old=facts)
        running = ad.trial_running()
        if running is not False:
            raise Exit(2, "a trial runner is running (or the process list is unknown): never restart during a trial"
                       if running else "cannot list python processes (PowerShell CIM failed); refusing")
        if not host or not hw.get("exists"):
            raise Exit(2, f"HOST window {host} not found (the farmhand joins the host; launch the host first)")
        self.check_prefs("identify", repair=False)
        if not old_pid:
            if a.no_close:
                smapi_pids = ad.smapi_pids()
                if smapi_pids is None:
                    raise Exit(2, "cannot list StardewModdingAPI.exe processes; refusing first launch")
                host_pid = hw.get("pid")
                extra = [pid for pid in smapi_pids if pid != host_pid]
                if extra:
                    raise Exit(2, f"--no-close first launch refused: other StardewModdingAPI.exe process(es) running {extra}")
                self.log("identify", no_close=True, first_launch=True, **facts)
                return None, None, int(host), hw
            raise Exit(2, f"no pid in {paths.FARMHAND_LAUNCH.name}: not an operator-launched farmhand")
        if a.no_close:
            if ad.alive(old_pid):
                raise Exit(2, f"--no-close but launch pid {old_pid} is still running; drop --no-close")
            self.log("identify", no_close=True, **facts)
            return old_pid, None, int(host), hw
        if not fh:
            raise Exit(2, f"no hwnd in {paths.FH_HWND_FILE.name}")
        w = ad.window(fh)
        exe = ad.exe(old_pid)
        facts.update(fh_window=w, exe=exe)
        why = []
        if str(fh) == str(host):
            why.append("farmhand hwnd == host hwnd")
        if not w.get("exists"):
            why.append(f"hwnd {fh} does not exist")
        elif w.get("pid") != old_pid:
            why.append(f"hwnd {fh} belongs to pid {w.get('pid')}, launch pid is {old_pid}")
        if (exe or "").lower() != SMAPI_EXE.lower():
            why.append(f"pid {old_pid} exe is {exe!r}, not {SMAPI_EXE}")
        if "running SMAPI" not in (w.get("title") or ""):
            why.append(f"title has no 'running SMAPI': {w.get('title')!r}")
        if hw.get("pid") == old_pid:
            why.append("host hwnd belongs to the farmhand pid")
        if why:
            self.log("identify", refused=why, **facts)
            raise Exit(2, "not the operator's farmhand: " + "; ".join(why))
        facts["session_before"] = ad.read_json(paths.COMMAND_DIR / "session.json")
        if not a.dry_run:
            facts["pose_before"] = _pose_brief(ad.pose())
        self.log("identify", ok=True, **facts)
        return old_pid, int(fh), int(host), hw

    # pre-flight: startup_preferences
    def check_prefs(self, when, repair):
        """Refuse a launch with gamepadMode ForceOn persisted. repair=True (only after the farmhand exited and only with
        --repair-startup-prefs) backs the file up and sets Auto. An adapter without startup_prefs() skips the check."""
        ad, a = self.ad, self.a
        read = getattr(ad, "startup_prefs", None)
        if read is None:
            self.log("startup_prefs", when=when, checked=False, note="adapter cannot read startup_preferences")
            return
        raw = read()
        if raw is None:
            self.log("startup_prefs", when=when, present=False, note="absent: the game starts with Auto")
            return
        try:
            facts = parse_startup_prefs(raw)
        except Exception as e:                    # XML parse error: never launch blind on a broken file
            raise Exit(2, f"startup_preferences unreadable ({e!r}); fix it before a restart")
        bad = prefs_force_on(facts)
        self.rec.setdefault("startup_prefs", {})[when] = facts
        self.log("startup_prefs", when=when, force_on=bad, **facts,
                 note="top-level gamepadMode ForceOn = the owner's own choice: reported only"
                 if facts.get("gamepadMode") == FORCE_ON else None)
        if not bad:
            return
        allow = getattr(a, "repair_startup_prefs", False)
        if not repair:
            if allow:
                self.log("startup_prefs", when=when, note="ForceOn: repaired after the farmhand exits (--repair-startup-prefs)")
                return
            raise Exit(2, f"startup_preferences has gamepadMode ForceOn at {bad}: the farmhand would start in pad mode with "
                          "a focused window (PLAN §1.5 a). Repair (owner-approved): rerun with --repair-startup-prefs "
                          f"(backup + ForceOn -> Auto after the farmhand exits), or edit {paths.STARTUP_PREFS.name} by hand "
                          "after a backup")
        if not allow:
            raise Exit(2, f"startup_preferences has gamepadMode ForceOn at {bad} after the exit; rerun with --repair-startup-prefs")
        if a.dry_run:
            return
        backup = ad.write_startup_prefs(repair_startup_prefs(raw, getattr(a, "repair_snappy_menus", "true")))
        after = parse_startup_prefs(read())
        self.rec["startup_prefs"]["repaired"] = dict(backup=backup, after=after)
        self.log("startup_prefs_repair", backup=backup, after=after, intervention="owner-approved edit (receipt)")
        if prefs_force_on(after):
            raise Exit(2, f"startup_preferences still ForceOn after the repair: {after}")

    # 2-3
    def close_and_wait(self, old_pid, fh):
        ad = self.ad
        rc, so, se = ad.ui("close", fh, "--expect-pid", old_pid)
        self.log("close", rc=rc, out=(so or se).strip()[-300:])
        if rc != 0:
            raise Exit(2, f"ui_op close refused/failed: {(so or se).strip()[-200:]}")
        t = ad.now()
        exited = ad.wait_exit(old_pid, self.a.timeout_exit)
        self.log("wait_exit", exited=exited, seconds=round(ad.now() - t, 1))
        if exited is None:
            raise Exit(4, f"cannot open SMAPI pid {old_pid} to wait for its exit (access?); not deploying, not launching")
        if not exited:
            raise Exit(4, f"SMAPI pid {old_pid} still running after {self.a.timeout_exit}s; no kill (CT decides)")
        for _ in range(10):
            ex = ad.read_json(paths.FARMHAND_EXIT) or {}
            if ex.get("pid") == old_pid:
                self.log("exit_record", **ex)
                return
            ad.sleep(1.0)
        self.log("exit_record", warning=f"{paths.FARMHAND_EXIT.name} does not name pid {old_pid} (process is gone anyway)")

    # 4
    def deploy(self, dll):
        ad = self.ad
        want = ad.sha256(dll)
        for _ in range(15):
            alive = ad.smapi_pids()
            if alive is None:
                raise Exit(7, "deploy refused: cannot list StardewModdingAPI.exe processes (tasklist failed)")
            if not alive:
                break
            ad.sleep(2.0)
        else:
            raise Exit(7, f"deploy refused: StardewModdingAPI.exe still running {alive} (DLL locked)")
        for i in range(5):
            if not ad.dll_locked():
                break
            self.log("deploy_wait_lock", attempt=i + 1)
            ad.sleep(2.0)
        else:
            raise Exit(7, "installed mod DLL still locked after 5 x 2 s")
        before = set(map(str, ad.deploy_records()))
        for attempt in range(1, 6):
            preset = getattr(self.a, "preset", None)
            rc, so, se = ad.prep("deploy", "--dll", dll, *(["--preset", preset] if preset else []))
            sharing = any(s in (se or "") for s in ("PermissionError", "WinError 32", "being used by another process"))
            self.log("deploy", attempt=attempt, rc=rc, sharing_violation=sharing, err=(se or "").strip()[-300:])
            if rc == 0 or not sharing:
                break
            ad.sleep(2.0)
        new = [p for p in ad.deploy_records() if str(p) not in before]
        if rc != 0 or not new:
            raise Exit(7, f"deploy failed (rc {rc}); new deploy records: {len(new)}")
        record = ad.read_json(new[-1]) or {}
        got = (((record.get("after") or {}).get("ArtiStardewBridge.dll")) or {}).get("sha256")
        self.rec["deploy"] = dict(dll=str(dll), sha256=want, record=str(new[-1]), after_sha256=got)
        if got != want:
            raise Exit(7, f"deploy record sha256 {got} != sha256(DLL) {want}")
        self.log("deploy_verified", record=str(new[-1]), dll8=want[:8])

    # 5
    def launch(self, old_pid):
        ad = self.ad
        t_launch = ad.now()
        rc, so, se = ad.prep("launch", "farmhand")
        self.log("launch", rc=rc, out=(so or se).strip()[-200:])
        if rc != 0:
            raise Exit(3, "prep_trial.py launch farmhand failed")
        new_pid = None
        for _ in range(30):
            rec = ad.read_json(paths.FARMHAND_LAUNCH) or {}
            if rec.get("pid") and rec.get("pid") != old_pid and _utc_ts(rec.get("utc")) >= t_launch - 2:
                new_pid = rec["pid"]
                break
            ad.sleep(1.0)
        if not new_pid:
            raise Exit(3, f"no new pid in {paths.FARMHAND_LAUNCH.name} within 30 s")
        hwnd = self.wait_window(new_pid, t_launch)
        old = ad.read_text(paths.FH_HWND_FILE)
        ad.write_text(paths.FH_HWND_FILE, f"{hwnd}\n")
        self.rec["new"] = dict(pid=new_pid, hwnd=hwnd, fh_hwnd_file_old=old)
        self.log("window", pid=new_pid, hwnd=hwnd, launched_after_s=round(ad.now() - t_launch, 1))
        self.separate_window(hwnd)
        return new_pid, hwnd, t_launch

    def wait_window(self, new_pid, t_launch):
        """ADR 242 follow-up (benchrun3: SMAPI still loading assemblies at 71 s, window at 96 s, exit 3 at 90 s): the
        window wait is bounded by evidence. Up to WINDOW_BASE_S as before; past it, the launch stays alive while its
        own SMAPI log (written after this launch) grew within WINDOW_GROWTH_S; never past WINDOW_CEILING_S. A process
        that exited ends the wait at once."""
        ad = self.ad
        start, grew_at, size, told = ad.now(), None, None, None
        stat = getattr(ad, "log_stat", lambda path: None)
        while True:
            rows = [w for w in ad.windows() if w.get("pid") == new_pid and "running SMAPI" in (w.get("title") or "")]
            if rows:
                return rows[0]["hwnd"]
            seen = stat(paths.FARMHAND_SMAPI_LOG)
            if seen and seen[1] >= t_launch - 2 and seen[0] != size:
                size, grew_at = seen[0], ad.now()
            elapsed = ad.now() - start
            state = window_wait_state(elapsed, ad.alive(new_pid), None if grew_at is None else ad.now() - grew_at)
            if state != "wait" and state != "growing":
                last = "never" if grew_at is None else f"{round(ad.now() - grew_at)} s ago"
                raise Exit(3, f"no 'running SMAPI' window for pid {new_pid} within {int(elapsed)} s "
                              f"({state}; its SMAPI log last grew {last}, {size or 0} bytes)")
            if state == "growing" and (told is None or ad.now() - told >= 10):
                told = ad.now()
                self.log("window_wait", pid=new_pid, elapsed_s=round(elapsed, 1), log_bytes=size,
                         log_grew_s_ago=round(ad.now() - grew_at, 1), ceiling_s=WINDOW_CEILING_S)
            ad.sleep(1.0)

    def separate_window(self, hwnd):
        """The new farmhand window opens on the HOST's exact rect (live 2026-10-07: both [311,142,1609,909]; S3 R6 failed
        on a title-bar mix-up). Move it with `ui_op.py pos` (SetWindowPos, no activation, no input; logged) to --rect, or by
        default to the HOST rect + (40,40) with the same size. Click coordinates are client-relative, so the join is
        unaffected. A failure here is a warning, never a stop."""
        a, ad = self.a, self.ad
        if a.rect == "none":
            self.log("separate_window", skipped="--rect none")
            return
        host = ad.window(self.host_hwnd)
        hr = host.get("rect")
        if a.rect:
            x, y, w, h = (int(v) for v in a.rect.split(","))
        elif hr:
            x, y, w, h = hr[0] + 40, hr[1] + 40, hr[2] - hr[0], hr[3] - hr[1]
        else:
            self.log("separate_window", warning="host rect unknown; window not moved")
            return
        rc, so, se = ad.ui("pos", hwnd, x, y, w, h)
        try:
            got = json.loads((so or "").strip().splitlines()[-1]).get("rect")
        except (ValueError, IndexError):
            got = None
        same = bool(got and hr and list(got) == list(hr))
        self.rec["new"]["rect"] = got
        self.log("separate_window", rc=rc, target=[x, y, x + w, y + h], rect=got, host_rect=hr,
                 warning="farmhand still shares the HOST rect" if same or rc != 0 else None)

    # 6
    def _wait(self, hwnd, want, timeout, stable=False):
        """Poll screenshots until the screen is `want` (stable: twice in a row). Returns the last screen dict."""
        ad = self.ad
        end = ad.now() + timeout
        prev = None
        last = None
        while True:
            # A missed Join-LAN click leaves its row hovered (MAD .0812 > .03),
            # hiding the still-present Co-op menu and preventing the bounded retry.
            if want in ("title", "coop", "ip"):
                self.neutral_cursor(hwnd)
            last = ad.screen(hwnd, want)
            self.log("screen", want=want, got=last.get("screen"), scores=last.get("scores"), path=last.get("path"))
            if last.get("screen") == want and (not stable or prev == want):
                return last
            prev = last.get("screen")
            if ad.now() >= end:
                return last
            ad.sleep(POLL_S)

    def neutral_cursor(self, hwnd):
        """Park outside the title buttons and Co-op reference crop so hover cannot hide either screen; at
        most every 10 s, and only with the farmhand verified in front (never a blind mouse move)."""
        xy = self.refs.get("neutral_cursor")
        if not xy or not getattr(self.a, "neutral_cursor", True):
            return
        now = self.ad.now()
        if self.last_neutral is not None and now - self.last_neutral < 10.0:
            return
        self.last_neutral = now
        rc, so, se = self.ad.ui("fg", hwnd)
        if '"ok": true' not in (so or ""):
            self.log("neutral_cursor", skipped="farmhand foreground not confirmed: no mouse move")
            return
        rc, so, se = self.ad.ui("move", hwnd, xy[0], xy[1])
        self.log("neutral_cursor", client=list(xy), rc=rc, intervention="cursor moved during the join (owner told)")

    def _click(self, hwnd, name):
        x, y = self.clicks[name]
        rc, so, se = self.ad.ui("click", hwnd, x, y)
        self.log("click", name=name, client=[x, y], rc=rc, out=(so or se).strip()[-200:])
        if rc != 0:
            raise Exit(5, f"click {name} failed: {(so or se).strip()[-200:]}")

    def _farmhand_title(self, hwnd):
        return "[farmhand]" in (self.ad.window(hwnd).get("title") or "")

    def join(self, hwnd, t_launch):
        if getattr(self.a, "join_slot", None):
            return self.join_by_facts(hwnd, t_launch)
        ad = self.ad
        for i, (screen, click) in enumerate(SEQ):
            timeout = max(5.0, self.a.timeout_join - (ad.now() - t_launch)) if screen == "title" else NEXT_WAIT_S
            got = self._wait(hwnd, screen, timeout, stable=(screen in ("title", "coop")))
            if got.get("screen") != screen:
                raise Exit(5, f"expected screen {screen!r}, got {got.get('screen')!r} (see {got.get('path')})")
            if screen == "ip" and got.get("ip_field") == "empty":
                self._click(hwnd, "ip_field")
                rc, so, se = ad.ui("type", self.refs.get("ip_text", "127.0.0.1"), "--hwnd", hwnd)
                self.log("type_ip", rc=rc, out=(so or se).strip()[-200:])
                if rc != 0:
                    raise Exit(5, f"typing the IP aborted (farmhand not foreground): {(so or se).strip()[-200:]}")
                got = self._wait(hwnd, "ip", 5.0)
                if got.get("ip_field") != "filled":
                    raise Exit(5, f"IP field still empty after typing (see {got.get('path')})")
            nxt = SEQ[i + 1][0] if i + 1 < len(SEQ) else None
            for attempt in (1, 2):
                self._click(hwnd, click)
                if nxt is None:
                    if self._wait_world(hwnd, screen):
                        return
                    still = True                     # _wait_world returns False only while the slot list is still shown
                else:
                    got = self._wait(hwnd, nxt, NEXT_WAIT_S)
                    if got.get("screen") == nxt:
                        break
                    still = got.get("screen") == screen
                if attempt == 2:
                    raise Exit(5, f"after clicking {click} twice the next screen did not appear")
                if not still:                        # never re-click blind on an unexpected screen
                    raise Exit(5, f"after clicking {click}: unexpected screen {got.get('screen')!r} ({got.get('path')})")
                self.log("retry_click", name=click, reason="still on %s after %.0fs (click eaten?)" % (screen, NEXT_WAIT_S))

    def _wait_world(self, hwnd, from_screen):
        """After the slot click: True when the title has [farmhand]. False = still on the slot list after 20 s
        (retry the click). Loading screens keep waiting up to WORLD_WAIT_S."""
        ad = self.ad
        t = ad.now()
        while ad.now() - t < WORLD_WAIT_S:
            if self._farmhand_title(hwnd):
                self.log("world", title=ad.window(hwnd).get("title"), seconds=round(ad.now() - t, 1))
                return True
            if ad.now() - t >= NEXT_WAIT_S:
                s = ad.screen(hwnd, "after-" + from_screen)
                self.log("screen", want="world", got=s.get("screen"), path=s.get("path"))
                if s.get("screen") == from_screen:
                    return False
            ad.sleep(POLL_S)
        raise Exit(5, f"no [farmhand] title within {WORLD_WAIT_S:.0f}s after the slot click")

    # 6b (Live7): the slot list from the mod's own join facts
    def _join_facts(self):
        f = getattr(self.ad, "join_facts", lambda: None)()
        if not isinstance(f, dict) or f.get("schema") != "arti_join_screen.v1":
            return None
        utc = f.get("utc_ms")
        if type(utc) is not int or abs(self.ad.now() * 1000 - utc) > JOIN_FACTS_FRESH_S * 1000:
            return None
        return f

    def _wait_facts(self, pred, timeout, tag):
        end = self.ad.now() + timeout
        last = None
        while True:
            f = self._join_facts()
            if f is not None:
                last = f
                if f.get("timed_out"):
                    raise Exit(5, f"FarmhandMenu connection timed out ({f.get('message')!r})")
                if pred(f):
                    return f
            if self.ad.now() >= end:
                self.log("join_facts", want=tag, last=_facts_brief(last))
                return None
            self.ad.sleep(POLL_S)

    @staticmethod
    def _slots_ready(f):
        return (f.get("sub_menu") == "FarmhandMenu" and f.get("farmhand_menu") is True and not f.get("getting_farmhands")
                and not f.get("approving_farmhand") and not f.get("loading") and f.get("current_item_index") == 0
                and bool(f.get("slots")))

    def _pick_slot(self, f):
        want = self.a.join_slot
        for s in f.get("slots") or ():
            if not s.get("farmhand_slot") or s.get("belongs_to_another_player") or not s.get("button"):
                continue
            if want == JOIN_SLOT_NEW and s.get("customized") is False:
                return s
            if want.startswith("name:") and s.get("customized") is True and s.get("name") == want[5:]:
                return s
        return None

    def _slot_click(self, f, slot):
        """Client pixel of the slot's native button centre; refused unless slot 0's button (same scale) contains the
        known-good screens.json slot click (a ui-scale / layout misunderstanding never clicks)."""
        scale = f.get("ui_scale")
        if not isinstance(scale, (int, float)) or not 0.5 <= scale <= 3.0:
            raise Exit(5, f"join facts ui_scale {scale!r} out of range")

        def client(rect):
            x, y, w, h = rect
            return [int(x * scale), int(y * scale), int((x + w) * scale), int((y + h) * scale)]
        first = next((s for s in f.get("slots") or () if s.get("index") == 0 and s.get("button")), None)
        known = self.clicks["slot"]
        if first is None:
            raise Exit(5, "join facts have no slot 0 button for the geometry check")
        r0 = client(first["button"])
        if not (r0[0] <= known[0] < r0[2] and r0[1] <= known[1] < r0[3]):
            raise Exit(5, f"geometry check failed: slot 0 {r0} does not contain the known slot click {known}")
        r = client(slot["button"])
        return [(r[0] + r[2]) // 2, (r[1] + r[3]) // 2]

    def join_by_facts(self, hwnd, t_launch):
        ad = self.ad
        for i, (screen, click) in enumerate(SEQ[:3]):
            timeout = max(5.0, self.a.timeout_join - (ad.now() - t_launch)) if screen == "title" else NEXT_WAIT_S
            got = self._wait(hwnd, screen, timeout, stable=(screen in ("title", "coop")))
            if got.get("screen") != screen:
                raise Exit(5, f"expected screen {screen!r}, got {got.get('screen')!r} (see {got.get('path')})")
            if screen == "ip" and got.get("ip_field") == "empty":
                self._click(hwnd, "ip_field")
                rc, so, se = ad.ui("type", self.refs.get("ip_text", "127.0.0.1"), "--hwnd", hwnd)
                self.log("type_ip", rc=rc, out=(so or se).strip()[-200:])
                if rc != 0:
                    raise Exit(5, f"typing the IP aborted (farmhand not foreground): {(so or se).strip()[-200:]}")
                got = self._wait(hwnd, "ip", 5.0)
                if got.get("ip_field") != "filled":
                    raise Exit(5, f"IP field still empty after typing (see {got.get('path')})")
            for attempt in (1, 2):
                self._click(hwnd, click)
                if click == "ip_ok":
                    # The next screen is the FarmhandMenu: confirmed by the mod's join facts, never by a crop.
                    if self._wait_facts(lambda f: f.get("sub_menu") == "FarmhandMenu", NEXT_WAIT_S, "FarmhandMenu"):
                        break
                    still = ad.screen(hwnd, "ip").get("screen") == "ip"
                else:
                    nxt = SEQ[i + 1][0]
                    got = self._wait(hwnd, nxt, NEXT_WAIT_S)
                    if got.get("screen") == nxt:
                        break
                    still = got.get("screen") == screen
                if attempt == 2:
                    raise Exit(5, f"after clicking {click} twice the next screen did not appear")
                if not still:
                    raise Exit(5, f"after clicking {click}: unexpected screen (join facts / {screen})")
                self.log("retry_click", name=click, reason=f"still on {screen}")
        timeout = getattr(self.a, "timeout_slots", SLOTS_WAIT_S)
        f = self._wait_facts(self._slots_ready, timeout, "slots")
        if f is None:
            raise Exit(5, f"no loaded FarmhandMenu slot list within {timeout:.0f}s (join_screen.json missing = "
                          "CharacterCreationEnabled off; a host in its intro keeps it waiting)")
        slot = self._pick_slot(f)
        if slot is None:
            raise Exit(5, f"no {self.a.join_slot!r} slot in the list: {[_slot_brief(s) for s in f.get('slots') or ()]}")
        xy = self._slot_click(f, slot)
        self.log("join_slot", want=self.a.join_slot, slot=_slot_brief(slot), client=xy, ui_scale=f.get("ui_scale"))
        for attempt in (1, 2):
            rc, so, se = ad.ui("click", hwnd, xy[0], xy[1])
            self.log("click", name="join_slot", client=xy, rc=rc, out=(so or se).strip()[-200:])
            if rc != 0:
                raise Exit(5, f"slot click failed: {(so or se).strip()[-200:]}")
            t = ad.now()
            while ad.now() - t < WORLD_WAIT_S:
                if self._farmhand_title(hwnd):
                    self.log("world", title=ad.window(hwnd).get("title"), seconds=round(ad.now() - t, 1))
                    return
                if ad.now() - t >= NEXT_WAIT_S:
                    again = self._join_facts()
                    if again is not None and self._slots_ready(again) and attempt == 1:
                        break                       # still on the loaded slot list: the click was eaten, retry once
                ad.sleep(POLL_S)
            else:
                raise Exit(5, f"no [farmhand] title within {WORLD_WAIT_S:.0f}s after the slot click")
            self.log("retry_click", name="join_slot", reason="slot list still shown")
        raise Exit(5, "after clicking the slot twice the world did not load")

    # 7b (Live7): a new farmhand lands in the game's CharacterCustomization
    def verify_charcreate(self, hwnd, session_before):
        ad = self.ad
        sess = None
        for _ in range(40):
            sess = ad.read_json(paths.COMMAND_DIR / "session.json") or {}
            if sess.get("world_ready") is True and sess.get("session_id") and sess.get("session_id") != (session_before or {}).get("session_id"):
                break
            ad.sleep(1.5)
        else:
            raise Exit(5, f"session.json has no new world_ready session (now {sess.get('session_id') if sess else None})")
        self.rec["session_after"] = sess.get("session_id")
        ctx = None
        for _ in range(10):
            ctx = ad.inspect()
            if ctx is not None:
                break
            ad.sleep(1.0)
        block = (ctx or {}).get("ui_pad") or {}
        menu = ((ctx or {}).get("interactive") or {}).get("menu_type")
        self.log("verify_charcreate", session_id=sess.get("session_id"), menu_type=menu, context=block.get("context"),
                 wait_reason=block.get("wait_reason"), char_custom=bool(block.get("char_custom")))
        if menu != "CharacterCustomization" or block.get("context") != "char_custom" or not block.get("char_custom"):
            raise Exit(6, f"joined, but not in the farmhand character creation (menu {menu!r}, context "
                          f"{block.get('context')!r}): left for the CT")
        self.rec["charcreate_ready"] = True

    # 7
    def verify(self, hwnd, session_before):
        ad = self.ad
        sess = None
        for _ in range(40):
            sess = ad.read_json(paths.COMMAND_DIR / "session.json") or {}
            if sess.get("world_ready") is True and sess.get("session_id") and sess.get("session_id") != (session_before or {}).get("session_id"):
                break
            ad.sleep(1.5)
        else:
            raise Exit(5, f"session.json has no new world_ready session (before {(session_before or {}).get('session_id')}, now {sess.get('session_id') if sess else None})")
        self.rec["session_after"] = sess.get("session_id")
        pose = ad.pose()
        self.log("verify", session_id=sess.get("session_id"), world_ready=sess.get("world_ready"), pose=_pose_brief(pose))
        menu = (pose or {}).get("active_menu")
        if not self.a.dismiss_levelup:
            if menu:
                raise Exit(6, f"post-join menu {menu!r} left for the CT")
            if pose is None:
                lv = ad.levelup(hwnd, "verify")
                self.log("verify_screen", levelup=lv.get("levelup"), path=lv.get("path"), note="pose unavailable")
                if lv.get("levelup"):
                    raise Exit(6, "LevelUp on screen (pose unavailable) left for the CT")
        elif menu or pose is None:
            try:
                n, pose = dismiss_levelups(ad, hwnd, self.log, first_pose=pose)
            except LevelUpRefused as e:
                raise Exit(6, str(e))
            self.rec["levelup_clicks"] = n
        self.rec["pose_after"] = _pose_brief(pose)

    def stage_cmd(self):
        return _stage_cmd(self.a.stage, shlex.split(getattr(self.a, "stage_args", None) or ""))

    def stage_hook(self):
        """--stage: the staging script after a verified join (exit 8 on failure; its output is kept in the record)."""
        if not getattr(self.a, "stage", None):
            return
        cmd = self.stage_cmd()
        rc, so, se = self.ad.run_stage(cmd, timeout=getattr(self.a, "stage_timeout", 1800.0))
        self.log("stage", cmd=cmd, rc=rc, out=(so or "")[-1500:], err=(se or "")[-800:])
        if rc != 0:
            raise Exit(8, f"stage script failed (rc {rc}): {((se or so) or '').strip()[-200:]}")

    def run(self):
        a, ad = self.a, self.ad
        host = None
        rc = 0
        try:
            old_pid, fh, host, _ = self.identify()
            self.host_hwnd = host
            dll = (a.deploy if a.deploy not in (None, True) else str(paths.DLL)) if a.deploy is not None else None
            plan = []
            if not a.no_close:
                plan += [f"ui_op.py close {fh} --expect-pid {old_pid}", f"wait for pid {old_pid} (<= {a.timeout_exit}s)"]
            if dll:
                plan += [f"prep_trial.py deploy --dll {dll}"]
            plan += ["prep_trial.py launch farmhand", "find the new 'running SMAPI' window; write fh-hwnd.txt"]
            if getattr(a, "join_slot", None):
                plan += [f"join slot {a.join_slot!r} from the mod's join_screen.json after IP OK (no slot crop)"]
            plan += [f"screen-gated clicks: {', '.join(f'{c} {self.clicks[c]}' for _, c in SEQ)}",
                     "verify [farmhand] title, new session_id, pose; LevelUp " + ("OK click" if a.dismiss_levelup else "-> exit 6")]
            if getattr(a, "stage", None):
                plan += [f"stage: {' '.join(self.stage_cmd())}"]
            plan += [f"ui_op.py fg {host}"]
            if a.dry_run:
                self.log("dry_run", plan=plan)
                return 0
            session_before = ad.read_json(paths.COMMAND_DIR / "session.json")
            if not a.no_close:
                self.close_and_wait(old_pid, fh)
            self.check_prefs("after_exit", repair=True)
            if dll:
                if not Path(dll).is_file():
                    raise Exit(7, f"DLL not found: {dll}")
                self.deploy(dll)
            new_pid, hwnd, t_launch = self.launch(old_pid)
            self.join(hwnd, t_launch)
            if getattr(a, "join_slot", None) == JOIN_SLOT_NEW:
                self.verify_charcreate(hwnd, session_before)
            else:
                self.verify(hwnd, session_before)
            self.log("done", rejoined=True)
            self.stage_hook()
        except Exit as e:
            rc = e.code
            self.log("exit", code=e.code, reason=str(e))
        except Exception as e:      # unexpected: still foreground the host and keep the record
            rc = 3
            self.log("exit", code=3, reason=f"unexpected {e!r}")
        finally:
            stopped = any(Path(p).exists() for p in getattr(ad, "stop_files", []))
            if host and not a.dry_run and not stopped:
                frc, so, se = ad.ui("fg", host)
                ok = '"ok": true' in (so or "")
                self.log("fg_host", hwnd=host, ok=ok, rc=frc)
            self.rec.update(exit_code=rc, seconds=round(ad.now() - self.t0, 1), utc_end=wf.utc(ad.now()))
            self.save()
        return rc


def _stage_cmd(script, args):
    script = str(script)
    if not os.path.isabs(script) and not os.path.exists(script):
        script = os.path.join(HERE, script)                 # a bare tool name means this folder
    if script.endswith(".sh"):
        import soak                                           # one Git Bash finder (never WSL's bash)
        return [soak._git_bash(), script, *args]
    return [sys.executable, script, *args]


def _slot_brief(s):
    return {k: s.get(k) for k in ("index", "customized", "name", "belongs_to_another_player")} if isinstance(s, dict) else None


def _facts_brief(f):
    if not isinstance(f, dict):
        return None
    return {k: f.get(k) for k in ("sub_menu", "getting_farmhands", "approving_farmhand", "timed_out", "message", "loading")}


def _join_slot_arg(text):
    t = (text or "").strip()
    if t == JOIN_SLOT_NEW or (t.startswith("name:") and 1 <= len(t) - 5 <= 12):
        return t
    raise argparse.ArgumentTypeError("--join-slot is 'new' or 'name:NAME' (1-12 characters)")


def _pose_brief(p):
    if not p:
        return None
    return {k: p.get(k) for k in ("location", "tile", "time_of_day", "date", "is_player_free", "active_menu", "energy")}


def _utc_ts(s):
    try:
        import calendar
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="restart_farmhand.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deploy", nargs="?", const=True, default=None, metavar="DLL",
                    help="deploy a mod DLL after SMAPI exited (default DLL: m1_paths.DLL / $ARTI_M1_DLL)")
    ap.add_argument("--dismiss-levelup", action="store_true", help="click OK on a LevelUp menu after the join (operator intervention)")
    ap.add_argument("--no-close", action="store_true", help="the farmhand is already gone (crash/closed); launch + join only")
    ap.add_argument("--stop-file", type=Path, help="Live7 cooperative STOP: refuse further launch/UI actions")
    ap.add_argument("--dry-run", action="store_true", help="identify + print the plan; sends nothing")
    ap.add_argument("--timeout-exit", type=float, default=120.0)
    ap.add_argument("--timeout-join", type=float, default=180.0, help="seconds from launch until the title screen must be ready")
    ap.add_argument("--refs", default=str(REFS), help="folder with screens.json + reference crops")
    ap.add_argument("--click", action="append", metavar="NAME=X,Y", help="override a click (coop, join_lan, ip_field, ip_ok, slot)")
    ap.add_argument("--rect", metavar="X,Y,W,H",
                    help="window rect for the new farmhand (default: HOST rect + 40,40, same size; 'none' = do not move)")
    ap.add_argument("--preset", choices=("v1", "v2-pad", "v2-probe", "live7-charcreate"),
                    help="trial flag preset passed to `prep_trial.py deploy --preset` (with --deploy)")
    ap.add_argument("--no-neutral-cursor", dest="neutral_cursor", action="store_false",
                    help="do not move the cursor to screens.json neutral_cursor before title shots")
    ap.add_argument("--repair-startup-prefs", action="store_true",
                    help="owner-approved: after the exit, back up startup_preferences and set gamepadMode ForceOn -> Auto")
    ap.add_argument("--repair-snappy-menus", choices=("true", "false"), default="true",
                    help="clientOptions snappyMenus written by --repair-startup-prefs (default true = the game default)")
    ap.add_argument("--stage", metavar="SCRIPT", help="run SCRIPT after a verified join (.py: this python; .sh: Git Bash)")
    ap.add_argument("--stage-args", default="", help="arguments for --stage (one string, shlex-split)")
    ap.add_argument("--stage-timeout", type=float, default=1800.0)
    ap.add_argument("--join-slot", type=_join_slot_arg, default=None,
                    help="Live7: pick the slot from the mod's join_screen.json: 'new' (an unclaimed cabin -> the game's "
                         "character creation, verified by inspect_context) or 'name:NAME' (ARTI's own farmhand)")
    ap.add_argument("--timeout-slots", type=float, default=SLOTS_WAIT_S,
                    help="--join-slot: seconds to wait for the loaded slot list after IP OK (host intro)")
    return ap


def main(argv=None, adapter=None):
    a = build_parser().parse_args(argv)
    refs = load_refs(a.refs)
    st = wf.stamp()
    record = paths.RUN / f"restart-{st}.json"
    i = 1
    while record.exists():
        i += 1
        record = paths.RUN / f"restart-{st}-{i}.json"
    ad = adapter or LiveAdapter(refs, record.with_suffix(""))
    if isinstance(ad, LiveAdapter) and a.stop_file:
        ad.stop_files = [a.stop_file, paths.RUN / "LIVE7_STOP"]
    if not a.dry_run:
        paths.RUN.mkdir(parents=True, exist_ok=True)
    rc = Restart(a, ad, refs, record).run()
    if a.dry_run:
        print("DRY RUN OK (nothing sent)" if rc == 0 else f"EXIT {rc}")
    else:
        print(("REJOINED" if rc == 0 else f"EXIT {rc}") + f" (record {record})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
