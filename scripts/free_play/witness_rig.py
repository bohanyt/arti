"""Native rig service for the contract-11 witness booking (ADR 96).

``booked(spec=..., output=...)`` is the entry named by ``pin --service``. It binds the
EXISTING services only: the soak launch/closeout steps (``witness_run.py``, ``ui_op.py``,
``pad_watch.py``), ``soak.Baseline``, the command-inbox session/inspect reads, the fix8
``render_ui``/``resolve_ui`` controller encoder, the goal runtime's leased UI executors,
the I1 collector verb wire and the decision provider. No motor, publisher, save edit or
admission rule lives here. Construction and ``check_idle`` are inert: no lock, launch
or game mutation before ``start``/``prepare_capture``.

Each seam is a module-level function so offline tests substitute fakes that honour the
real interface. The service file loaded by the booking delegates to this package module,
so one module identity exists.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arti_runtime.witness import WitnessFailure, render_ui, resolve_ui
from scripts.free_play import soak
from scripts.free_play import witness as W
from scripts.free_play.witness_factory import pinned

OPERATOR = ROOT / "scripts/m1_operator"
# Repo dependencies the service shells out to or imports; pinned by ``pin --artifact``
# so the booked spec names them explicitly (WITNESS.md: refuse unpinned dependencies).
DEPENDENCIES = ("scripts/m1_operator/witness_run.py", "scripts/m1_operator/ui_op.py",
                "scripts/m1_operator/pad_watch.py", "scripts/free_play/i1_collector.py",
                "arti_runtime/witness.py")
UI_VERBS = ("choose", "scroll", "type")
CONTAINER = ("container_exit", "container_transfer")
IDENTITY = W.IDENTITY
GIB = 1024 ** 3
WITNESS_PAGE = ("You are ARTI playing Stardew Valley on your own farmhand. Below is what you see "
                "right now (facts read from the game) and every control you may use at this moment. "
                "Decide what you want to do and pick exactly one control by its index. Leaving, "
                "closing or declining is a real choice when it is listed. Reply with JSON only.")


# ----------------------------------------------------------------------------- seams
def smapi_running():
    return soak.smapi_running()


def owner_idle(cfg, run=None):
    soak.owner_idle(cfg, run)


def trial_running():
    from scripts.m1_operator import win_facts
    return win_facts.trial_running()


def run_operator(script, args, log, env):
    """Existing operator CLI call, exactly as soak.call: one log per invocation."""
    with Path(log).open("xb") as out:
        rc = subprocess.call([sys.executable, "-B", str(OPERATOR / script), *map(str, args)],
                             cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    if rc:
        raise WitnessFailure(f"operator_step_failed:{script}:{rc}")


def read_session(inbox):
    import arti_stardew_command as command
    return command.baca_sesi(inbox, retry_detik=0.25)


def inspect(inbox, session):
    """One inspect_context through the command inbox: no port, no input. Raw envelope or None."""
    import arti_stardew_command as command
    result = command.minta_inspect_context(folder=inbox, sesi=session)
    return result.mentah if result.status == command.SELESAI and isinstance(result.mentah, dict) else None


def helper_preflight(host_out):
    return soak.helper_started_preflight(host_out)


def startup_pause(host_out, run_id, *, release=False):
    soak.startup_pause(host_out, run_id, release=release)


def drawable(runtime, record):
    from scripts.free_play.drawable import DrawableWindows
    windows = DrawableWindows.from_runtime(runtime, record=record)
    windows.check()
    return windows


def host_liveness(host_out, host_pid):
    return soak.host_liveness(host_out, time.time(), host_pid)


def controller_factory(config, *, journal_dir):
    """The existing goal runtime: leased ui_pad / container executors over its own stores."""
    from arti_stardew_runtime import StardewGoalRuntime
    return StardewGoalRuntime(config=config, provider=None, journal_dir=journal_dir, start_worker=False)


def provider_factory(config):
    from arti_stardew_runtime import construct_stardew_decision_provider, stardew_decision_provider_admission
    policy, reason = stardew_decision_provider_admission(config)
    if policy is None:
        raise WitnessFailure("decision_provider_unadmitted:" + reason)
    provider = construct_stardew_decision_provider(config)
    if provider is None:
        raise WitnessFailure("decision_provider_unavailable")
    return provider


def collector_factory(inbox, output, *, save_id, generation):
    from scripts.free_play.i1_collector import Collector
    return Collector(inbox, output, save_id=save_id, owner="witness", generation=generation)


def stage_tools(env, save_name):
    """Existing stage_save catalog/location readers. parse_script accepts None for either."""
    os.environ.update(env)
    from scripts.m1_operator import stage_save
    ops = stage_save.LiveOps()
    catalog = ops.catalog()
    raw = ops.read_bytes(stage_save.save_file(save_name))
    return catalog, stage_save.save_location_names(raw) if raw else None


# ----------------------------------------------------------------------------- helpers
def save_identity(folder):
    """Read-only identity of a restored disposable save (the facts stage_template derives)."""
    folder = Path(folder)
    root = ET.parse(folder / folder.name).getroot()
    player = root.find("player")
    host = int(player.findtext("UniqueMultiplayerID", "0"))
    peers = root.findall("./farmhands/Farmer")
    peers += [e for e in root.findall(".//farmhand") if e.find("UniqueMultiplayerID") is not None]
    unique = {int(p.findtext("UniqueMultiplayerID", "0")): p for p in peers}
    unique.pop(host, None)
    unique.pop(0, None)
    if len(unique) != 1:
        raise WitnessFailure("one_established_farmhand_required")
    peer_id, peer = next(iter(unique.items()))
    if root.findtext("allowChatCheats") != "true":
        raise WitnessFailure("disposable_cheats_flag_required")
    return dict(save_folder=folder.name, save_id=int(root.findtext("uniqueIDForThisGame")), host_id=host,
                farmhand_id=peer_id, farmhand_name=peer.findtext("name"))


def ui_record(run, still_offered):
    """Evaluator action record plus contract result_v1 from the existing LeasedRun."""
    from arti_stardew_generic_ui import contract_result
    return dict(lifecycle=run.lifecycle, outcome=run.outcome, reason=run.reason, request_id=run.request_id,
                cleanup_confirmed=bool(run.cleanup),
                world_effect_status="observed_native_readback" if run.verified else "unobserved",
                native_receipt=dict(applied=run.applied, verified=run.verified, submitted=run.submitted,
                                    readback=run.readback, context_revision=run.context_revision,
                                    input_count=run.input_count, result_v1_source="LeasedRun"),
                result_v1=contract_result(run, still_offered))


def world_record(result):
    """Evaluator action record from a native arti.verb_result/3 envelope; closure is the receipt."""
    executed = result.get("executed") is True
    native = result.get("native_lifecycle")
    released = result.get("released") is True and isinstance(result.get("release_receipt"), dict)
    closed = (result.get("closure") or {}).get("closed") is True
    refusal = result.get("not_executed") if isinstance(result.get("not_executed"), dict) else None
    if native == "uncertain" or (executed and not released):
        lifecycle, contract = "uncertain", dict(state="uncertain", term="native_release_unproven"
                                                if native != "uncertain" else "native_uncertain")
    elif executed and native == "settled":
        lifecycle, contract = "completed", dict(state="done", changed="native_closure" if closed else None)
    elif refusal is not None or native == "not_admitted":
        lifecycle = "rejected"
        contract = dict(state="refused", reason=str((refusal or {}).get("reason") or native)[:128], valid=False)
    else:
        lifecycle, contract = "not_taken", dict(state="refused", reason="no_input", valid=False)
    return dict(lifecycle=lifecycle, outcome=native, reason=contract.get("reason") or contract.get("term"),
                request_id=result.get("request_id"), cleanup_confirmed=released,
                world_effect_status="observed_native_closure" if closed and executed else "unobserved",
                native_receipt=dict(delivered=result.get("delivered"), edges=result.get("edges"),
                                    closure=result.get("closure"), witnesses=result.get("witnesses"),
                                    timing=result.get("timing"), result_v1_source="arti.verb_result/3"),
                result_v1=contract)


def offered_world_targets(context):
    """What the game offers at her grab tile now: the named kind (blacksmith, note) and the generic A button a counter
    like Gunther's gets. Read only from the published facts, with the runtime's own validator (ADR 132)."""
    from arti_stardew_generic_world import parse_targets
    context = context if isinstance(context, dict) else {}
    named = context.get("world_interaction_target")
    return ([dict(named)] if isinstance(named, dict) else []) + list(parse_targets(context))


# ----------------------------------------------------------------------------- service
class Service:
    offline = False

    def __init__(self, *, spec, output):
        self.spec, self.output = spec, Path(output)
        self.cfg = W.read(pinned(spec, spec["rig"]["config"]))
        section = self.cfg.get("rig_service")
        if not isinstance(section, dict) or not section.get("trial_config") or not section.get("host_template"):
            raise WitnessFailure("rig_service_config_required")
        self.section = section
        self.root = Path(self.cfg["root"]).resolve()
        self.game, self.app = Path(self.cfg["game"]), Path(self.cfg["appData"])
        self.prep, self.mod = self.root / "operator-staging", self.game / "Mods/ArtiStardewBridge"
        self.package = Path(spec["package"]["path"])
        self.trial_config = pinned(spec, section["trial_config"])
        self.host_template = Path(section["host_template"]).resolve()
        self.bridge = pinned(spec, self.package / "bridge/ArtiStardewBridge.dll")
        self.helper = pinned(spec, self.package / "helper/ArtiHostHelper.dll")
        self.provenance = W.read(pinned(spec, self.package / "provenance.json"))
        for relative in (*DEPENDENCIES, self.host_template / "host-mods/ArtiStardewBridge/manifest.json"):
            path = Path(relative) if Path(relative).is_absolute() else ROOT / relative
            if not path.is_file() or spec["pins"].get(str(path.resolve())) is None:
                raise WitnessFailure("dependency_unpinned:" + path.name)
            pinned(spec, path)
        self.trial = W.read(self.trial_config)
        from arti_runtime.witness import reject_witness_config
        reject_witness_config(self.trial)
        self.inbox = self.identity = self.dependencies = None
        self.launch = self.runtime = self.host_out = self.host_pid = self.windows = None
        self.controller = self.provider = self._collector = None
        self.catalog = self.locations = None
        self.started = False
        self.reads = self.turns = 0
        self.views, self.last = {}, None

    # ----------------------------------------------------------------- guards
    def check_idle(self):
        """Inert: validates pins, trial admission and native inbox state. No lock or launch."""
        mod_config = W.read(self.mod / "config.json")
        inbox = mod_config.get("CommandInboxDirectory")
        if not inbox:
            raise WitnessFailure("command_inbox_unconfigured")
        self.inbox = Path(inbox)
        if (self.inbox / "stop.json").exists():
            raise WitnessFailure("command_stop_present")
        if (self.inbox / ".i1-writer").exists():
            raise WitnessFailure("native_writer_occupied")
        from arti_stardew_runtime import stardew_decision_provider_admission
        policy, reason = stardew_decision_provider_admission(self.trial)
        if policy is None:
            raise WitnessFailure("decision_provider_unadmitted:" + reason)
        if self.provenance.get("dll_sha256") != W.digest(self.bridge) or self.provenance.get("helper_sha256") != W.digest(self.helper):
            raise WitnessFailure("package_artifact_mismatch")
        self.dependencies = soak.candidate_dependencies(self.bridge, self.provenance)
        if smapi_running():
            raise WitnessFailure("games_running")
        W.write(self.output / "rig-service.json", dict(tag="STAGING", inert=True, credit=False,
            trial_config_sha256=W.digest(self.trial_config), bridge_sha256=self.provenance["dll_sha256"],
            helper_sha256=self.provenance["helper_sha256"], dependencies=sorted(self.dependencies),
            model=dict(kind=policy.provider_kind, model=getattr(policy, "model", None))))

    def check(self):
        if self.started:
            if not smapi_running():
                raise WitnessFailure("native_session_lost")
            if self.host_out is not None and host_liveness(self.host_out, self.host_pid)["lost"]:
                raise WitnessFailure("host_stop_or_lost_telemetry")
            if self.windows is not None:
                self.windows.check()
        if shutil.disk_usage(self.root).free < 10 * GIB:
            raise WitnessFailure("low_disk")

    def wait(self, seconds):
        time.sleep(min(max(float(seconds), 0.0), 120.0))

    def games_closed(self):
        return not smapi_running()

    def trial_running(self):
        return trial_running()

    # ----------------------------------------------------------------- baseline
    def baseline(self, output):
        """The complete soak mutation set: saves, mod/config, DLLs, mailbox, N1 state, journals."""
        if self.inbox is None:
            raise WitnessFailure("booking_not_acquired")
        files = {"bridge.dll": self.mod / "ArtiStardewBridge.dll", "mod.json": self.mod / "config.json",
                 "trial.json": self.prep / "trial-config.json", "approval.json": self.prep / "approval.json",
                 "preferences": self.app / "startup_preferences"}
        for name in ("session.json", "request.json", "result.json", "stop.json"):
            files["inbox-" + name] = self.inbox / name
        for name in soak.N1_RESTORED:
            files["n1-" + name] = self.inbox / name
        for name in self.dependencies:
            files["dependency-" + name] = self.mod / name
        journals = Path(os.environ.get("LOCALAPPDATA", str(self.app.parent))) / "ArtiStardewBridge"
        names = (self.binding(output)['snapshot']['save_name'],)
        return soak.Baseline(output, files, self.app / "Saves", owned_saves=names, file_sets=soak.journal_file_sets(
            journals, {"discoveries": (self.mod / "data", "discoveries-*.json")}))

    # ----------------------------------------------------------------- lifecycle
    def env(self, launch):
        env = dict(os.environ, ARTI_M1_PREP=str(self.prep), ARTI_M1_RUN=str(launch / "runtime"),
                   ARTI_M1_WORKTREE=str(ROOT), ARTI_CHECKOUT=str(ROOT), ARTI_VENV_PYTHON=sys.executable,
                   ARTI_STARDEW_GAME=str(self.game), ARTI_M1_TOOLS=str(OPERATOR),
                   ARTI_STARDEW_SAVES=str(self.app / "Saves"),
                   ARTI_HOST_SMAPI_LOG=str(self.app / "ErrorLogs/SMAPI-latest.txt"),
                   ARTI_FARMHAND_SMAPI_LOG=str(self.app / "ErrorLogs/SMAPI-latest.player-2.txt"))
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["ARTI_M1_SAVE_NAME"] = self.identity["save_folder"]
        return env

    def binding(self, output):
        binding = self.spec["capabilities"].get(Path(output).name)
        if not binding or not binding.get("snapshot", {}).get("save_name"):
            raise WitnessFailure("unknown_case_output")
        return binding

    def start(self, output):
        """Existing soak launch order over the restored disposable save; never ARMED, no day runner."""
        output = Path(output)
        if self.started or smapi_running():
            raise WitnessFailure("games_running")
        binding = self.binding(output)
        save = self.app / "Saves" / binding["snapshot"]["save_name"]
        self.identity = save_identity(save)
        index = len(list(output.glob("launch-*"))) + 1
        launch = output / f"launch-{index}"
        launch.mkdir(exist_ok=False)
        env = self.env(launch)
        self.launch, self.runtime, self.host_out = launch, launch / "runtime", launch / "host"
        shutil.copy2(self.bridge, self.mod / "ArtiStardewBridge.dll")
        for name, source in self.dependencies.items():
            shutil.copy2(source, self.mod / name)
        join = W.read(self.mod / "config.json")
        before = W.digest(self.mod / "config.json")
        previous = join.get("CharacterCreationEnabled")
        join["CharacterCreationEnabled"] = True
        W.write(self.mod / "config.json", join)
        W.write(launch / "STAGING-join-config.json", dict(tag="STAGING", before_sha256=before,
                after_sha256=W.digest(self.mod / "config.json"), field="CharacterCreationEnabled",
                before=previous, after=True, reason="existing named farmhand join-slot facts; Baseline restores"))
        W.write(self.prep / "trial-config.json", dict(self.trial, stardew_host_controller="harness"))
        template = launch / "template"
        shutil.copytree(self.host_template, template)
        host_mod = template / "host-mods/ArtiStardewBridge"
        shutil.copy2(self.bridge, host_mod / "ArtiStardewBridge.dll")
        for name, source in self.dependencies.items():
            shutil.copy2(source, host_mod / name)
        env["ARTI_M1_DLL"] = str(host_mod / "ArtiStardewBridge.dll")
        run_operator("witness_run.py", ["prepare-run", "--run-dir", self.runtime, "--template", template,
                     "--sha256", self.provenance["dll_sha256"], "--row", "free-play",
                     "--snapshot", "unused-no-restore"], launch / "prepare.log", env)
        helper_dir = self.runtime / "batch-host/host-mods/ArtiHostHelper"
        shutil.copytree(self.helper.parent, helper_dir)
        run_id = f"witness-{output.name}-{index}"
        W.write(helper_dir / "config.json", dict(Enabled=True, RunId=run_id, OutputDirectory=str(self.host_out),
                SaveFolder=self.identity["save_folder"], SaveId=self.identity["save_id"],
                HostId=self.identity["host_id"], FarmhandId=self.identity["farmhand_id"],
                HostDelaySeconds=int(self.section.get("host_delay", 0)), StartupPauseSeconds=300,
                SleepAt=int(self.section.get("host_sleep_at", 2400)), GiftTimes=[], GiftWhen="idle",
                GiftIdleSeconds=20, GiftItem="(O)388", StageHostGiftCount=0))
        soak.staged_mod_preflight(((self.mod, "ArtiStardewBridge.dll", self.provenance["dll_sha256"]),
            (self.runtime / "batch-host/host-mods/ArtiStardewBridge", "ArtiStardewBridge.dll", self.provenance["dll_sha256"]),
            (helper_dir, "ArtiHostHelper.dll", self.provenance["helper_sha256"])))
        W.write(launch / "launch.json", dict(tag="STAGING", credit=False, run_id=run_id, case=output.name,
                save_folder=self.identity["save_folder"], code_sha=self.spec["code_sha"],
                dll_sha256=self.provenance["dll_sha256"], trial_config_sha256=W.digest(self.trial_config)))
        owner_idle(self.cfg)
        self.started = True  # closeout owns everything from the first launch call
        run_operator("witness_run.py", ["launch-host", "--recovery", self.runtime / "batch-host"], launch / "launch-host.log", env)
        deadline = time.monotonic() + 90
        while not (self.host_out / "status.json").exists():
            if (self.host_out / "HOST_STOP").exists():
                raise WitnessFailure("host_helper_stopped")
            if time.monotonic() >= deadline:
                raise WitnessFailure("host_load_timeout")
            time.sleep(.5)
        W.write(launch / "helper-preflight.json", helper_preflight(self.host_out))
        startup_pause(self.host_out, run_id)
        record = self.runtime / "batch-host/host-launch.json"
        self.host_pid = (W.read(record) or {}).get("pid") if record.is_file() else None
        owner_idle(self.cfg, launch)
        run_operator("witness_run.py", ["launch-farmhand", "--join-slot", "name:" + self.identity["farmhand_name"]],
                     launch / "launch-farmhand.log", env)
        startup_pause(self.host_out, run_id, release=True)
        max_hour = int(binding.get("max_hour", self.section.get("max_hour", 26)))
        run_operator("witness_run.py", ["wait-ready", "--who", "farmhand", "--max-hour", max_hour, "--timeout", 30],
                     launch / "ready.log", env)

        def window_record(row):
            with (launch / "drawable-windows.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(dict(utc=soak.utc(), **row)) + "\n")
        self.windows = drawable(self.runtime, window_record)
        host_hwnd = (self.runtime / "host-hwnd.txt").read_text(encoding="utf-8").strip()
        run_operator("ui_op.py", ["fg", host_hwnd], launch / "host-fg.log", env)
        run_operator("pad_watch.py", ["preflight"], launch / "pad-preflight.log", env)
        self.views, self.last = {}, None

    def verify_session(self):
        """Native session facts of the owned farmhand, read from the existing inbox session.json."""
        session = read_session(self.inbox)
        raw = session.mentah
        if not session.world_ready or not session.inbox_enabled:
            raise WitnessFailure("native_session_not_ready")
        if session.is_main_player:
            raise WitnessFailure("farmhand_session_required")
        if session.player_name != self.identity["farmhand_name"]:
            raise WitnessFailure("farmhand_identity_mismatch")
        if raw.get("halt"):
            raise WitnessFailure("native_halt_present")
        if any(raw.get(k) in (None, "") for k in IDENTITY):
            raise WitnessFailure("native_identity_unavailable")
        self.native = {k: raw[k] for k in IDENTITY}
        W.write(self.launch / "session-native.json", dict(self.native, player_name=session.player_name,
                location_id=session.location_id, world_ready=True, generation=raw.get("generation"),
                fenced_generation=raw.get("fenced_generation")))

    # ----------------------------------------------------------------- observation
    def observe(self):
        """Raw native inspect_context retained under native/; identity and tick are native fields."""
        session = read_session(self.inbox)
        raw = inspect(self.inbox, session)
        if raw is None:
            raise WitnessFailure("readback_unavailable")
        self.reads += 1
        folder = self.output / "native"
        folder.mkdir(exist_ok=True)
        path = folder / f"{self.reads:04d}-inspect.json"
        W.write(path, raw)
        W.write(folder / f"{self.reads:04d}-session.json", session.mentah)
        tick = raw.get("captured_ticks")
        context = raw.get("context")
        if type(tick) is not int or not isinstance(context, dict):
            raise WitnessFailure("readback_unavailable")
        view = None
        if isinstance(context.get("ui_pad"), dict):
            from arti_stardew_ui_pad import baca_ui_pad
            view = baca_ui_pad(context, age_s=0.0)
        if view is not None:
            affordances = render_ui(view)
        else:
            published = context.get("affordances")
            affordances = deepcopy(published) if isinstance(published, list) else []
        pointer = path.relative_to(self.output).as_posix()
        sample = dict(state=raw, affordances=affordances, tick=tick, raw_pointer=pointer, authoritative=True,
                      complete=True, stale=False, **{k: raw.get(k) for k in IDENTITY})
        self.views[pointer] = view
        self.last = sample
        return sample

    # ----------------------------------------------------------------- one model turn
    def choose(self, offer):
        """One real ARTI turn through the existing provider: only the supplied facts and controls."""
        if self.provider is None:
            self.provider = provider_factory(self.trial)
        shown = offer["affordances"]
        page = dict(facts=offer["state"], controls=[dict(index=i, verb=e["verb"], args=e["args"],
                    **({"role": e["role"]} if e.get("role") else {})) for i, e in enumerate(shown)])
        messages = [dict(role="system", content=WITNESS_PAGE),
                    dict(role="user", content=json.dumps(page, ensure_ascii=False, sort_keys=True))]
        schema = dict(type="object", additionalProperties=False, required=["choice", "why"],
                      properties=dict(choice=dict(type="integer", minimum=0, maximum=len(shown) - 1),
                                      why=dict(type="string", maxLength=300)))
        self.turns += 1
        text = self.provider.complete_reflection(messages, schema,
                                                 timeout_sec=float(self.section.get("decision_timeout_s", 120)))
        try:
            reply = json.loads(text)
            index = reply["choice"]
        except (ValueError, KeyError, TypeError):
            raise WitnessFailure("model_reply_invalid") from None
        W.write(self.output / f"turn-{self.turns}.json", dict(page=page, reply=reply))
        if type(index) is not int or not 0 <= index < len(shown):
            raise WitnessFailure("model_choice_invalid")
        return deepcopy(shown[index])

    # ----------------------------------------------------------------- execution
    def collector(self):
        if self._collector is None:
            raw = read_session(self.inbox).mentah
            generation = max(raw.get("generation") or 0, raw.get("fenced_generation") or 0) + 1
            index = len(list(self.output.glob("collector-*"))) + 1
            self._collector = collector_factory(self.inbox, self.output / f"collector-{index}",
                                                save_id=self.identity["save_id"], generation=generation)
        return self._collector

    def execute(self, entry):
        if self.last is None:
            raise WitnessFailure("observe_before_execute")
        if entry["verb"] in UI_VERBS or entry.get("args", {}).get("control"):
            view = self.views.get(self.last["raw_pointer"])
            if view is None:
                raise WitnessFailure("not_published")
            primitive, args = resolve_ui(view, entry)
            if self.controller is None:
                config = dict(self.trial, stardew_command_dir=str(self.inbox), stardew_host_controller="harness")
                self.controller = controller_factory(config, journal_dir=self.output / "controller")
            if primitive in CONTAINER:
                run = self.controller._live7_container_close(args, view.receiver_token, lambda **kw: view, primitive)
            else:
                run, _ = self.controller._run_ui_pad(primitive, args, origin="witness")
            fresh = self.views.get(self.last["raw_pointer"])
            return ui_record(run, entry in (render_ui(fresh) if fresh is not None else []))
        payload = dict(op="verb", v="arti.verb_request/1", request_id=uuid.uuid4().hex, attempt_id=uuid.uuid4().hex,
                       actor="model", verb=entry["verb"], args=deepcopy(entry["args"]), pins=deepcopy(entry.get("pins") or {}))
        timeout = float(self.binding(self.output).get("timeout_s", 20))
        result = self.collector().exchange(payload, acquired=lambda s, seq: True, closed=lambda req, res: True,
                                           monitor=self.check, timeout=min(timeout, 600))
        return world_record(result)

    def press_world(self, target):
        """One world_interact on the target the game offers now, through the same leased route as her own press (ADR 132)."""
        if self.controller is None:
            config = dict(self.trial, stardew_command_dir=str(self.inbox), stardew_host_controller="harness")
            self.controller = controller_factory(config, journal_dir=self.output / "controller")
        _, pid = self.controller._binding_view()
        ctx = self.controller._ui_key_context(pid)
        if ctx is None:
            raise WitnessFailure("world_context_unavailable")

        def fresh():
            if target not in offered_world_targets((inspect(self.inbox, read_session(self.inbox)) or {}).get("context")):
                raise ValueError("world_target_changed")
        from arti_stardew_command import OUTCOME_SUKSES_INTERACT
        run = self.controller._run_leased_once("world_interact", dict(target), ctx, origin="witness_buttons", revalidate=fresh,
                                               expect_outcome=OUTCOME_SUKSES_INTERACT.get(target.get("kind")))
        return ui_record(run, False)

    def await_result(self, result, timeout_s=20):
        """Re-read the native terminal receipt for the same request; never a second input."""
        request_id = result.get("request_id")
        collector = self.collector()

        def terminal():
            try:
                current = json.loads((self.inbox / "result.json").read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                return None
            if current.get("request_id") != request_id or current.get("native_lifecycle") == "admitted":
                return None
            return current
        return world_record(collector.poll(terminal, monitor=self.check, timeout=min(float(timeout_s), 600),
                                           label="terminal:" + str(request_id)))

    # ----------------------------------------------------------------- staging hooks
    def run_script(self, argv):
        """Existing stage_save checked farmhand staging in its own process; rc returned."""
        env = self.env(self.launch)
        index = len(list(self.launch.glob("stage-save-*.log"))) + 1
        with (self.launch / f"stage-save-{index}.log").open("wb") as log:
            return subprocess.call([sys.executable, "-B", str(OPERATOR / "stage_save.py"), *map(str, argv)],
                                   cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=300)

    def staging_tools(self):
        if self.catalog is None:
            self.catalog, self.locations = stage_tools(self.env(self.launch), self.identity["save_folder"])

    def stage(self, commands, output):
        from scripts.free_play import i1_staging
        self.staging_tools()
        i1_staging.stage(dict(target="farmhand", commands=list(commands)), collector=self.collector(), output=output,
                         catalog=self.catalog, locations=self.locations, run_script=self.run_script,
                         trial_running=self.trial_running)

    def prepare_capture(self, *, capability, recipe, binding, output):
        """Start the owned session and apply the recipe's persistent checked STAGING. No model action."""
        self.start(output)
        commands = recipe["stage"].get("debug") or []
        if commands:
            self.stage(commands, Path(output) / "STAGING-debug")

    def resume_fixture(self, *, capability, binding, output):
        """Transient UI/state only, declared per binding as checked farmhand commands."""
        commands = binding.get("resume") or []
        index = len(list(Path(output).glob("STAGING-resume-*"))) + 1
        target = Path(output) / f"STAGING-resume-{index}"
        if commands:
            self.stage(commands, target)
        else:
            target.mkdir(exist_ok=False)
            W.write(target / "STAGING.json", dict(tag="STAGING", credit=False, commands=[], note="no transient state declared"))

    # ----------------------------------------------------------------- closeout
    def closeout(self, output):
        """Owned barrier-resolved closure; closes only recorded windows; never kills a game."""
        output = Path(output)
        closure = dict(tag="STAGING", barrier_resolved=False, games_closed=None)
        for attr in ("controller", "provider", "_collector"):
            handle = getattr(self, attr)
            if handle is not None and hasattr(handle, "close"):
                try:
                    handle.close()
                finally:
                    setattr(self, attr, None)
        self.controller = self.provider = self._collector = None
        launch = self.launch
        if launch is None:
            closure.update(barrier_resolved=not smapi_running(), games_closed=not smapi_running(), launch=None)
            return closure
        env = self.env(launch)
        for role, source in (("host", self.app / "ErrorLogs/SMAPI-latest.txt"),
                             ("farmhand", self.app / "ErrorLogs/SMAPI-latest.player-2.txt")):
            if source.exists():
                shutil.copy2(source, launch / (role + "-SMAPI.txt"))
        if smapi_running():
            barrier = self.host_out / "barrier.json"
            deadline = time.monotonic() + 120
            while True:
                try:
                    if time.time() - barrier.stat().st_mtime < 5 and W.read(barrier).get("saving") is False:
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    closure.update(reason="save_barrier_unresolved", games_closed=False)
                    W.write(launch / "closure.json", closure)
                    return closure  # do not close games or restore; baseline retained
                time.sleep(.5)
        for role, record, hwnd in (("farmhand", "farmhand-launch.json", "fh-hwnd.txt"),
                                   ("host", "batch-host/host-launch.json", "host-hwnd.txt")):
            launched, handle = self.runtime / record, self.runtime / hwnd
            if launched.exists() and handle.exists():
                index = len(list(launch.glob(f"close-{role}-*.log"))) + 1
                run_operator("ui_op.py", ["close", handle.read_text(encoding="utf-8").strip(),
                             "--expect-pid", W.read(launched)["pid"]], launch / f"close-{role}-{index}.log", env)
        deadline = time.monotonic() + 30
        while smapi_running() and time.monotonic() < deadline:
            time.sleep(1)
        if self.host_out.exists():
            (self.host_out / "STOP").write_text("witness finalization\n", encoding="utf-8")
        closed = not smapi_running()
        if closed:
            final = launch / "native-final"
            final.mkdir(exist_ok=True)
            for name in ("session.json", "result.json", "halt.json", "mod_exit.json", "rearm.json",
                         "stop.json", "stop_all.json", "control.json"):
                if (self.inbox / name).is_file():
                    shutil.copy2(self.inbox / name, final / name)
            W.write(final / "MANIFEST.json", soak.manifest(final))
            owner_idle(self.cfg)
            self.started, self.windows = False, None
        closure.update(barrier_resolved=closed, games_closed=closed, launch=launch.name,
                       **({} if closed else dict(reason="smapi_remains_do_not_kill")))
        W.write(launch / "closure.json", closure)
        return closure


def button_spec(spec, rig_config, output):
    """The witness_buttons booking (ADR 132): this checkout's Invoke-Soak spec, pinned here for one run; no model section."""
    package = Path(spec["provenance"]).parent
    config = W.read(rig_config)
    section = config.get("rig_service") or dict(trial_config=spec["trialConfig"], host_template=spec["hostTemplate"])
    resolved = dict(config, rig_service=section)
    pinned_config = Path(output) / "button-rig-config.json"
    W.write(pinned_config, resolved)
    bridge, helper, provenance = package / "bridge/ArtiStardewBridge.dll", package / "helper/ArtiHostHelper.dll", package / "provenance.json"
    paths = [pinned_config, bridge, helper, provenance, Path(section["trial_config"]),
             Path(section["host_template"]) / "host-mods/ArtiStardewBridge/manifest.json", *(ROOT / d for d in DEPENDENCIES)]
    save_name = Path(spec["templates"]["Standard"]).name
    return dict(code_sha=spec["codeSha"], rig=dict(config=str(pinned_config)), package=dict(path=str(package)),
                pins={str(Path(p).resolve()): W.digest(p) for p in paths},
                capabilities={Path(output).name: dict(snapshot=dict(save_name=save_name), max_hour=26)})


def booked(*, spec, output):
    # The booking loads this file under its own module name; delegate to the package module so
    # tests and the booking share one Service definition and one set of seams.
    from scripts.free_play import witness_rig
    return witness_rig.Service(spec=spec, output=output)
