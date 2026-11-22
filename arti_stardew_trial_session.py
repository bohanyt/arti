"""Issue #235 supervised trial seam for the ordinary Stardew bridge runtime.

Importing this module is inert.  The bridge calls :func:`prepare_bridge_trial`
only at process startup; without the explicit trial environment it returns
immediately and normal bridge behaviour is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping


_APPROVAL_ENV = "ARTI_STARDEW_BRIDGE_TRIAL_APPROVAL"
_CONFIG_ENV = "ARTI_STARDEW_BRIDGE_TRIAL_CONFIG"
_ARTIFACT_ENV = "ARTI_STARDEW_BRIDGE_TRIAL_ARTIFACT"
_REPORT_ENV = "ARTI_STARDEW_BRIDGE_TRIAL_REPORT"
_REQUIRED_ENV = (_APPROVAL_ENV, _CONFIG_ENV, _ARTIFACT_ENV, _REPORT_ENV)
_PROFILE_KEYS = (
    "stardew_decision_provider_kind",
    "stardew_decision_model",
    "stardew_decision_effort",
)
_MAX_LIMITS = {"provider": 240, "input": 48, "seconds": 600, "steps": 128}


def _effective_lower(current: Any, approved: int) -> int:
    """Explicit malformed/negative caps admit no work; zero stays zero."""
    if type(current) is int and current >= 0:
        return min(current, approved)
    return 0


def _validate_bridge_grant(
    grant: object,
    *,
    actual: Mapping[str, object],
    profile: Mapping[str, object],
    session: str | None,
    now: float,
    envelope: str,
) -> dict[str, int]:
    """Validate the bridge grant without inventing a standalone owner goal."""
    if not isinstance(grant, dict) or grant.get("envelope") != envelope:
        raise ValueError("owner_envelope_missing")
    if grant.get("status") != "APPROVED" or not grant.get("owner_resolution_url"):
        raise ValueError("owner_resolution_missing")
    if "bridge" not in grant.get("modes", []):
        raise ValueError("mode_not_granted")
    if actual.get("dirty") or grant.get("identity") != dict(actual):
        raise ValueError("artifact_mismatch")
    if grant.get("profile") != dict(profile) or grant.get("session") != session:
        raise ValueError("profile_or_session_mismatch")
    for key in ("issued_at", "expires_at"):
        value = grant.get(key)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid_approval_time")
    if not grant["issued_at"] <= now < grant["expires_at"]:
        raise ValueError("approval_expired")
    if not actual.get("dll") or grant.get("save_resolved") is not True:
        raise ValueError("native_save_or_artifact_unresolved")

    limits = grant.get("limits", {})
    if not isinstance(limits, dict):
        raise ValueError("invalid_trial_limits")
    normalized: dict[str, int] = {}
    for key, maximum in _MAX_LIMITS.items():
        value = limits.get(key)
        if type(value) is not int or not 0 < value <= maximum:
            raise ValueError("invalid_trial_limit:" + key)
        normalized[key] = value

    bridge = grant.get("bridge")
    if not isinstance(bridge, dict) or bridge.get("owner_ingress") != "existing_ptt_asr":
        raise ValueError("bridge_owner_ingress_missing")
    if bridge.get("ordinary_voice_authorized") is not True:
        raise ValueError("bridge_voice_permission_missing")
    if bridge.get("gameplay_budget_scope") != "stardew_only":
        raise ValueError("bridge_budget_scope_invalid")
    if bridge.get("retain_raw_audio") is not False or bridge.get("retain_transcript") is not False:
        raise ValueError("bridge_retention_must_be_disabled")
    # A bridge grant constrains an already-running owner ingress.  It must not
    # smuggle the standalone trial's synthetic/direct owner objective into the
    # ordinary bridge as a second goal.
    if grant.get("owner_text") not in (None, ""):
        raise ValueError("bridge_owner_text_forbidden")
    return normalized


class BridgeTrialSession:
    """Own one supervised ordinary bridge runtime and its lower TrialBudget."""

    def __init__(
        self,
        *,
        config: Mapping[str, object],
        grant: Mapping[str, object],
        limits: Mapping[str, int],
        report_path: str | Path,
        budget: Any,
        clock: Callable[[], float],
    ) -> None:
        self.config = dict(config)
        self.grant = dict(grant)
        self.limits = dict(limits)
        self.report_path = Path(report_path).resolve()
        self.budget = budget
        self.clock = clock
        self._lock = threading.RLock()
        self._runtime: Any | None = None
        self._provider: Any | None = None
        self._started = False
        self._stopped = False
        self._stop_reason: str | None = None
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._ptt_captures = 0
        self._actual_mic_events = 0
        self._injected_mic_events = 0
        self._actual_epochs: set[object] = set()
        self._seen_owner_epochs: set[object] = set()

    @property
    def runtime(self) -> Any | None:
        return self._runtime

    @property
    def stop_requested(self) -> bool:
        return self.budget.stopped or bool(
            getattr(self._runtime, "trial_delivery_failed", False))

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    def _lowered_config(self) -> dict[str, object]:
        cfg = dict(self.config)
        cfg["stardew_runtime_enabled"] = True
        cfg["stardew_actions_per_session"] = _effective_lower(
            cfg.get("stardew_actions_per_session", self.limits["input"]), self.limits["input"]
        )
        cfg["stardew_provider_requests_per_session"] = _effective_lower(
            cfg.get("stardew_provider_requests_per_session", self.limits["provider"]), self.limits["provider"]
        )
        cfg["stardew_continuation_decisions"] = _effective_lower(
            cfg.get("stardew_continuation_decisions", self.limits["steps"]), self.limits["steps"]
        )
        cfg["stardew_continuation_wall_seconds"] = _effective_lower(
            cfg.get("stardew_continuation_wall_seconds", self.limits["seconds"]), self.limits["seconds"]
        )
        return cfg

    @staticmethod
    def _default_provider_factory(config: Mapping[str, object]) -> Any:
        from arti_stardew_runtime import construct_stardew_decision_provider

        provider = construct_stardew_decision_provider(config)
        if provider is None:
            raise ValueError("bridge_trial_provider_not_admitted")
        return provider

    @staticmethod
    def _default_runtime_factory(
        config: Mapping[str, object],
        *,
        provider: Any,
        trial_admission: Callable[[str], None],
        on_generation_result: Callable,
        journal_dir: str,
        fresh_trial_startup: Callable[[Path], None],
    ) -> Any:
        from arti_stardew_runtime import ProductionDecisionProvider, StardewGoalRuntime

        extra: dict[str, object] = {}
        if (
            isinstance(provider, ProductionDecisionProvider)
            and config.get("stardew_wiki_enabled") is True
        ):
            from arti_stardew_wiki_http import WikiHTTPFetcher

            extra["wiki_fetcher"] = WikiHTTPFetcher()
        runtime = StardewGoalRuntime(
            config=dict(config),
            provider=provider,
            fail_closed_owner_provider=True,
            trial_admission=trial_admission,
            on_generation_result=on_generation_result,
            journal_dir=journal_dir,
            **extra,
        )
        runtime.start(fresh_trial_startup=fresh_trial_startup)
        return runtime

    def start_runtime(
        self,
        *,
        provider_factory: Callable[[Mapping[str, object]], Any] | None = None,
        runtime_factory: Callable[..., Any] | None = None,
        start_monitor: bool = True,
    ) -> Any:
        """Reserve the journal and install all lower guards before runtime start."""
        with self._lock:
            if self._started:
                raise ValueError("bridge_trial_runtime_already_started")
            self._started = True
            # Exclusive journal reservation is the first effect.  If anything
            # later fails, the reservation remains consumed and is never replayed.
            self.budget.begin()
            cfg = self._lowered_config()
            self.config = cfg
            make_provider = provider_factory or self._default_provider_factory
            make_runtime = runtime_factory or self._default_runtime_factory
            try:
                provider = make_provider(cfg)
                if provider is None:
                    raise ValueError("bridge_trial_provider_not_admitted")
                # Provider accounting is attached before runtime construction;
                # physical requests reserve their budget slot before transport.
                provider.physical_start_guard = self.budget.admit
                runtime = make_runtime(
                    cfg,
                    provider=provider,
                    trial_admission=self.budget.admit,
                    on_generation_result=self._on_generation_result,
                    journal_dir=str(self.budget.path.parent),
                    fresh_trial_startup=self.budget.admit_runtime_startup,
                )
                if runtime is None:
                    raise ValueError("bridge_trial_runtime_not_started")
                runtime._trial_deadline_unix_ms = self.budget.deadline_unix_ms
            except Exception:
                self._stop_reason = "START_FAILED"
                self._write_report("START_FAILED")
                raise
            self._provider = provider
            self._runtime = runtime
            self._write_report("RUNNING")
            if start_monitor:
                self._monitor = threading.Thread(
                    target=self._deadline_monitor,
                    name="stardew-bridge-trial-deadline",
                    daemon=True,
                )
                self._monitor.start()
            return runtime

    def allows_owner_ingress(self, trigger_type: str) -> bool:
        with self._lock:
            return trigger_type == "ptt" and not self.stop_requested

    def request_stop(self, reason: str) -> None:
        """Latch admission now; monitor performs potentially blocking cleanup."""
        with self._lock:
            self.budget.stop()
            if self._stop_reason is None:
                self._stop_reason = reason
            runtime = self._runtime
        if runtime is not None:
            runtime.stop_admissions()

    def _on_generation_result(self, request: Any, result: Any) -> None:
        # Invoked by the ordinary runtime before its trial delivery barrier opens.
        reason = None
        if not result.applied:
            reason = result.reason or "TRIAL_REJECTED"
        elif result.action_evidence is not None:
            evidence = result.action_evidence
            if evidence.lifecycle != "completed" or not evidence.cleanup_confirmed:
                reason = "NATIVE_ACTION_FAILURE"
            elif evidence.world_effect_status == "uncertain":
                reason = "UNCERTAIN_MUTATION"
        elif result.inspect_evidence is not None and result.inspect_evidence.status != "current":
            reason = "INSPECT_UNAVAILABLE"
        if reason is not None:
            self.request_stop(reason)

    def note_ptt_capture(self) -> None:
        with self._lock:
            self._ptt_captures += 1

    def note_mic_event(self, *, actual: bool, epoch: object | None = None) -> None:
        """Record provenance only; never retain waveform or transcript text."""
        with self._lock:
            if actual:
                self._actual_mic_events += 1
                if epoch is not None:
                    self._actual_epochs.add(epoch)
            else:
                self._injected_mic_events += 1

    def note_owner_turn(self, epoch: object | None) -> None:
        """Classify direct/injected PTT turns without storing their text."""
        key = epoch if epoch is not None else ("none", self._ptt_captures, len(self._seen_owner_epochs))
        with self._lock:
            if key in self._seen_owner_epochs:
                return
            self._seen_owner_epochs.add(key)
            if epoch not in self._actual_epochs:
                self._injected_mic_events += 1

    def poll_deadline_once(self) -> bool:
        if self.stop_requested:
            self.stop(self._stop_reason or "trial_delivery_error")
            return False
        try:
            self.budget.check_time()
            return True
        except ValueError as exc:
            if str(exc) != "trial_deadline":
                raise
            self.stop("TRIAL_DEADLINE")
            return False

    def _deadline_monitor(self) -> None:
        while not self._monitor_stop.wait(0.10):
            if not self.poll_deadline_once():
                return

    def stop(self, reason: str = "bridge_shutdown") -> None:
        """Stop only the same Stardew runtime; never pause/stop the host bridge."""
        runtime = None
        self.request_stop(reason)
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._monitor_stop.set()
            runtime = self._runtime
        if runtime is not None:
            runtime.stop()
        self._write_report(self._stop_reason or reason)

    def _write_report(self, status: str) -> None:
        provider = self._provider
        runtime = self._runtime
        actual = self._actual_mic_events
        injected = self._injected_mic_events
        if actual:
            mic = "actual_ptt_asr"
        elif injected:
            mic = "injected_ptt"
        else:
            mic = "not_observed"
        payload = {
            "schema": "BRIDGE_TRIAL_REPORT_V1",
            "status": status,
            "live_verified": False,
            "host_paused": False,
            "reserved": dict(self.budget.used),
            "decisions_reserved": self.budget.decisions,
            "observed": {
                "provider_physical_requests": getattr(provider, "physical_requests", 0),
                "physical_action_publications": getattr(runtime, "physical_action_publications", 0),
            },
            "limits": dict(self.limits),
            "provenance": {
                "mic": mic,
                "actual_mic_events": actual,
                "injected_mic_events": injected,
                "ptt_captures": self._ptt_captures,
                "gameplay_budget_scope": "stardew_only",
                "gameplay_budget_covers_voice": False,
                "ordinary_voice_authorized": True,
                "raw_audio_retained": False,
                "transcript_retained": False,
            },
        }
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.report_path.with_suffix(self.report_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf8")
        os.replace(tmp, self.report_path)


def prepare_bridge_trial(
    *,
    environ: Mapping[str, str] | None = None,
    identity_fn: Callable[..., Mapping[str, object]] | None = None,
    session_resolver: Callable[[Mapping[str, object]], str | None] | None = None,
    clock: Callable[[], float] = time.time,
) -> BridgeTrialSession | None:
    """Fail-closed preflight.  With no trial env this function is fully inert."""
    env = os.environ if environ is None else environ
    present = [bool(env.get(key)) for key in _REQUIRED_ENV]
    if not any(present):
        return None
    if not all(present):
        raise ValueError("bridge_trial_env_incomplete")

    approval_path = Path(env[_APPROVAL_ENV]).resolve()
    config_path = Path(env[_CONFIG_ENV]).resolve()
    artifact_path = Path(env[_ARTIFACT_ENV]).resolve()
    report_path = Path(env[_REPORT_ENV]).resolve()

    grant = json.loads(approval_path.read_text(encoding="utf8"))
    config_bytes = config_path.read_bytes()
    if hashlib.sha256(config_bytes).hexdigest() != grant.get("config_sha256"):
        raise ValueError("config_mismatch")
    config = json.loads(config_bytes)
    if not isinstance(config, dict) or config.get("stardew_runtime_enabled") is not True:
        raise ValueError("bridge_trial_runtime_disabled")

    # Lazy imports keep ordinary no-trial bridge startup unchanged.
    from scripts.stardew_m1_trial import ENVELOPE, TrialBudget, identity

    actual_identity = (identity_fn or identity)(artifact=artifact_path)
    profile = {key: config.get(key) for key in _PROFILE_KEYS}
    if session_resolver is None:
        from arti_stardew_command import baca_sesi

        def session_resolver(cfg: Mapping[str, object]) -> str | None:
            folder = cfg.get("stardew_command_dir")
            return baca_sesi(folder).session_id if folder else None

    session = session_resolver(config)
    limits = _validate_bridge_grant(
        grant,
        actual=actual_identity,
        profile=profile,
        session=session,
        now=clock(),
        envelope=ENVELOPE,
    )
    journal_path = grant.get("journal_path")
    if not isinstance(journal_path, str) or not journal_path:
        raise ValueError("journal_identity_missing")
    budget = TrialBudget(journal_path, grant, clock=clock)
    return BridgeTrialSession(
        config=config,
        grant=grant,
        limits=limits,
        report_path=report_path,
        budget=budget,
        clock=clock,
    )
