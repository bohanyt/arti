"""prep_trial.py: M1 background trial package (explicit subcommands, nothing runs on import).

  config                      write PREP/trial-config.json (explicit reviewed config for scripts/stardew_m1_trial.py)
  deploy [--dll PATH] [--preset v1|v2-pad|v2-probe]
                              back up the installed mod DLL+config, install the Stage 2 DLL and the trial config.json
                              (default DLL: m1_paths.DLL). Only after SMAPI has exited: SMAPI locks the DLL.
                              Presets (Primitives v2, PLAN §1.5 b): v2-pad = + GamepadInputEnabled +
                              FarmhandInactiveRenderingEnabled (the cursor-hidden gate needs the Draw path); v2-probe =
                              + GamepadProbeEnabled (probe DLL only). ARTI_M1_MOD_CONFIG_EXTRA (JSON) is merged last.
  restore                     put the installed mod DLL+config back from the NEWEST backup (exact bytes, hash-checked)
  launch host|farmhand        start vanilla Stardew (HOST) or SMAPI (farmhand, via a supervisor that holds stdin open
                              and writes RUN/farmhand-launch.json + farmhand-exit.json); no clicks
  status                      read the farmhand's session.json from the command dir (world_ready, player, flags)
  approval --owner-text TEXT --owner-url URL [--seconds S]   write approval.json for mode=combined (identity + DLL +
                               session pinned); S = limits.seconds, 60..600 (sleep/animal 900, S15 alone 1200; default 600)
  preserve-journal LABEL      move a used RUN/journal aside to RUN/journal-attempt<LABEL>-<utc> (never deletes)
  run                         run scripts/stardew_m1_trial.py --mode combined from the Stage 2 worktree; refuses a
                              used journal (preserve it first) and (OPS, batch 6) refuses to start unless one fresh pose
                              read shows free=True and menu=None (--no-pose-gate only for a deliberate menu trial)
  procs                       list Stardew/SMAPI processes

Folders come from m1_paths.py (environment variables). Never edits saves. Never commits. Mod backups and restores are
byte-exact and hash-checked.
"""
import argparse, hashlib, json, os, shutil, subprocess, sys, time, uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also under `python -I` (the farmhand supervisor)
import m1_paths as paths  # noqa: E402

PREP = paths.PREP
WORKTREE = paths.WORKTREE
DEV = paths.DEV
PY = paths.PY
GAME = paths.GAME
MOD = paths.MOD
RUN = paths.RUN
COMMAND_DIR = paths.COMMAND_DIR
JOURNAL_DIR = paths.JOURNAL_DIR
TRIAL_CONFIG = paths.TRIAL_CONFIG
APPROVAL = paths.APPROVAL
ENVELOPE = "https://github.example.invalid/owner/private-lab-repository/issues/example-a"
SAVE_NAME = paths.SAVE_NAME

MOD_TRIAL_CONFIG = {
    "ContainerExitInputEnabled": True, "ContainerTransferInputEnabled": True, "WorldInteractionInputEnabled": True,
    "SelectItemInputEnabled": True, "Enabled": True, "Port": paths.PORT, "WaterOnceEnabled": False,
    "ControllerBootstrapProbeEnabled": False, "ControllerMenuStepEnabled": False, "ControllerStickHoldProbeEnabled": False,
    "MenuStepCommandInboxEnabled": True, "WorldPressEnabled": True, "UseToolInputEnabled": True,
    "BackgroundMovementEnabled": True, "BackgroundToolUiEnabled": True, "FarmhandInactiveRenderingEnabled": True,
    # Primitives v1 batch 1 (WS1 farm tools, WS2 ui_key; both default-OFF in ModConfig), on for trials 2026-10-07.
    "BackgroundFarmToolsEnabled": True, "UiKeyInputEnabled": True,
    # Primitives v1 WS3a (door / forage world_interact kinds, move_through; default-OFF in ModConfig), on for trials.
    "WorldInteractKindsEnabled": True, "MoveThroughEnabled": True,
    # Primitives v1 WS3b (NPC talk, eat, bed; default-OFF in ModConfig), on for trials from batch 3.
    "NpcEatBedEnabled": True,
    # ADR 243 (raw hands): the generic ACTION/USE target for any faced tile, not only beside a chest, a counter or an
    # Action tile (ModEntry.GenericWorld.cs GenericWorldReady; default-OFF in ModConfig). Its admission is unchanged.
    "GenericWorldInputEnabled": True,
    "CommandInboxDirectory": str(COMMAND_DIR),
}

# Primitives v2 trial presets (every flag default OFF in ModConfig; `deploy --preset` adds them to MOD_TRIAL_CONFIG).
PRESETS = {
    "v1": {},
    # PadButtonInputEnabled: WS-G0 pad_button (LB/RB/Start/Y). UiPadMenusEnabled: WS-G1's flag; SMAPI's config
    # reader (Newtonsoft) ignores a key the deployed ModConfig does not know yet, so it is harmless before G1 merges.
    "v2-pad": {paths.PAD_FLAG: True, paths.DRAW_FLAG: True, "PadButtonInputEnabled": True, "UiPadMenusEnabled": True,
               "PadSelectJournalEnabled": True},
    # The pad path refuses to start with either probe on (one pad writer); probes run with the v2-probe preset only.
    "v2-probe": {"GamepadProbeEnabled": True, "ReflexProbeEnabled": True},
    # Live7 character creation (docs/stardew/live7/CHARACTER_CREATION.md): v2-pad + the char_custom context, farmer_profile
    # and the pre-world join_screen.json (CharacterCreationEnabled, default OFF in ModConfig).
    "live7-charcreate": {paths.PAD_FLAG: True, paths.DRAW_FLAG: True, "PadButtonInputEnabled": True, "UiPadMenusEnabled": True,
                         "PadSelectJournalEnabled": True, "CharacterCreationEnabled": True},
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def cmd_config(_a):
    local = json.loads((DEV / "config_local.json").read_text(encoding="utf-8"))
    cfg = {k: v for k, v in local.items() if k.startswith("stardew_") or k.startswith("codex_")}
    cfg.update(stardew_runtime_enabled=True, stardew_continuation_enabled=True, stardew_provider_enabled=True,
               stardew_spend_enabled=True, stardew_continuation_decisions=64, stardew_continuation_wall_seconds=600, stardew_gather_deadline_seconds=600,
               stardew_continuation_per_minute=20, stardew_actions_per_session=48, stardew_provider_requests_per_session=64,
               stardew_receiver_port=paths.PORT, stardew_command_dir=str(COMMAND_DIR), stardew_wiki_enabled=False,
               # owner decision 2026-10-07: luna 6, medium effort, Codex fast tier
               stardew_decision_model="gpt-6-luna", stardew_decision_effort="medium",
               stardew_decision_service_tier="priority",
               # WS2 day-start rule (default false in production; on for trials, PLAN Primitives v1)
               stardew_day_start_handler_enabled=True,
               # ADR 243: her raw hands on every page (default false in code; on where she plays)
               stardew_live7_raw_hands=True)
    TRIAL_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(json.dumps({"written": str(TRIAL_CONFIG), "sha256": sha(TRIAL_CONFIG),
                      "provider": cfg.get("stardew_decision_provider_kind"), "model": cfg.get("stardew_decision_model")}, indent=1))


def cmd_deploy(a):
    dll = Path(a.dll).resolve()
    assert dll.is_file(), dll
    bk = PREP / ("mod-backup-" + stamp())
    bk.mkdir(parents=True, exist_ok=False)
    rec = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "backup": str(bk), "before": {}, "after": {}}
    for name in ("ArtiStardewBridge.dll", "config.json", "manifest.json"):
        src = MOD / name
        if src.exists():
            shutil.copy2(src, bk / name)
            rec["before"][name] = {"sha256": sha(src), "size": src.stat().st_size}
    shutil.copy2(dll, MOD / "ArtiStardewBridge.dll")
    cfg = dict(MOD_TRIAL_CONFIG)
    cfg.update(PRESETS[a.preset])
    rec["preset"] = a.preset
    # Probe-only (a probe branch): extra default-OFF flags as JSON, e.g. '{"GamepadProbeEnabled": true}'.
    extra = os.environ.get("ARTI_M1_MOD_CONFIG_EXTRA", "").strip()
    if extra:
        cfg.update(json.loads(extra))
    (MOD / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    COMMAND_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("ArtiStardewBridge.dll", "config.json", "manifest.json"):
        p = MOD / name
        rec["after"][name] = {"sha256": sha(p), "size": p.stat().st_size}
    rec["sourceDll"] = {"path": str(dll), "sha256": sha(dll)}
    assert rec["after"]["ArtiStardewBridge.dll"]["sha256"] == rec["sourceDll"]["sha256"]
    (PREP / ("deploy-record-" + stamp() + ".json")).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    # private config for the worktree runtime (gitignored; never committed)
    shutil.copy2(DEV / "config_local.json", WORKTREE / "config_local.json")
    print(json.dumps(rec, indent=1))


def cmd_restore(_a):
    backups = sorted(PREP.glob("mod-backup-*"))
    assert backups, "no backup"
    bk = backups[-1]
    out = {}
    for name in ("ArtiStardewBridge.dll", "config.json", "manifest.json"):
        src = bk / name
        if src.exists():
            shutil.copy2(src, MOD / name)
            out[name] = {"restored": True, "sha256": sha(MOD / name), "equal": sha(MOD / name) == sha(src)}
    print(json.dumps({"from": str(bk), "files": out}, indent=1))


def cmd_launch(a):
    RUN.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    if a.role == "host":
        exe = GAME / "Stardew Valley.exe"
        p = subprocess.Popen([str(exe)], cwd=str(GAME), creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec = {"role": "host", "pid": p.pid, "exe": str(exe), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    else:
        if _owner_run_without_word():
            print(json.dumps({"refused": "owner_join_needs_owner_word", "message": (
                "The owner hosts this run: run `mabar rejoin --owner-ready` on his word, or start her game by hand, join "
                "from its window and run `mabar rejoin`.")}))
            return 2
        # SMAPI reads console commands from stdin and exits on EOF, so a long-lived supervisor must hold the pipe open
        # (the S3 harness did the same: stdin_pipe=True, CREATE_NO_WINDOW). DETACHED_PROCESS + DEVNULL made it exit at once.
        sup = subprocess.Popen([str(PY), "-I", "-B", str(Path(__file__).resolve()), "_supervise", "farmhand"], cwd=str(GAME),
                               creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec = {"role": "farmhand", "supervisorPid": sup.pid, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "note": "SMAPI pid is written by the supervisor to run/farmhand-launch.json"}
    (RUN / f"launch-{a.role}-{stamp()}.json").write_text(json.dumps(rec), encoding="utf-8")
    print(json.dumps(rec))


def _owner_run_without_word():
    from scripts.livepath import halt_authority as HA

    return RUN.name == "runtime" and HA.prepared_owner_run(RUN.parent) and HA.recovery_guard_config() is None


def start_owner_launcher(guard, word):
    """Send the owner's launch authority over the worker's private pipe to the detached launcher."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    launcher = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "_supervise", "owner-word"],
                                cwd=str(GAME), creationflags=flags, close_fds=True,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with launcher.stdin:
        launcher.stdin.write(json.dumps(dict(guard=guard, word=word)).encode("utf-8"))
    return launcher


def cmd_supervise(a):
    from contextlib import ExitStack, nullcontext
    from scripts.livepath import halt_authority as HA, owner_join
    from arti_runtime import ipc

    word = None
    if getattr(a, "role", None) == "owner-word":
        envelope = json.load(sys.stdin)
        guard, word = envelope["guard"], envelope["word"]
        run = Path(guard["run"])
        if (guard["act"] != "launch" or run.resolve() / "runtime" != RUN.resolve()
                or Path(guard["inbox"]).resolve() != COMMAND_DIR.resolve()):
            owner_join.refuse("owner_join_attempt_mismatch")
        lock = owner_join.spawn_lock(COMMAND_DIR)
    else:
        guard = HA.recovery_guard_config()
        if _owner_run_without_word():
            owner_join.refuse("owner_join_needs_owner_word")
        lock = nullcontext()

    exe = GAME / "StardewModdingAPI.exe"
    launch_id = uuid.uuid4().hex
    path = RUN / "farmhand-launch.json"
    with ExitStack() as streams:
        with lock:
            predecessor = HA.read_launch_record(path)
            if word is not None:
                owner_join.require_attempt(word, run, parent="launcher")
                mode = owner_join.require_mode(word, run)
                HA.child_recovery_gate(guard, owner_launch=mode)
                owner_join.reserve_spawn(run, word)
            _, previous = HA.read_record(COMMAND_DIR / "session.json")
            stamp_id = stamp() + ("-" + launch_id if word is not None else "")
            stderr = f"farmhand-stderr-{stamp_id}.raw"
            launch = dict(pid=None, launch_id=launch_id, exe=str(exe), stderr=stderr,
                          previous_runtime=HA.identity(previous)[1], issued_ms=int(time.time() * 1000),
                          utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            if word is not None:
                launch.update(owner_attempt=word["id"], owner_host=word["host"],
                              replaces=owner_join.replaced(predecessor[1]))
            out = streams.enter_context((RUN / f"farmhand-stdout-{stamp_id}.raw").open("wb"))
            err = streams.enter_context((RUN / stderr).open("wb"))
            ipc.write_json(path, launch, fsync=True)  # preserve the pending launch before a process can publish or crash
            reservation = None
            try:
                saved = (launch_id, predecessor)
                if word is not None:
                    if (owner_join.wf.image_pids(owner_join.SMAPI) != [word["host"]["pid"]]
                            or owner_join.wf.trial_running() is not False):
                        owner_join.refuse("owner_join_spawn_already_in_use")
                    if not owner_join.wf.lan_host(word["host"]["pid"]):
                        owner_join.refuse("owner_join_local_host_unconfirmed")
                    owner_join.require_attempt(word, run, parent="launcher")
                    if launch["replaces"] is not None:
                        owner_join.require_gone(launch["replaces"])
                    decision = HA.child_recovery_gate(guard, saved_launch=saved, owner_launch=mode)
                else:
                    decision = HA.child_recovery_gate(guard, saved_launch=saved)
                if decision is not None and decision.watchdog:
                    reservation = HA.reserve_watchdog(guard["run"], decision.watchdog)
                    if reservation is None:
                        raise HA.HandsHeld(HA.Decision("hold", "watchdog_recovery_limit"))
                    launch["watchdog"] = reservation
                    ipc.write_json(path, launch, fsync=True)
                    final = HA.child_recovery_gate(guard, saved_launch=saved, reservation=reservation)
                    if final.watchdog != reservation["proof"]:
                        raise HA.HandsHeld(HA.Decision("hold", "watchdog_recovery_authority_changed"))
                child = subprocess.Popen([str(exe), "--mods-path", str(GAME / "Mods")], cwd=str(GAME),
                                         creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.PIPE, stdout=out, stderr=err)
            except Exception:
                if reservation is not None:
                    HA.release_watchdog(guard["run"], reservation)
                if HA.read_record(path)[1] == launch:
                    if isinstance(predecessor[0], bytes):
                        ipc.atomic_write(path, predecessor[0], fsync=True)
                    elif predecessor[0] is None:
                        ipc.write_json(path, dict(launch, not_started=True), fsync=True)
                raise
            launch["pid"] = child.pid
            if word is not None:
                launch["process_start"] = owner_join.wf.process_start(child.pid)
            elif (started := owner_join.wf.process_start(child.pid)) is not None:
                launch["process_start"] = started
            ipc.write_json(path, launch, fsync=True)
        if reservation is not None:
            from scripts.livepath.mabar_supervise import log_line
            log_line(guard["run"], "ALERT restarting her game after its own watchdog stopped it (%d of %d)." %
                     (reservation["slot"], HA.WATCHDOG_LIMIT))
        rc = child.wait()
    ipc.write_json(RUN / "farmhand-exit.json", dict(pid=child.pid, launch_id=launch_id, returncode=rc,
                                                   utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())), fsync=True)
    if word is not None:
        owner_join.record_ended(run, COMMAND_DIR)


def cmd_status(_a):
    p = COMMAND_DIR / "session.json"
    if not p.exists():
        print(json.dumps({"session": None, "note": "no session.json yet (farmhand not connected / mod inbox off)"}))
        return
    j = paths.read_session()
    keys = ("protocol", "session_id", "player_id", "player_name", "location_id", "is_main_player", "world_ready",
            "inbox_enabled", "menu_step_enabled", "world_press_enabled", "input_busy", "inflight_request_id")
    print(json.dumps({k: j.get(k) for k in keys}, indent=1))


def cmd_approval(a):
    if getattr(a, "live7", False):
        return cmd_live7_approval(a)
    if getattr(a, "goal_file", None):
        a.goal = Path(a.goal_file).read_text(encoding="utf-8-sig").strip()
    if a.seconds < 60:
        raise SystemExit(f"--seconds must be 60..600 (got {a.seconds})")
    inputs = getattr(a, "inputs", None)
    sys.path.insert(0, str(WORKTREE / "scripts"))
    sys.path.insert(0, str(WORKTREE))
    try:
        import stardew_m1_trial as T  # inert import
    except ModuleNotFoundError as exc:
        # An absent configured runner cannot authorize the extended envelope.
        if exc.name == "stardew_m1_trial" and a.seconds > 600:
            raise SystemExit(f"--seconds must be 60..600 (got {a.seconds})") from None
        raise
    # The configured runtime worktree may precede this operator helper during
    # review/integration. Never approve a higher envelope for that older runner.
    maximum = T.input_approval_max(a.goal) if hasattr(T, "input_approval_max") else T.INPUT_APPROVAL_MAX
    seconds_max = T.seconds_approval_max(a.goal) if hasattr(T, "seconds_approval_max") else 600
    selected = [p for p in ("a", "b", "c") if getattr(a, "part_" + p, False)]
    campaign = selected[0] if selected else None
    if len(selected) > 1:
        raise SystemExit("choose exactly one campaign approval")
    if campaign:
        recognizes = getattr(T, "part_" + campaign + "_goal", lambda text: False)
        from arti_stardew_part_a import CAMPAIGN_INPUT
        if not recognizes(a.goal) or a.seconds != 5400 or inputs != CAMPAIGN_INPUT[campaign] or a.minutes < 91:
            raise SystemExit(f"--part-{campaign} requires the exact Part {campaign.upper()} goal, --seconds 5400 "
                             f"--inputs {CAMPAIGN_INPUT[campaign]} --minutes >=91")
    else:
        for part in ("a", "b", "c"):
            if getattr(T, "part_" + part + "_goal", lambda text: False)(a.goal):
                raise SystemExit(f"Part {part.upper()} requires explicit --part-{part} approval")
    if not 60 <= a.seconds <= seconds_max:
        raise SystemExit(f"--seconds must be 60..{seconds_max} (got {a.seconds})")
    if inputs is not None and not 1 <= inputs <= maximum:
        raise SystemExit(f"--inputs must be 1..{maximum} (got {inputs})")
    from arti_stardew_command import baca_sesi
    dll = MOD / "ArtiStardewBridge.dll"
    actual = T.identity(root=WORKTREE, artifact=dll)
    assert not actual["dirty"], "Stage 2 worktree is dirty; commit first"
    if not getattr(a, "owner_text", None) or not getattr(a, "owner_url", None):
        raise SystemExit("ordinary approval requires --owner-text and --owner-url")
    cfg_bytes = TRIAL_CONFIG.read_bytes()
    cfg = json.loads(cfg_bytes)
    profile = {k: cfg.get(k) for k in ("stardew_decision_provider_kind", "stardew_decision_model", "stardew_decision_effort")}
    session = baca_sesi(str(COMMAND_DIR)).session_id
    now = time.time()
    backups = sorted(p for p in PREP.glob(f"save-backup-{SAVE_NAME.split('_')[0]}-*") if p.is_dir())
    if not backups:
        raise SystemExit(f"no backup for selected save {SAVE_NAME}")
    grant = dict(envelope=T.ENVELOPE, status="APPROVED", owner_resolution_url=a.owner_url,
                 owner_statement=a.owner_text, modes=["combined"], identity=actual, profile=profile, session=session,
                 issued_at=now, expires_at=now + 60 * a.minutes, save_resolved=True,
                 save={"name": SAVE_NAME, "backup": str(backups[-1])},
                 limits=dict(provider=64, input=48 if inputs is None else inputs, seconds=a.seconds,
                             steps=T.steps_approval_max(a.goal) if hasattr(T, "steps_approval_max") else 128),
                 journal_path=str(JOURNAL_DIR),
                 config_sha256=hashlib.sha256(cfg_bytes).hexdigest(), owner_text=a.goal)
    if inputs is not None:
        grant["input_decision"] = f"operator --inputs {inputs} (owner default 48)"  # FIXB24: recorded, OPS section 4
    if campaign:
        from arti_stardew_part_a import limits_for
        grant.update(campaign="skills_w1_part_" + campaign, limits=limits_for(campaign))
    APPROVAL.write_text(json.dumps(grant, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"written": str(APPROVAL), "identity": actual, "session": session, "expires_in_min": a.minutes}, indent=1))


def cmd_live7_approval(a):
    """Automatic immutable day/attempt record; source/DLL pin comes from the cycle.

    Reusing an identity needs no human renewal. Never rewrite a prior approval.
    """
    if not all(getattr(a, k, None) for k in ("identity_file", "output", "journal", "cycle", "day_trial")):
        raise SystemExit("--live7 requires --identity-file --output --journal --cycle --day-trial")
    if any(getattr(a, "part_" + p, False) for p in ("a", "b", "c")) or a.inputs is not None:
        raise SystemExit("live7_has_no_numeric_caps")
    sys.path.insert(0, str(WORKTREE / "scripts"))
    sys.path.insert(0, str(WORKTREE))
    import stardew_m1_trial as T
    from arti_stardew_command import baca_sesi
    from arti_stardew_live7 import GOAL
    actual = T.identity(root=WORKTREE, artifact=MOD / "ArtiStardewBridge.dll")
    pinned = json.loads(Path(a.identity_file).read_text(encoding="utf-8"))
    if actual.get("dirty") or actual != pinned:
        raise SystemExit("live7_source_or_dll_drift")
    cfg_bytes = TRIAL_CONFIG.read_bytes()
    cfg = json.loads(cfg_bytes)
    native = baca_sesi(str(COMMAND_DIR))
    session = native.session_id
    if not native.mentah.get("runtime_instance_id"):
        raise SystemExit("live7_native_scope_missing")
    state_path = Path(a.identity_file).resolve().parent / "cycle_state.json"
    save = dict(name=SAVE_NAME)
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("save_id"):
            # The world is resolved natively before the first disk save exists.
            # Never admit this exception using just an operator-supplied name.
            scope = state.get("stop_scope") or {}
            if (state.get("schema") != "live7_cycle.v1" or state.get("cycle") != a.cycle
                    or (state.get("save_identity") or {}).get("save_id") != state["save_id"]
                    or (state.get("save_identity") or {}).get("session_id") != session
                    or scope.get("session_id") != session
                    or scope.get("runtime_instance_id") != native.mentah.get("runtime_instance_id")
                    or native.mentah.get("world_ready") is not True
                    or native.mentah.get("is_main_player") is not False
                    or str(native.mentah.get("player_id")) != str(state.get("player_id"))
                    or native.mentah.get("player_name") != state.get("player_name")):
                raise SystemExit("live7_native_save_identity_mismatch")
            save = dict(name=state.get("save_name"), id=state["save_id"], basis="native_scene",
                        folder_pending=not bool(state.get("save_folder")))
    if "id" not in save and not (paths.SAVES / SAVE_NAME / SAVE_NAME).is_file():
        raise SystemExit("live7_save_missing")
    grant = dict(envelope=T.ENVELOPE, status="APPROVED", automatic=True, campaign="live7",
                 owner_resolution_url="automatic:live7-owner-decisions-2026-10-20",
                 owner_statement="Live7 automatic approval; owner HOST, ARTI joins; no numeric caps.",
                 owner_text=GOAL, modes=["combined"], identity=actual,
                 profile={k: cfg.get(k) for k in ("stardew_decision_provider_kind", "stardew_decision_model", "stardew_decision_effort")},
                 session=session, issued_at=time.time(), expires_at=None, save_resolved=True,
                 save=save, limits=dict.fromkeys(("provider", "input", "seconds", "steps")),
                 config_sha256=hashlib.sha256(cfg_bytes).hexdigest(), journal_path=str(Path(a.journal).resolve()),
                 cycle=a.cycle, day_trial=a.day_trial,
                 cycle_id=str(Path(a.identity_file).resolve().parent),
                 native_scope=dict(session_id=session, runtime_instance_id=native.mentah.get("runtime_instance_id")),
                 live7_resume=json.loads(Path(a.resume_file).read_text(encoding="utf-8")) if a.resume_file else {})
    target = Path(a.output)
    with target.open("x", encoding="utf-8") as f:
        json.dump(grant, f, indent=2, ensure_ascii=False)
    print(json.dumps({"written": str(target), "identity": actual, "session": session, "automatic": True}))


def cmd_preserve_journal(a):
    """Move RUN/journal aside so the next `run` starts on an empty journal. Never deletes; refuses to overwrite."""
    if not JOURNAL_DIR.exists():
        print(json.dumps({"preserved": None, "note": "no journal folder; nothing to move"})); return
    dest = RUN / f"journal-attempt{a.label}-{stamp()}"
    if dest.exists():
        raise SystemExit(f"{dest} already exists; not overwriting")
    try:
        JOURNAL_DIR.rename(dest)
    except OSError as e:  # Windows: a `tail -f`, an editor or a monitor holds the folder
        raise SystemExit(f"cannot move {JOURNAL_DIR} ({e}); something holds it open (tail -f? editor?). Stop it and retry; never delete it.")
    print(json.dumps({"preserved": str(dest), "files": sorted(p.name for p in dest.iterdir())}))


def run_command(report, *, skills: bool = False, envelope_ok: bool = False, overnight: bool = False,
                inputs_ok: bool = False, inputs: int | None = None, day_start: bool = False) -> list:
    """The stardew_m1_trial.py argv of `run` (pure; tested). ``skills`` passes --skills (skills tahap 1 sessions);
    ``envelope_ok`` passes --skills-envelope-ok (opt-in raise of the approved input to the skill plan's envelope);
    FIXB23 D-B20-2: ``inputs_ok`` passes --inputs-ok (a combat goal's envelope), ``inputs`` passes --inputs N."""
    return ([str(PY), "-B", str(WORKTREE / "scripts/stardew_m1_trial.py"), "--mode", "combined"]
            + (["--skills"] if skills else []) + (["--skills-envelope-ok"] if envelope_ok else [])
            + (["--overnight"] if overnight else [])
            + (["--inputs-ok"] if inputs_ok else []) + (["--inputs", str(int(inputs))] if inputs is not None else [])
            + ["--approval", str(APPROVAL), "--artifact", str(MOD / "ArtiStardewBridge.dll"), "--config", str(TRIAL_CONFIG),
               "--report", str(report)])


DAY_START_MENUS = ("ShippingMenu", "LevelUpMenu")  # FIXB17: the end-of-night screens the day-start rule clears


def pose_gate(pose, *, overnight: bool = False, day_start: bool = False) -> str | None:
    """OPS trial start gate (live batch 6: V2B6ACC4a/4b started with a knock-out dialog / the bed question open -> zero-input
    trials -> global stop). None = the trial may start; else the refusal text. Pure; tested."""
    if not isinstance(pose, dict):
        return "no pose read (the farmhand did not answer on the port within 2.5 s); re-check the game, then retry"
    # FIXB25 D-SOAK-2: --day-start = the same start on an end-of-night screen, without the runner's overnight wait
    if (overnight or day_start) and pose.get("active_menu") in DAY_START_MENUS:
        # FIXB17 (MS11 / MS13 / D1 witness): an end-of-night screen is exactly what the trial's day-start pre-phase clears
        return None
    if pose.get("is_player_free") is not True or pose.get("active_menu") is not None:
        return (f"pose not ready: free={pose.get('is_player_free')} menu={pose.get('active_menu')} "
                f"loc={pose.get('location')} tile={pose.get('tile')} time={pose.get('time_of_day')}; "
                "clear the menu / wait until the farmer is free, then retry (never launch in the same command)")
    return None


def read_pose():
    import poll_state  # binds the mod port for 2.5 s: only between trials
    return poll_state.sample()


def cmd_run(_a):
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    if (JOURNAL_DIR / "g1_journal.jsonl").exists():
        raise SystemExit("journal already used: move run/journal aside (preserve) before a new attempt")
    grant = None
    if getattr(_a, "goal_file", None):
        grant = json.loads(APPROVAL.read_text(encoding="utf-8"))
        goal = Path(_a.goal_file).read_text(encoding="utf-8-sig").strip()
        if goal != grant.get("owner_text"):
            raise SystemExit("--goal-file does not match approved owner_text; obtain a matching approval")
    if not getattr(_a, "no_pose_gate", False):
        refusal = pose_gate(read_pose(), overnight=getattr(_a, "overnight", False),
                            day_start=getattr(_a, "day_start", False))
        if refusal is not None:
            raise SystemExit("trial start refused: " + refusal)
    report = RUN / ("report-" + stamp() + ".json")
    cmd = run_command(report, skills=getattr(_a, "skills", False), envelope_ok=getattr(_a, "skills_envelope_ok", False),
                      overnight=getattr(_a, "overnight", False), inputs_ok=getattr(_a, "inputs_ok", False),
                      inputs=getattr(_a, "inputs", None))
    print("exec:", " ".join(cmd), flush=True)
    t0 = time.time()
    grant = grant if grant is not None else json.loads(APPROVAL.read_text(encoding="utf-8"))
    # Leave the same 100-second teardown margin for S15's 1200-second trial.
    timeout = max(1000, grant.get("limits", {}).get("seconds", 0) + 100)
    p = subprocess.run(cmd, cwd=str(WORKTREE), text=True, capture_output=True, timeout=timeout)
    (RUN / (report.stem + "-stdout.txt")).write_text(p.stdout, encoding="utf-8")
    (RUN / (report.stem + "-stderr.txt")).write_text(p.stderr, encoding="utf-8")
    print(json.dumps({"exit": p.returncode, "seconds": round(time.time() - t0, 1), "report": str(report)}))
    if report.exists():
        rep = json.loads(report.read_text(encoding="utf-8"))
        print(json.dumps({k: rep.get(k) for k in SUMMARY_KEYS}, indent=1, ensure_ascii=False)[:6000])
    print("stderr tail:", p.stderr[-2500:])


# FIXB23 review: the run summary keys (+ the input envelope: the needed number of a refused combat trial, the applied one)
SUMMARY_KEYS = ("status", "reason", "reserved", "elapsed_seconds", "final_goal", "physical_inputs", "physical_requests",
                "input_envelope_needed", "trial_input_envelope")


def cmd_procs(_a):
    out = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True, text=True).stdout
    print("\n".join(l for l in out.splitlines() if "Stardew" in l or "SMAPI" in l or "StardewModdingAPI" in l) or "(none)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", help="write trial-config.json").set_defaults(f=cmd_config)
    d = sub.add_parser("deploy", help="back up + install the mod DLL and trial config (SMAPI must be closed)")
    d.add_argument("--dll", default=str(paths.DLL), help="default: %(default)s")
    d.add_argument("--preset", choices=sorted(PRESETS), default="v1",
                   help="trial flag preset: v1 (default), v2-pad (pad latch + Draw path), v2-probe (probe DLL)")
    d.set_defaults(f=cmd_deploy)
    sub.add_parser("restore", help="restore the newest mod backup").set_defaults(f=cmd_restore)
    l = sub.add_parser("launch", help="start the HOST or the farmhand (no clicks)")
    l.add_argument("role", choices=["host", "farmhand"]); l.set_defaults(f=cmd_launch)
    sub.add_parser("status", help="farmhand session.json summary").set_defaults(f=cmd_status)
    s_ = sub.add_parser("_supervise"); s_.add_argument("role"); s_.set_defaults(f=cmd_supervise)
    ap_ = sub.add_parser("approval", help="write approval.json (session-pinned)")
    ap_.add_argument("--owner-text"); ap_.add_argument("--owner-url")
    ap_.add_argument("--live7", action="store_true", help="automatic immutable Live7 day approval, no numeric caps")
    for name in ("identity-file", "output", "journal", "cycle", "day-trial", "resume-file"):
        ap_.add_argument("--" + name)
    goal = ap_.add_mutually_exclusive_group()
    goal.add_argument("--goal", default="Kumpulkan kayu dari pohon terdekat dengan kapak, lalu simpan kayunya ke chest.")
    goal.add_argument("--goal-file", type=Path, help="UTF-8 owner goal to pin in approval.json")
    ap_.add_argument("--minutes", type=int, default=45)
    ap_.add_argument("--part-a", action="store_true", help="explicit 3-day Skills W1 Part A envelope")
    ap_.add_argument("--part-b", action="store_true", help="explicit town/adventure Skills W1 Part B envelope")
    ap_.add_argument("--part-c", action="store_true", help="explicit progress-day Skills W1 Part C envelope (1,300 inputs, owner 2026-10-20)")
    ap_.add_argument("--seconds", type=int, default=600, help="trial wall limit (60..600; sleep/animal up to 900, S15 alone up to 1200; default 600)")
    ap_.add_argument("--inputs", type=int, default=None, metavar="N",
                     help="approved limits.input: 1..160, animal routine up to 280, S15 alone up to 320; owner default 48; explicit operator decision")
    ap_.set_defaults(f=cmd_approval)
    pj = sub.add_parser("preserve-journal", help="move a used run/journal aside (never deletes)")
    pj.add_argument("label", help="label of the attempt that wrote the journal"); pj.set_defaults(f=cmd_preserve_journal)
    r = sub.add_parser("run", help="run the combined trial (refuses a used journal)")
    r.add_argument("--goal-file", type=Path, help="require this UTF-8 goal to match the pinned approval")
    r.add_argument("--skills", action="store_true", help="pass --skills to stardew_m1_trial.py (skills tahap 1)")
    r.add_argument("--skills-envelope-ok", action="store_true",
                   help="pass --skills-envelope-ok (allow the skill plan's input envelope above the approval)")
    r.add_argument("--overnight", action="store_true",
                   help="FIXB17: span a night: start in an end-of-night screen (ShippingMenu / LevelUpMenu: the day-start "
                        "pre-phase clears it) and keep the runtime alive after the goal until the day-start rule cleared")
    r.add_argument("--inputs-ok", action="store_true",
                   help="FIXB23: pass --inputs-ok (a combat goal raises the approved input to its envelope, kill 1 = 134)")
    r.add_argument("--inputs", type=int, default=None, metavar="N",
                   help="pass --inputs N (1..160, or 1..280 for the animal routine; explicit operator envelope)")
    r.add_argument("--day-start", action="store_true",
                   help="FIXB25: start on a farmhand ShippingMenu / LevelUpMenu (the runner's day-start pre-phase clears it, "
                        "then the goal runs); unlike --overnight no wait after the goal")
    r.add_argument("--no-pose-gate", action="store_true",
                   help="skip the start gate (free=True, menu=None); only for a trial that deliberately starts in a menu")
    r.set_defaults(f=cmd_run)
    sub.add_parser("procs", help="list Stardew/SMAPI processes").set_defaults(f=cmd_procs)
    a = ap.parse_args(argv)
    if a.cmd == "approval" and not a.live7 and (not a.owner_text or not a.owner_url):
        ap.error("ordinary approval requires --owner-text and --owner-url")
    if a.cmd == "_supervise" and a.role == "owner-word":
        from dataclasses import asdict
        from scripts.livepath.halt_authority import HandsHeld, RecoveryTimedOut
        try:
            a.f(a)
        except HandsHeld as exc:
            print(json.dumps({"hold": asdict(exc.decision)}), flush=True)
            return 2
        except RecoveryTimedOut as exc:
            print(json.dumps({"timeout": str(exc)}), flush=True)
            return 1
    else:
        rc = a.f(a)
        return rc if a.cmd == "launch" else None  # a refused launch says so to its caller


if __name__ == "__main__":
    sys.exit(main())
