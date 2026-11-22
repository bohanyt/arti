"""Bounded per-episode freshness fences for observed Stardew traversal evidence.

Pure evidence only: no movement, collision/occupancy oracle, state store, target
selection, or NAV capability. Time limits are caller policy in the receiver's
monotonic clock; they are not Stardew tick/movement-duration calibrations.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from arti_stardew_navigation_observed import (
    CONFIRMED_LOCAL_STEP,
    LOCATION_CHANGED,
    PoseReadback,
    TraversalObservation,
    observe_cardinal_traversal,
)
from arti_stardew_route import HasilRute, UNREACHABLE, cari_rute_kardinal

Tile = tuple[int, int]
Edge = tuple[Tile, Tile]

ACTIVE = "ACTIVE"
CLOSED = "CLOSED"
SUPERSEDED = "SUPERSEDED"
EXPIRED = "EXPIRED"

EDGE_ADMITTED = "EDGE_ADMITTED"
CLASSIFIER_NO_EDGE = "CLASSIFIER_NO_EDGE"
LOCATION_FENCED = "LOCATION_FENCED"
EPOCH_FENCED = "EPOCH_FENCED"
BINDING_FENCED = "BINDING_FENCED"
DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
STALE_SAMPLE = "STALE_SAMPLE"
GAP_EXCEEDED = "GAP_EXCEEDED"
OUT_OF_ORDER_SAMPLE = "OUT_OF_ORDER_SAMPLE"
NOT_FIRST_AFTER_RELEASE = "NOT_FIRST_AFTER_RELEASE"
CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
EPISODE_NOT_ACTIVE = "EPISODE_NOT_ACTIVE"
INVALID_INPUT = "INVALID_INPUT"

DEFAULT_MAX_ATTEMPTS = 16
DEFAULT_MAX_EDGES = 16


@dataclass(frozen=True)
class EpisodePolicy:
    # Required time values force the caller to choose evidence/transport bounds.
    max_before_after_gap: float
    max_after_age: float
    max_episode_age: float
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_edges: int = DEFAULT_MAX_EDGES
    # fix54I: the admitted->released interval is lease-owned and release-proven input time. A caller may let that
    # measured interval (at most this much) extend the before->after gap; 0 keeps the fixed bound.
    max_command_extension: float = 0.0

    def __post_init__(self) -> None:
        for name in ("max_before_after_gap", "max_after_age", "max_episode_age", "max_command_extension"):
            if not _finite_nonnegative(getattr(self, name)):
                raise ValueError(f"{name} harus finite >= 0")
        for name in ("max_attempts", "max_edges"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} harus integer polos > 0")


@dataclass(frozen=True)
class EpisodeBinding:
    episode_id: str
    receiver_epoch_id: str
    location_key: str
    player_id: int
    screen_id: int
    started_at: float

    def __post_init__(self) -> None:
        _require_text(self.episode_id, "episode_id")
        _require_text(self.receiver_epoch_id, "receiver_epoch_id")
        _require_text(self.location_key, "location_key")
        if type(self.player_id) is not int or type(self.screen_id) is not int:
            raise ValueError("player_id/screen_id harus integer polos")
        if not _finite_nonnegative(self.started_at):
            raise ValueError("started_at harus finite >= 0")


@dataclass(frozen=True)
class EpisodeSample:
    pose: PoseReadback
    receiver_epoch_id: str
    location_key: str
    accepted_seq: int

    def __post_init__(self) -> None:
        if not isinstance(self.pose, PoseReadback):
            raise ValueError("pose harus PoseReadback canonical")
        _require_text(self.receiver_epoch_id, "receiver_epoch_id")
        _require_text(self.location_key, "location_key")
        if type(self.accepted_seq) is not int or self.accepted_seq < 0:
            raise ValueError("accepted_seq harus integer polos >= 0")


@dataclass(frozen=True)
class SampleBoundary:
    """Latest accepted pose sequence visible at admission or release."""

    receiver_epoch_id: str
    location_key: str
    last_accepted_sample_seq: int
    at: float

    def __post_init__(self) -> None:
        _require_text(self.receiver_epoch_id, "receiver_epoch_id")
        _require_text(self.location_key, "location_key")
        if (
            type(self.last_accepted_sample_seq) is not int
            or self.last_accepted_sample_seq < 0
        ):
            raise ValueError("last_accepted_sample_seq harus integer polos >= 0")
        if not _finite_nonnegative(self.at):
            raise ValueError("boundary.at harus finite >= 0")


@dataclass(frozen=True)
class EpisodeLocationTransition:
    before_location_key: str
    after_location_key: str
    before: PoseReadback
    after: PoseReadback


@dataclass(frozen=True)
class EpisodeAdmission:
    status: str
    reason: str
    observation: TraversalObservation | None = None
    edge: Edge | None = None
    new_edge: bool = False
    location_transition: EpisodeLocationTransition | None = None


@dataclass(frozen=True)
class EpisodeRouteResult:
    route: HasilRute | None
    evidence_limited: bool
    reason: str

    @property
    def status(self) -> str:
        return self.route.status if self.route is not None else EPISODE_NOT_ACTIVE


class NavigationEpisode:
    """One bounded, ephemeral directed-edge evidence container."""

    def __init__(self, binding: EpisodeBinding, policy: EpisodePolicy) -> None:
        if not isinstance(binding, EpisodeBinding) or not isinstance(policy, EpisodePolicy):
            raise ValueError("binding/policy type tidak valid")
        self.binding = binding
        self.policy = policy
        self._state = ACTIVE
        self._current_monotonic = binding.started_at
        self._attempt_ids: set[str] = set()
        self._observation_keys: set[tuple[object, ...]] = set()
        self._edges: set[Edge] = set()
        self._nodes: set[Tile] = set()
        self._close_reason: str | None = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_monotonic(self) -> float:
        return self._current_monotonic

    @property
    def attempt_count(self) -> int:
        return len(self._attempt_ids)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    @property
    def edges(self) -> frozenset[Edge]:
        return frozenset(self._edges)

    @property
    def nodes(self) -> frozenset[Tile]:
        return frozenset(self._nodes)

    @property
    def close_reason(self) -> str | None:
        return self._close_reason

    def close(self, reason: str = "closed by caller") -> None:
        if self._state == ACTIVE:
            self._state, self._close_reason = CLOSED, reason

    def supersede(self, reason: str = "superseded by newer episode") -> None:
        if self._state == ACTIVE:
            self._state, self._close_reason = SUPERSEDED, reason

    def admit_observation(
        self,
        *,
        attempt_id: str,
        direction: str,
        before: EpisodeSample,
        admitted: SampleBoundary,
        released: SampleBoundary,
        after: EpisodeSample,
        current_monotonic: float,
    ) -> EpisodeAdmission:
        """Maybe admit one edge from an already-observed movement attempt."""

        error = self._advance_current(current_monotonic)
        if error is not None:
            return error
        if self._state != ACTIVE:
            return _result(EPISODE_NOT_ACTIVE, f"episode {self._state.lower()}")

        if not isinstance(attempt_id, str) or not attempt_id:
            return _result(INVALID_INPUT, "attempt_id harus string non-kosong")
        if not isinstance(before, EpisodeSample) or not isinstance(after, EpisodeSample):
            return _result(INVALID_INPUT, "before/after harus EpisodeSample")
        if not isinstance(admitted, SampleBoundary) or not isinstance(released, SampleBoundary):
            return _result(INVALID_INPUT, "admitted/released harus SampleBoundary")

        key = (
            direction,
            before.receiver_epoch_id,
            before.location_key,
            before.accepted_seq,
            before.pose.received_at,
            before.pose.tile,
            released.last_accepted_sample_seq,
            released.at,
            after.receiver_epoch_id,
            after.location_key,
            after.accepted_seq,
            after.pose.received_at,
            after.pose.tile,
        )
        if attempt_id in self._attempt_ids or key in self._observation_keys:
            return _result(DUPLICATE_OBSERVATION, "attempt/witness sudah diproses")
        if len(self._attempt_ids) >= self.policy.max_attempts:
            return _result(CAPACITY_EXHAUSTED, "attempt cap penuh; tanpa eviction")

        # A distinct submission consumes one bounded attempt slot even if rejected.
        self._attempt_ids.add(attempt_id)
        self._observation_keys.add(key)

        epochs = {
            before.receiver_epoch_id,
            admitted.receiver_epoch_id,
            released.receiver_epoch_id,
            after.receiver_epoch_id,
        }
        if epochs != {self.binding.receiver_epoch_id}:
            self._fence(SUPERSEDED, "receiver epoch changed/mismatched")
            return _result(EPOCH_FENCED, "receiver epoch mismatch; zero new edge")

        if any(
            (
                before.pose.player_id != self.binding.player_id,
                before.pose.screen_id != self.binding.screen_id,
                after.pose.player_id != self.binding.player_id,
                after.pose.screen_id != self.binding.screen_id,
            )
        ):
            self._fence(SUPERSEDED, "player/screen binding changed")
            return _result(BINDING_FENCED, "player/screen mismatch; zero new edge")

        if (
            before.location_key != self.binding.location_key
            or admitted.location_key != self.binding.location_key
            or released.location_key != self.binding.location_key
        ):
            self._fence(CLOSED, "source location key mismatch")
            return _result(LOCATION_FENCED, "source location key mismatch; zero new edge")

        window_error = self._validate_window(before, admitted, released, after)
        if window_error is not None:
            return window_error

        location_changed = (
            after.location_key != self.binding.location_key
            or after.pose.location != before.pose.location
        )
        if location_changed:
            observation = observe_cardinal_traversal(before.pose, after.pose, direction)
            transition = EpisodeLocationTransition(
                before.location_key,
                after.location_key,
                before.pose,
                after.pose,
            )
            self._fence(CLOSED, "observed location transition")
            return EpisodeAdmission(
                status=LOCATION_FENCED,
                reason=(
                    "observed location transition; no local edge or causal door/warp claim"
                ),
                observation=observation,
                location_transition=transition,
            )

        gap = after.pose.received_at - before.pose.received_at
        held = min(self.policy.max_command_extension, max(0.0, released.at - admitted.at))
        if gap > self.policy.max_before_after_gap + held:
            return _result(GAP_EXCEEDED, "before->after gap exceeds caller evidence bound")

        after_age = self._current_monotonic - after.pose.received_at
        if after_age < 0:
            return _result(OUT_OF_ORDER_SAMPLE, "current_monotonic precedes after")
        if after_age > self.policy.max_after_age:
            return _result(STALE_SAMPLE, "after sample is stale at caller current time")

        observation = observe_cardinal_traversal(before.pose, after.pose, direction)
        if observation.status != CONFIRMED_LOCAL_STEP or observation.edge is None:
            return EpisodeAdmission(
                CLASSIFIER_NO_EDGE,
                f"canonical classifier={observation.status}; zero new edge",
                observation=observation,
            )

        edge = observation.edge
        if edge in self._edges:
            return EpisodeAdmission(
                DUPLICATE_OBSERVATION,
                "directed edge already witnessed; no new evidence",
                observation=observation,
                edge=edge,
            )
        if len(self._edges) >= self.policy.max_edges:
            return EpisodeAdmission(
                CAPACITY_EXHAUSTED,
                "edge cap penuh; fail closed tanpa eviction",
                observation=observation,
            )

        self._edges.add(edge)
        self._nodes.update(edge)
        return EpisodeAdmission(
            EDGE_ADMITTED,
            "fresh same-epoch confirmed local step admitted",
            observation=observation,
            edge=edge,
            new_edge=True,
        )

    def query_route(
        self,
        *,
        start: object,
        goals: object,
        max_path_length: object,
        current_monotonic: float,
    ) -> EpisodeRouteResult:
        """Call #129 only with this active episode's witnessed nodes/edges."""

        error = self._advance_current(current_monotonic)
        if error is not None:
            return EpisodeRouteResult(None, True, error.reason)
        if self._state != ACTIVE:
            return EpisodeRouteResult(
                None, True, f"episode {self._state.lower()}; route unavailable"
            )

        route = cari_rute_kardinal(
            self._nodes, self._edges, start, goals, max_path_length
        )
        reason = (
            "UNREACHABLE means unavailable from current witnessed evidence; "
            "not collision truth or physical impossibility"
            if route.status == UNREACHABLE
            else "route is bounded to current episode witnessed nodes/edges"
        )
        return EpisodeRouteResult(route, True, reason)

    def _validate_window(
        self,
        before: EpisodeSample,
        admitted: SampleBoundary,
        released: SampleBoundary,
        after: EpisodeSample,
    ) -> EpisodeAdmission | None:
        if before.accepted_seq != admitted.last_accepted_sample_seq:
            return _result(
                OUT_OF_ORDER_SAMPLE,
                "before is not latest accepted sample at admission",
            )
        if released.last_accepted_sample_seq < admitted.last_accepted_sample_seq:
            return _result(OUT_OF_ORDER_SAMPLE, "release sequence moved backward")
        if after.accepted_seq != released.last_accepted_sample_seq + 1:
            return _result(
                NOT_FIRST_AFTER_RELEASE,
                "after is not first accepted sample after release",
            )
        if before.pose.received_at < self.binding.started_at:
            return _result(OUT_OF_ORDER_SAMPLE, "before precedes episode start")
        # The release->after order is proven by the sequence check above (first accepted sample
        # after release); time only has to be non-decreasing. Strict < rejected a frame received in
        # the release's own 15.6 ms Windows clock tick (live 2026-10-07 M1 attempt 10q).
        if not (
            before.pose.received_at
            <= admitted.at
            <= released.at
            <= after.pose.received_at
        ):
            return _result(
                OUT_OF_ORDER_SAMPLE,
                "before/admission/release/after times are not monotonic",
            )
        return None

    def _advance_current(self, current_monotonic: object) -> EpisodeAdmission | None:
        if not _finite_nonnegative(current_monotonic):
            return _result(INVALID_INPUT, "current_monotonic harus finite >= 0")
        now = float(current_monotonic)
        if now < self._current_monotonic:
            return _result(OUT_OF_ORDER_SAMPLE, "current_monotonic moved backward")
        self._current_monotonic = now
        if (
            self._state == ACTIVE
            and now - self.binding.started_at > self.policy.max_episode_age
        ):
            self._fence(EXPIRED, "max_episode_age exceeded")
        return None

    def _fence(self, state: str, reason: str) -> None:
        if self._state == ACTIVE:
            self._state, self._close_reason = state, reason


def _result(status: str, reason: str) -> EpisodeAdmission:
    return EpisodeAdmission(status=status, reason=reason)


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} harus string non-kosong")


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value >= 0
    )
