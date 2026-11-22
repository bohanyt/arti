"""Bounded local Stardew target approach from observed feedback.

This module is a pure state machine. It proposes at most one short cardinal
movement at a time and consumes canonical post-release pose evidence before it
can propose again. It does not own native input, permission, a receiver, or a
world-state store.

Candidate geometry is only a heuristic. An unobserved proposal is a probe, not
a safe edge. Directed traversal evidence is admitted only by the existing
NavigationEpisode classifier; reverse edges, occupancy facts, command lifecycle
completion, and location transitions are never converted into arrival proof.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import math

from arti_stardew_navigation_episode import (
    BINDING_FENCED,
    CAPACITY_EXHAUSTED,
    CLASSIFIER_NO_EDGE,
    DUPLICATE_OBSERVATION,
    EDGE_ADMITTED,
    EPISODE_NOT_ACTIVE,
    EPOCH_FENCED,
    GAP_EXCEEDED,
    INVALID_INPUT as EPISODE_INVALID_INPUT,
    LOCATION_FENCED,
    NOT_FIRST_AFTER_RELEASE,
    OUT_OF_ORDER_SAMPLE,
    STALE_SAMPLE,
    EpisodeBinding,
    EpisodePolicy,
    EpisodeSample,
    NavigationEpisode,
    SampleBoundary,
)
from arti_stardew_navigation_observed import (
    AMBIGUOUS_DISPLACEMENT,
    CONFIRMED_LOCAL_STEP,
    IDENTITY_CHANGED,
    INVALID_INPUT as OBSERVATION_INVALID_INPUT,
    LOCATION_CHANGED,
    NO_TILE_PROGRESS,
    STALE_READBACK,
    SUBTILE_PROGRESS,
    PoseReadback,
)

Tile = tuple[int, int]
Pixel = tuple[float, float]

PROPOSED = "PROPOSED"
AWAITING_FEEDBACK = "AWAITING_FEEDBACK"
PROGRESS = "PROGRESS"
NO_PROGRESS = "NO_PROGRESS"
REPLAN_REQUIRED = "REPLAN_REQUIRED"
ARRIVED = "ARRIVED"
UNAVAILABLE = "UNAVAILABLE"
BLOCKED = "BLOCKED"
PARTIAL = "PARTIAL"
UNCERTAIN = "UNCERTAIN"
CANCELLED = "CANCELLED"

DIRECT_TARGET = "DIRECT_TARGET"
ADJACENT_INTERACTION = "ADJACENT_INTERACTION"
CANDIDATE_PROBE = "CANDIDATE_PROBE"
CLEANUP_CONFIRMED = "CLEANUP_CONFIRMED"
CLEANUP_UNCERTAIN = "CLEANUP_UNCERTAIN"

DEFAULT_MAX_PROPOSALS = 32
MAX_PROPOSALS = 40  # FIXB34: explicit parent opt-in for a long shipping-bin approach; default stays 32.
DEFAULT_REPEATED_FAILURE_THRESHOLD = 2
DEFAULT_MAX_REPLANS = 1
DEFAULT_RECENT_TILE_WINDOW = 8
MOVEMENT_TICKS_MIN = 1
MOVEMENT_TICKS_MAX = 60
MAX_REACH_TILES = 16
# fix89D / ADR 158: with the mod's guarded segment hold (MoveSegmentGuard) one straight observed-open run is one
# hold. The mod stops it at the stop tile's centre, on no progress or on leaving the line; ticks are only a cap.
SEGMENT_GUARD_TILES_MAX = 10
SEGMENT_GUARD_TICKS_MAX = 120
SEGMENT_GUARD_SLACK_TICKS = 12  # about one tile past the estimate; the native stop, not the count, ends the hold
# fix92A / ADR 162: with the mod's guarded PATH (input_once.move_path) one observed-open routed path with turns is one
# request: the mod turns at each reached stop (one released frame) and stops at the last. Same caps as the mod.
PATH_GUARD_TILES_MAX = 30
PATH_GUARD_SEGMENTS_MAX = 8
PATH_GUARD_TICKS_MAX = 180
PATH_GUARD_TURN_PX = 12  # 8 px corridor + half a frame step at the turn pose (feedback tolerance off the first segment)
ROUTING_RADIUS = 12
ROUTING_MAX_NODES = 600
ROUTING_WINDOW_MAX_NODES = 2304  # FIXB15 D-B13-1: a published nav-grid window (<= 48 x 48) is searched whole
ROUTING_MAX_FACT_TILES = 4096

_DIRECTIONS = ("right", "down", "left", "up")
_DELTA: dict[str, Tile] = {
    "right": (1, 0),
    "down": (0, 1),
    "left": (-1, 0),
    "up": (0, -1),
}
_PERPENDICULAR = {
    "right": ("up", "down"),
    "left": ("up", "down"),
    "up": ("left", "right"),
    "down": ("left", "right"),
}
_TERMINAL = {ARRIVED, UNAVAILABLE, BLOCKED, PARTIAL, UNCERTAIN, CANCELLED}


@dataclass(frozen=True)
class ApproachBinding:
    """Trusted parent binding; session/area authority stays outside this module."""

    session_id: str
    area_id: str
    episode: EpisodeBinding

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        _require_text(self.area_id, "area_id")
        if not isinstance(self.episode, EpisodeBinding):
            raise ValueError("episode harus EpisodeBinding canonical")


@dataclass(frozen=True)
class ApproachPolicy:
    """Bounded controller policy; fixed caller-selected ticks unless adaptive movement is explicitly enabled."""

    move_ticks: int
    max_proposals: int = DEFAULT_MAX_PROPOSALS
    repeated_failure_threshold: int = DEFAULT_REPEATED_FAILURE_THRESHOLD
    max_replans: int = DEFAULT_MAX_REPLANS
    recent_tile_window: int = DEFAULT_RECENT_TILE_WINDOW
    adaptive_move_ticks: bool = False  # FIXB36: explicit ship-only opt-in, measured one-tile presses
    max_segment_tiles: int = 1  # P13-only opt-in; observed open straight paths, never inferred edges
    segment_guard: bool = False  # fix89D: the mod advertises the guarded segment hold (ADR 158)
    path_guard: bool = False  # fix92A: the mod advertises the guarded path (ADR 162); needs segment_guard

    def __post_init__(self) -> None:
        if type(self.adaptive_move_ticks) is not bool:
            raise ValueError("adaptive_move_ticks harus boolean")
        if type(self.path_guard) is not bool or (self.path_guard and not self.segment_guard):
            raise ValueError("path_guard harus boolean dan hanya bersama segment_guard")
        if type(self.segment_guard) is not bool:
            raise ValueError("segment_guard harus boolean")
        limit = SEGMENT_GUARD_TILES_MAX if self.segment_guard else 4
        if type(self.max_segment_tiles) is not int or not 1 <= self.max_segment_tiles <= limit:
            raise ValueError(f"max_segment_tiles harus integer polos 1..{limit}")
        if (
            not _plain_int(self.move_ticks)
            or not MOVEMENT_TICKS_MIN <= self.move_ticks <= MOVEMENT_TICKS_MAX
        ):
            raise ValueError("move_ticks harus integer polos 1..60")
        if (
            not _plain_int(self.max_proposals)
            or not 1 <= self.max_proposals <= MAX_PROPOSALS
        ):
            raise ValueError("max_proposals harus integer polos 1..40")
        if (
            not _plain_int(self.repeated_failure_threshold)
            or self.repeated_failure_threshold < 1
        ):
            raise ValueError("repeated_failure_threshold harus integer polos >= 1")
        if not _plain_int(self.max_replans) or not 0 <= self.max_replans <= 1:
            raise ValueError("max_replans harus 0 atau 1")
        if (
            not _plain_int(self.recent_tile_window)
            or not 4 <= self.recent_tile_window <= 16
        ):
            raise ValueError("recent_tile_window harus integer polos 4..16")


@dataclass(frozen=True)
class ApproachTarget:
    """One observed target pin and an explicit caller-supplied reach condition."""

    target_ref: str
    target_revision: str
    session_id: str
    receiver_epoch_id: str
    location_key: str
    area_id: str
    observed_at: float
    valid_until: float
    target_tile: Tile
    reach_kind: str
    reach_tiles: tuple[Tile, ...]

    def __post_init__(self) -> None:
        for name in (
            "target_ref",
            "target_revision",
            "session_id",
            "receiver_epoch_id",
            "location_key",
            "area_id",
        ):
            _require_text(getattr(self, name), name)
        if not _finite_nonnegative(self.observed_at) or not _finite_nonnegative(
            self.valid_until
        ):
            raise ValueError("target observed_at/valid_until harus finite >= 0")
        if self.valid_until < self.observed_at:
            raise ValueError("target valid_until tidak boleh sebelum observed_at")
        _require_tile(self.target_tile, "target_tile")
        if self.reach_kind not in (DIRECT_TARGET, ADJACENT_INTERACTION):
            raise ValueError("reach_kind tidak dikenal")
        if not isinstance(self.reach_tiles, tuple) or not self.reach_tiles:
            raise ValueError("reach_tiles harus tuple non-kosong")
        if len(self.reach_tiles) > MAX_REACH_TILES:
            raise ValueError(f"reach_tiles melebihi {MAX_REACH_TILES}")
        if len(set(self.reach_tiles)) != len(self.reach_tiles):
            raise ValueError("reach_tiles tidak boleh duplikat")
        for tile in self.reach_tiles:
            _require_tile(tile, "reach_tile")
        if self.reach_kind == DIRECT_TARGET:
            if self.reach_tiles != (self.target_tile,):
                raise ValueError(
                    "DIRECT_TARGET harus memakai target_tile sebagai satu-satunya reach tile"
                )
        else:
            if self.target_tile in self.reach_tiles:
                raise ValueError(
                    "ADJACENT_INTERACTION tidak boleh menganggap occupied target tile sebagai arrival"
                )
            if any(
                _manhattan(tile, self.target_tile) != 1 for tile in self.reach_tiles
            ):
                raise ValueError(
                    "ADJACENT_INTERACTION reach_tiles harus kardinal-adjacent ke target_tile"
                )

    @property
    def pin(self) -> tuple[object, ...]:
        return (
            self.target_ref,
            self.target_revision,
            self.session_id,
            self.receiver_epoch_id,
            self.location_key,
            self.area_id,
            self.observed_at,
            self.valid_until,
            self.target_tile,
            self.reach_kind,
            tuple(sorted(self.reach_tiles)),
        )


@dataclass(frozen=True)
class ApproachRouting:
    """Caller-observed tile passability for one step: a planning hint, never arrival/edge proof.

    ``blocked_tiles`` are tiles whose current scene facts say not passable; ``passable_tiles``
    are tiles with a passable fact and no blocking fact. Every other tile is unknown and is
    routed through optimistically; physical feedback still decides every step.
    """

    blocked_tiles: frozenset[Tile] = frozenset()
    passable_tiles: frozenset[Tile] = frozenset()
    # FIXB15 D-B13-1 (live V2B13ACC11a: a slime 25 tiles away needed 1445 BFS nodes; the 600-node / radius-12 search
    # gave up and the greedy fallback pressed into walls): the caller's complete walkability window (x0, y0, x1, y1,
    # inclusive; the nav grid) widens the search to that window with a node cap of its area.
    window: tuple[int, int, int, int] | None = None
    # Live7 navigation only: fresh native-open cells may recheck a transient no-motion hint.
    # Per-edge failure counts, the single replan, deadline and proposal budget still apply.
    recheck_tiles: frozenset[Tile] = frozenset()

    def __post_init__(self) -> None:
        for name in ("blocked_tiles", "passable_tiles", "recheck_tiles"):
            tiles = getattr(self, name)
            if not isinstance(tiles, frozenset):
                raise ValueError(f"{name} harus frozenset")
            if len(tiles) > ROUTING_MAX_FACT_TILES:
                raise ValueError(f"{name} melebihi {ROUTING_MAX_FACT_TILES}")
            for tile in tiles:
                _require_tile(tile, name)
        if self.blocked_tiles & self.passable_tiles:
            raise ValueError("tile tidak boleh blocked sekaligus passable")
        if not self.recheck_tiles <= self.passable_tiles:
            raise ValueError("recheck tiles must be currently passable")
        if self.window is not None:
            w = self.window
            if (not isinstance(w, tuple) or len(w) != 4 or any(type(v) is not int or v < 0 for v in w)
                    or w[2] < w[0] or w[3] < w[1] or (w[2] - w[0] + 1) * (w[3] - w[1] + 1) > ROUTING_WINDOW_MAX_NODES):
                raise ValueError("window tidak valid")


@dataclass(frozen=True)
class MovementProposal:
    attempt_id: str
    direction: str
    ticks: int
    evidence_kind: str
    historically_witnessed: bool
    before_accepted_seq: int
    before_received_at: float
    before_tile: Tile
    target_ref: str
    target_revision: str
    replan_index: int
    segment_tiles: tuple[Tile, ...] = ()
    segment_stop: Tile | None = None  # fix89D: last segment tile for the mod's guarded hold (ADR 158)
    # fix92A / ADR 162: the stops after the first segment (each a 90-degree turn) and every tile of the path in order.
    path_next: tuple[Tile, ...] = ()
    path_tiles: tuple[Tile, ...] = ()


@dataclass(frozen=True)
class ApproachEvidence:
    attempt_id: str
    direction: str
    admission_status: str
    observation_status: str | None
    before_tile: Tile
    after_tile: Tile | None
    before_pixel: Pixel | None
    after_pixel: Pixel | None
    directed_edge: tuple[Tile, Tile] | None
    reason: str


@dataclass(frozen=True)
class ApproachDecision:
    status: str
    reason: str
    terminal: bool
    blocker: str | None
    proposal: MovementProposal | None
    target_ref: str | None
    current_tile: Tile | None
    distance_to_reach: int | None
    proposals_used: int
    replans_used: int
    progress_events: int
    evidence: tuple[ApproachEvidence, ...]


@dataclass(frozen=True)
class _PendingAttempt:
    proposal: MovementProposal
    before: EpisodeSample
    detour: bool


class LocalApproachController:
    """One bounded local approach episode with no side effects of its own."""

    def __init__(
        self,
        *,
        binding: ApproachBinding,
        evidence_policy: EpisodePolicy,
        policy: ApproachPolicy,
        initial_sample: EpisodeSample | None,
        target: ApproachTarget | None,
        deadline_at: float,
        parent_proposal_budget: int,
        initial_pixels_per_tick: float | None = None,
    ) -> None:
        if not isinstance(binding, ApproachBinding):
            raise ValueError("binding harus ApproachBinding")
        if not isinstance(evidence_policy, EpisodePolicy):
            raise ValueError("evidence_policy harus EpisodePolicy canonical")
        if not isinstance(policy, ApproachPolicy):
            raise ValueError("policy harus ApproachPolicy")
        if not _finite_nonnegative(deadline_at):
            raise ValueError("deadline_at harus finite >= 0")
        if not _plain_int(parent_proposal_budget) or parent_proposal_budget < 0:
            raise ValueError("parent_proposal_budget harus integer polos >= 0")

        self.binding = binding
        self.evidence_policy = evidence_policy
        self.policy = policy
        self.deadline_at = float(deadline_at)
        self._proposal_budget_cap = min(
            policy.max_proposals, parent_proposal_budget
        )
        self._episode = NavigationEpisode(binding.episode, evidence_policy)
        self._target_pin = target.pin if isinstance(target, ApproachTarget) else None
        self._target_ref = target.target_ref if isinstance(target, ApproachTarget) else None
        self._last_sample: EpisodeSample | None = None
        self._startup_blocker: tuple[str, str] | None = None
        if initial_sample is None:
            self._startup_blocker = ("MISSING_POSE", "initial pose tidak tersedia")
        elif not isinstance(initial_sample, EpisodeSample):
            self._startup_blocker = (
                "INVALID_POSE",
                "initial pose bukan EpisodeSample",
            )
        else:
            shape_problem = _sample_shape_problem(initial_sample)
            ep = binding.episode
            if shape_problem is not None:
                self._startup_blocker = (
                    "INVALID_INITIAL_POSE",
                    shape_problem,
                )
            elif (
                initial_sample.receiver_epoch_id != ep.receiver_epoch_id
                or initial_sample.location_key != ep.location_key
                or initial_sample.pose.player_id != ep.player_id
                or initial_sample.pose.screen_id != ep.screen_id
            ):
                self._startup_blocker = (
                    "INITIAL_BINDING_MISMATCH",
                    "initial pose tidak cocok dengan episode receiver/location/player/screen",
                )
            elif initial_sample.pose.received_at < ep.started_at:
                self._startup_blocker = (
                    "INITIAL_POSE_BEFORE_EPISODE",
                    "initial pose mendahului episode start",
                )
            else:
                self._last_sample = initial_sample
        if target is None:
            self._startup_blocker = (
                "MISSING_TARGET",
                "target teramati tidak tersedia",
            )
        elif not isinstance(target, ApproachTarget):
            self._startup_blocker = (
                "INVALID_TARGET",
                "target bukan ApproachTarget",
            )

        self._current_monotonic = binding.episode.started_at
        self._pending: _PendingAttempt | None = None
        self._terminal: ApproachDecision | None = None
        self._proposals_used = 0
        self._replans_used = 0
        self._progress_events = 0
        self._evidence: list[ApproachEvidence] = []
        self._failure_counts: Counter[tuple[object, ...]] = Counter()
        self._visited_counts: Counter[Tile] = Counter()
        self._recent_tiles: list[Tile] = []
        self._detour_pending = False
        self._detour_blocked_direction: str | None = None
        self._continue_direction: str | None = None
        # Routing-only avoidance hint: tiles a press toward produced no displacement (Stardew
        # obstacles occupy whole tiles, so the tile is avoided from every side). Never a certified
        # blocked edge, never added to the canonical witnessed edges, unused without routing.
        self._no_progress_tiles: set[Tile] = set()
        self._routing: ApproachRouting | None = None
        if initial_pixels_per_tick is not None and (
                not _finite_nonnegative(initial_pixels_per_tick) or initial_pixels_per_tick == 0):
            raise ValueError("initial_pixels_per_tick harus positive finite")
        self._pixels_per_tick = initial_pixels_per_tick
        if self._last_sample is not None:
            self._visited_counts[self._last_sample.pose.tile] += 1
            self._recent_tiles.append(self._last_sample.pose.tile)

    @property
    def witnessed_edges(self) -> frozenset[tuple[Tile, Tile]]:
        return self._episode.edges

    @property
    def pixels_per_tick(self) -> float | None:
        return self._pixels_per_tick

    @property
    def terminal_decision(self) -> ApproachDecision | None:
        return self._terminal

    def next_step(
        self,
        *,
        current_sample: EpisodeSample | None,
        current_target: ApproachTarget | None,
        current_monotonic: float,
        current_session_id: str,
        current_area_id: str,
        permission_granted: bool,
        remaining_parent_budget: int,
        cancelled: bool = False,
        routing: ApproachRouting | None = None,
    ) -> ApproachDecision:
        """Return one movement proposal or one explicit wait/stop/terminal reason.

        ``routing`` (optional, supplied fresh on every step) enables bounded shortest-path
        routing over the caller's current passability facts; without it the greedy
        controller is unchanged.
        """
        if self._terminal is not None:
            return self._terminal
        self._routing = None
        if self._pending is not None:
            return self._decision(
                AWAITING_FEEDBACK,
                "proposal outstanding harus diselesaikan dari cleanup/post-release feedback; tidak ada replay",
                "PENDING_FEEDBACK",
                current_sample,
                current_target,
            )
        if cancelled:
            return self._finish(
                CANCELLED,
                "owner/runtime cancellation",
                "CANCELLED",
                current_sample,
                current_target,
            )
        if self._startup_blocker is not None:
            blocker, reason = self._startup_blocker
            return self._finish(
                UNAVAILABLE, reason, blocker, current_sample, current_target
            )
        if not _plain_int(remaining_parent_budget) or remaining_parent_budget < 0:
            return self._finish(
                UNCERTAIN,
                "remaining_parent_budget invalid",
                "INVALID_PARENT_BUDGET",
                current_sample,
                current_target,
            )
        if routing is not None and not isinstance(routing, ApproachRouting):
            return self._finish(
                UNCERTAIN,
                "routing invalid; fail closed",
                "INVALID_ROUTING",
                current_sample,
                current_target,
            )
        self._routing = routing

        context = self._validate_context(
            sample=current_sample,
            target=current_target,
            now=current_monotonic,
            session_id=current_session_id,
            area_id=current_area_id,
            permission_granted=permission_granted,
        )
        if context is not None:
            status, blocker, reason = context
            return self._finish(
                status, reason, blocker, current_sample, current_target
            )
        assert current_sample is not None and current_target is not None
        if routing is not None:
            # A no-motion press is not a permanent obstacle. The caller must explicitly
            # supply fresh native-open cells; never reset failure counts or certify an edge.
            self._no_progress_tiles.difference_update(routing.recheck_tiles)

        passive_oscillation = self._ingest_current_sample(current_sample)
        if passive_oscillation:
            if self._arm_replan(
                _direction_between(current_sample.pose.tile, self._previous_tile()),
                "observed tile oscillation",
            ):
                return self._decision(
                    REPLAN_REQUIRED,
                    "observed physical tile oscillation; one bounded detour armed",
                    "OSCILLATION",
                    current_sample,
                    current_target,
                )
            return self._finish(
                self._blocked_status(),
                "tile oscillation persisted after bounded replan",
                "OSCILLATION",
                current_sample,
                current_target,
            )

        if current_sample.pose.tile in current_target.reach_tiles:
            return self._finish(
                ARRIVED,
                "fresh observed pose satisfies explicit reach condition",
                None,
                current_sample,
                current_target,
            )

        if current_monotonic >= self.deadline_at:
            return self._finish(
                self._resource_status(),
                "caller deadline exhausted before next proposal",
                "DEADLINE_EXHAUSTED",
                current_sample,
                current_target,
            )
        local_remaining = self._proposal_budget_cap - self._proposals_used
        if local_remaining <= 0 or remaining_parent_budget <= 0:
            return self._finish(
                self._resource_status(),
                "proposal budget exhausted; lower parent budget wins",
                "BUDGET_EXHAUSTED",
                current_sample,
                current_target,
            )

        choice = self._choose_direction(current_sample, current_target)
        if choice is None:
            if self._replans_used < self.policy.max_replans:
                preferred = self._preferred_directions(
                    current_sample.pose.tile, current_target
                )
                blocked = preferred[0] if preferred else None
                if self._arm_replan(blocked, "no improving candidate remains"):
                    return self._decision(
                        REPLAN_REQUIRED,
                        "no improving candidate; one bounded detour armed",
                        "LOCAL_EVIDENCE_LIMIT",
                        current_sample,
                        current_target,
                    )
            return self._finish(
                self._blocked_status(),
                "bounded local candidate set exhausted; not a physical impossibility claim",
                "LOCAL_EVIDENCE_LIMIT",
                current_sample,
                current_target,
            )

        direction, detour = choice
        next_tile = _step(current_sample.pose.tile, direction)
        witnessed = (
            current_sample.pose.tile,
            next_tile,
        ) in self._episode.edges
        attempt_id = (
            f"{self.binding.episode.episode_id}-approach-"
            f"{self._proposals_used + 1:02d}"
        )
        # fix92A / ADR 162: with the mod's guarded path, the routed observed-open path (with turns) is one proposal.
        path = self._guarded_path(current_sample, current_target, direction) if self.policy.path_guard else None
        segment = path[0] if path else self._straight_segment(current_sample, current_target, direction)
        proposal = MovementProposal(
            attempt_id=attempt_id,
            direction=direction,
            ticks=(path[1] if path else
                   self._movement_ticks(current_sample, direction, len(segment)) if segment
                   else self._movement_ticks(current_sample, direction)),
            evidence_kind=CANDIDATE_PROBE,
            historically_witnessed=witnessed,
            before_accepted_seq=current_sample.accepted_seq,
            before_received_at=current_sample.pose.received_at,
            before_tile=current_sample.pose.tile,
            target_ref=current_target.target_ref,
            target_revision=current_target.target_revision,
            replan_index=self._replans_used,
            segment_tiles=segment,
            segment_stop=segment[-1] if segment and self.policy.segment_guard else None,
            path_next=path[2] if path else (),
            path_tiles=path[3] if path else (),
        )
        self._proposals_used += 1
        self._pending = _PendingAttempt(
            proposal=proposal, before=current_sample, detour=detour
        )
        return self._decision(
            PROPOSED,
            "one bounded cardinal candidate probe",
            None,
            current_sample,
            current_target,
            proposal=proposal,
        )

    def discard_unpublished(self, proposal_id: str, reason: str) -> ApproachDecision | None:
        """Dispose only an exact proposal the executor proved never published.

        The sole executor must prove zero publication, confirmed cleanup and
        current ownership before calling. This is audit, not movement feedback;
        proposal cost and all traversal state remain consumed/preserved.
        """
        pending = self._pending
        if (self._terminal is not None or pending is None
                or type(proposal_id) is not str or proposal_id != pending.proposal.attempt_id
                or reason != "prepublication_context_changed"):
            return None
        self._append_evidence(pending, after=None,
            admission_status="UNPUBLISHED_DISCARDED", observation_status=None,
            edge=None, reason=reason)
        self._pending = None
        return self._decision("UNPUBLISHED_DISCARDED", reason, None, None, None)

    def consume_feedback(
        self,
        *,
        proposal_id: str,
        admitted: SampleBoundary,
        released: SampleBoundary,
        after: EpisodeSample,
        current_target: ApproachTarget | None,
        current_monotonic: float,
        current_session_id: str,
        current_area_id: str,
        permission_granted: bool,
        cleanup_status: str,
        held_ticks: int | None = None,
    ) -> ApproachDecision:
        """Consume one executor result; never retries or sends input itself.

        ``held_ticks`` (fix89D / ADR 158): the H frames the mod reports for a guarded segment hold, which may
        settle before its tick cap. Speed is calibrated only from frames actually held; never from the cap.
        """
        if self._terminal is not None:
            return self._terminal
        pending = self._pending
        if pending is None:
            return self._finish(
                UNCERTAIN,
                "feedback tanpa pending proposal",
                "UNEXPECTED_FEEDBACK",
                after,
                current_target,
            )
        if proposal_id != pending.proposal.attempt_id:
            return self._finish(
                UNCERTAIN,
                "feedback attempt_id tidak cocok; no replay",
                "ATTEMPT_MISMATCH",
                after,
                current_target,
            )
        if cleanup_status not in (CLEANUP_CONFIRMED, CLEANUP_UNCERTAIN):
            return self._finish(
                UNCERTAIN,
                "cleanup_status tidak dikenal",
                "INVALID_CLEANUP_STATUS",
                after,
                current_target,
            )
        if cleanup_status != CLEANUP_CONFIRMED:
            self._pending = None
            self._append_evidence(
                pending,
                after=after,
                admission_status="NOT_ADMITTED_UNCERTAIN_CLEANUP",
                observation_status=None,
                edge=None,
                reason="cleanup/release uncertain; traversal evidence not admitted",
            )
            return self._finish(
                UNCERTAIN,
                "cleanup/release uncertain; stop without replay",
                "UNCERTAIN_CLEANUP",
                after,
                current_target,
            )
        if current_session_id != self.binding.session_id:
            return self._finish(
                UNCERTAIN,
                "session berubah saat movement outstanding",
                "SESSION_CHANGED",
                after,
                current_target,
            )
        if current_area_id != self.binding.area_id:
            return self._finish(
                PARTIAL,
                "area berubah saat movement outstanding",
                "AREA_CHANGED",
                after,
                current_target,
            )
        if not permission_granted:
            return self._finish(
                PARTIAL,
                "permission tidak lagi tersedia setelah attempt",
                "PERMISSION_REVOKED",
                after,
                current_target,
            )
        if (
            not _finite_nonnegative(current_monotonic)
            or current_monotonic < self._current_monotonic
        ):
            return self._finish(
                UNCERTAIN,
                "feedback monotonic time invalid/out-of-order",
                "OUT_OF_ORDER_TIME",
                after,
                current_target,
            )
        self._current_monotonic = float(current_monotonic)

        admission = self._episode.admit_observation(
            attempt_id=pending.proposal.attempt_id,
            direction=pending.proposal.direction,
            before=pending.before,
            admitted=admitted,
            released=released,
            after=after,
            current_monotonic=current_monotonic,
        )
        self._pending = None
        obs_status = (
            admission.observation.status
            if admission.observation is not None
            else None
        )
        self._append_evidence(
            pending,
            after=after,
            admission_status=admission.status,
            observation_status=obs_status,
            edge=admission.edge,
            reason=admission.reason,
        )

        if admission.status == LOCATION_FENCED or obs_status == LOCATION_CHANGED:
            return self._finish(
                PARTIAL,
                "observed location transition; not arrival or a causal door claim",
                "LOCATION_CHANGED",
                after,
                current_target,
            )
        if admission.status in {
            EPOCH_FENCED,
            BINDING_FENCED,
            GAP_EXCEEDED,
            STALE_SAMPLE,
            OUT_OF_ORDER_SAMPLE,
            NOT_FIRST_AFTER_RELEASE,
            EPISODE_NOT_ACTIVE,
            EPISODE_INVALID_INPUT,
        }:
            return self._finish(
                UNCERTAIN,
                f"feedback evidence rejected: {admission.reason}",
                admission.status,
                after,
                current_target,
            )
        if admission.status == CAPACITY_EXHAUSTED:
            return self._finish(
                self._resource_status(),
                "navigation evidence episode capacity exhausted",
                "EVIDENCE_CAPACITY_EXHAUSTED",
                after,
                current_target,
            )

        before_tile = pending.before.pose.tile if hasattr(pending, "before") else None
        slide = (
            obs_status == AMBIGUOUS_DISPLACEMENT
            and not pending.proposal.segment_tiles
            and before_tile is not None
            and max(abs(after.pose.tile[0] - before_tile[0]), abs(after.pose.tile[1] - before_tile[1])) == 1
        )
        segment_progress = (obs_status == AMBIGUOUS_DISPLACEMENT
                            and self._segment_feedback(pending, after))
        if not slide and not segment_progress and obs_status in {
            AMBIGUOUS_DISPLACEMENT,
            STALE_READBACK,
            IDENTITY_CHANGED,
            OBSERVATION_INVALID_INPUT,
        }:
            return self._finish(
                UNCERTAIN,
                f"unexpected/invalid physical readback: {obs_status}",
                obs_status,
                after,
                current_target,
            )

        target_problem = self._target_problem(current_target, current_monotonic)
        if target_problem is not None:
            blocker, reason = target_problem
            return self._finish(
                PARTIAL if self._proposals_used else UNAVAILABLE,
                reason,
                blocker,
                after,
                current_target,
            )
        assert current_target is not None

        if segment_progress:
            # The episode validated identity/freshness/first-after-release above.
            # Its canonical classifier admits ZERO edges for a multi-tile move.
            # Only the independently observed endpoint counts; never synthesize
            # intermediate traversal witnesses from occupancy or command completion.
            dx, dy = _DELTA[pending.proposal.direction]
            transverse = ((after.pose.pixel[0] - pending.before.pose.pixel[0]) * dy
                          - (after.pose.pixel[1] - pending.before.pose.pixel[1]) * dx)
            frames = _held_frames(pending.proposal, held_ticks)
            if pending.proposal.path_next and (after.pose.tile not in pending.proposal.segment_tiles
                                               or abs(transverse) >= 0.01):
                frames = None  # fix92A / ADR 162: an endpoint past (or into) a turn keeps the previous speed estimate
            if frames is not None:  # a guarded hold without a valid frame count keeps the previous estimate
                self._pixels_per_tick = (None if abs(transverse) >= 0.01 else
                    ((after.pose.pixel[0] - pending.before.pose.pixel[0]) * dx
                     + (after.pose.pixel[1] - pending.before.pose.pixel[1]) * dy) / frames)
            self._last_sample = after
            self._progress_events += 1
            self._continue_direction = None
            self._visited_counts[after.pose.tile] += 1
            self._append_recent_tile(after.pose.tile)
            if after.pose.tile in current_target.reach_tiles:
                return self._finish(ARRIVED, "fresh observed segment endpoint satisfies current reach",
                                    None, after, current_target)
            return self._decision(PROGRESS, "fresh observed segment endpoint; zero inferred edges",
                                  None, after, current_target)

        if slide:
            self._pixels_per_tick = None  # collision feedback requires a fixed-tick probe before resizing
            # Live 2026-10-07 (M1 attempt 10z9): pressing up into the chest made Stardew slide the
            # farmer 12.5 px sideways around the corner, across a tile boundary. The pose is fresh and
            # admitted; only the direction differs. Progress from the observed pose, never an edge;
            # multi-tile jumps stay UNCERTAIN.
            self._last_sample = after
            self._progress_events += 1
            self._continue_direction = None
            return self._decision(
                PROGRESS,
                "observed one-tile slide off the proposed direction; no directed edge fabricated",
                "SLIDE_DISPLACEMENT",
                after,
                current_target,
            )

        if obs_status in {SUBTILE_PROGRESS, CONFIRMED_LOCAL_STEP}:
            # Only admitted, released, clean physical feedback reaches here. A slide or jump cannot calibrate.
            # Subtile/one-tile readback can also slide sideways in pixels; never retain an older estimate then.
            frames = _held_frames(pending.proposal, held_ticks)
            if frames is not None:  # fix89D: a guarded hold with no valid frame count keeps the old estimate
                self._pixels_per_tick = None
            before_pixel, after_pixel = pending.before.pose.pixel, after.pose.pixel
            if frames is not None and before_pixel is not None and after_pixel is not None:
                dx, dy = _DELTA[pending.proposal.direction]
                px, py = after_pixel[0] - before_pixel[0], after_pixel[1] - before_pixel[1]
                distance, transverse = px * dx + py * dy, px * dy - py * dx
                if 0 < distance < 128 and abs(transverse) < 0.01:
                    self._pixels_per_tick = distance / frames

        if obs_status == SUBTILE_PROGRESS:
            self._last_sample = after
            self._progress_events += 1
            self._continue_direction = pending.proposal.direction
            return self._decision(
                PROGRESS,
                "fresh pixel displacement observed; no directed edge fabricated",
                None,
                after,
                current_target,
            )

        if obs_status == NO_TILE_PROGRESS:
            # The preceding partial press may have hit a collision, rather than measured walking speed.
            self._pixels_per_tick = None
            self._last_sample = after
            self._continue_direction = None
            self._no_progress_tiles.add(
                _step(pending.before.pose.tile, pending.proposal.direction)
            )
            key = self._failure_key(after, pending.proposal.direction)
            self._failure_counts[key] += 1
            count = self._failure_counts[key]
            if count >= self.policy.repeated_failure_threshold:
                if self._arm_replan(
                    pending.proposal.direction,
                    f"repeated no-progress x{count}",
                ):
                    return self._decision(
                        REPLAN_REQUIRED,
                        f"repeated physical no-progress x{count}; one bounded detour armed",
                        "NO_PROGRESS_THRESHOLD",
                        after,
                        current_target,
                    )
                return self._finish(
                    self._blocked_status(),
                    "repeated no-progress after bounded replan; edge remains unknown, not certified blocked",
                    "NO_PROGRESS_THRESHOLD",
                    after,
                    current_target,
                )
            return self._decision(
                NO_PROGRESS,
                f"no physical displacement observed ({count}/{self.policy.repeated_failure_threshold})",
                "NO_PROGRESS",
                after,
                current_target,
            )

        if (
            obs_status == CONFIRMED_LOCAL_STEP
            and admission.status in {EDGE_ADMITTED, DUPLICATE_OBSERVATION}
        ):
            self._last_sample = after
            self._progress_events += 1
            self._continue_direction = None
            self._visited_counts[after.pose.tile] += 1
            self._append_recent_tile(after.pose.tile)
            if pending.detour:
                self._detour_pending = False
                self._detour_blocked_direction = None
            if self._has_oscillation():
                forbidden = _direction_between(
                    after.pose.tile, self._previous_tile()
                )
                if self._arm_replan(
                    forbidden, "observed A-B-A-B tile oscillation"
                ):
                    return self._decision(
                        REPLAN_REQUIRED,
                        "observed tile oscillation; one bounded detour armed",
                        "OSCILLATION",
                        after,
                        current_target,
                    )
                return self._finish(
                    self._blocked_status(),
                    "tile oscillation persisted after bounded replan",
                    "OSCILLATION",
                    after,
                    current_target,
                )
            if after.pose.tile in current_target.reach_tiles:
                return self._finish(
                    ARRIVED,
                    "fresh observed pose satisfies explicit still-current reach condition",
                    None,
                    after,
                    current_target,
                )
            return self._decision(
                PROGRESS,
                "fresh one-tile displacement observed and directed witness handled canonically",
                None,
                after,
                current_target,
            )

        if admission.status == CLASSIFIER_NO_EDGE:
            return self._finish(
                UNCERTAIN,
                f"classifier returned unsupported no-edge state {obs_status!r}",
                "UNSUPPORTED_NO_EDGE_STATE",
                after,
                current_target,
            )
        if admission.status == DUPLICATE_OBSERVATION:
            return self._finish(
                UNCERTAIN,
                "duplicate evidence without a fresh physical witness",
                "DUPLICATE_FEEDBACK",
                after,
                current_target,
            )
        return self._finish(
            UNCERTAIN,
            f"unhandled evidence status {admission.status}",
            admission.status,
            after,
            current_target,
        )

    def _validate_context(
        self,
        *,
        sample: EpisodeSample | None,
        target: ApproachTarget | None,
        now: float,
        session_id: str,
        area_id: str,
        permission_granted: bool,
    ) -> tuple[str, str, str] | None:
        if not _finite_nonnegative(now) or now < self._current_monotonic:
            return (
                UNCERTAIN,
                "OUT_OF_ORDER_TIME",
                "current_monotonic invalid atau mundur",
            )
        self._current_monotonic = float(now)
        if session_id != self.binding.session_id:
            return (UNCERTAIN, "SESSION_CHANGED", "session binding berubah")
        if area_id != self.binding.area_id:
            return (
                PARTIAL if self._proposals_used else UNAVAILABLE,
                "AREA_CHANGED",
                "area binding berubah",
            )
        if not permission_granted:
            return (
                PARTIAL if self._proposals_used else UNAVAILABLE,
                "PERMISSION_UNAVAILABLE",
                "parent permission tidak tersedia",
            )
        if sample is None:
            return (
                PARTIAL if self._proposals_used else UNAVAILABLE,
                "MISSING_POSE",
                "current pose tidak tersedia",
            )
        sample_problem = self._sample_problem(sample, now)
        if sample_problem is not None:
            blocker, reason = sample_problem
            status = (
                UNCERTAIN
                if blocker
                in {
                    "EPOCH_CHANGED",
                    "IDENTITY_CHANGED",
                    "SAMPLE_OUT_OF_ORDER",
                    "SAMPLE_IDENTITY_CONFLICT",
                }
                else (PARTIAL if self._proposals_used else UNAVAILABLE)
            )
            return (status, blocker, reason)
        target_problem = self._target_problem(target, now)
        if target_problem is not None:
            blocker, reason = target_problem
            return (
                PARTIAL if self._proposals_used else UNAVAILABLE,
                blocker,
                reason,
            )
        return None

    def _sample_problem(
        self, sample: EpisodeSample, now: float
    ) -> tuple[str, str] | None:
        if not isinstance(sample, EpisodeSample):
            return ("INVALID_POSE", "current pose bukan EpisodeSample")
        shape_problem = _sample_shape_problem(sample)
        if shape_problem is not None:
            return ("INVALID_POSE", shape_problem)
        ep = self.binding.episode
        if now - ep.started_at > self.evidence_policy.max_episode_age:
            return (
                "EPISODE_EXPIRED",
                "canonical navigation evidence episode melewati max_episode_age",
            )
        if (
            self._last_sample is not None
            and sample.accepted_seq == self._last_sample.accepted_seq
            and sample != self._last_sample
        ):
            return (
                "SAMPLE_IDENTITY_CONFLICT",
                "accepted_seq yang sama membawa canonical sample berbeda; fail closed",
            )
        if sample.receiver_epoch_id != ep.receiver_epoch_id:
            return ("EPOCH_CHANGED", "receiver epoch berubah")
        if sample.location_key != ep.location_key:
            return ("LOCATION_CHANGED", "location key berubah")
        if (
            sample.pose.player_id != ep.player_id
            or sample.pose.screen_id != ep.screen_id
        ):
            return ("IDENTITY_CHANGED", "player/screen binding berubah")
        if sample.pose.received_at > now:
            return (
                "SAMPLE_OUT_OF_ORDER",
                "pose received_at berada di masa depan terhadap current_monotonic",
            )
        age = now - sample.pose.received_at
        if age > self.evidence_policy.max_after_age:
            return (
                "STALE_POSE",
                "current pose melewati evidence freshness bound",
            )
        if self._last_sample is not None:
            if (
                sample.accepted_seq < self._last_sample.accepted_seq
                or sample.pose.received_at < self._last_sample.pose.received_at
            ):
                return (
                    "SAMPLE_OUT_OF_ORDER",
                    "current pose lebih lama dari pose controller terakhir",
                )
        return None

    def _target_problem(
        self, target: ApproachTarget | None, now: float
    ) -> tuple[str, str] | None:
        if target is None:
            return ("TARGET_REMOVED", "target tidak lagi teramati")
        if not isinstance(target, ApproachTarget):
            return ("INVALID_TARGET", "current target bukan ApproachTarget")
        if self._target_pin is None:
            return (
                "MISSING_TARGET_PIN",
                "controller tidak punya initial target pin",
            )
        if target.pin != self._target_pin:
            return (
                "TARGET_CHANGED",
                "target identity/revision/observation lifetime/reach geometry berubah",
            )
        ep = self.binding.episode
        if target.session_id != self.binding.session_id:
            return ("SESSION_CHANGED", "target session tidak cocok")
        if target.receiver_epoch_id != ep.receiver_epoch_id:
            return ("EPOCH_CHANGED", "target receiver epoch tidak cocok")
        if target.location_key != ep.location_key:
            return ("LOCATION_CHANGED", "target location key tidak cocok")
        if target.area_id != self.binding.area_id:
            return ("AREA_CHANGED", "target area tidak cocok")
        if target.observed_at > now:
            return (
                "TARGET_OUT_OF_ORDER",
                "target observed_at berada di masa depan",
            )
        if now > target.valid_until:
            return ("TARGET_EXPIRED", "target observation sudah expired")
        return None

    def _ingest_current_sample(self, sample: EpisodeSample) -> bool:
        previous = self._last_sample
        if previous is None:
            self._last_sample = sample
            self._append_recent_tile(sample.pose.tile)
            return False
        moved = (
            sample.pose.tile != previous.pose.tile
            or sample.pose.pixel != previous.pose.pixel
        )
        tile_changed = sample.pose.tile != previous.pose.tile
        self._last_sample = sample
        if moved:
            self._continue_direction = None
        if tile_changed:
            self._visited_counts[sample.pose.tile] += 1
            self._append_recent_tile(sample.pose.tile)
            return self._has_oscillation()
        return False

    def _straight_segment(self, sample: EpisodeSample, target: ApproachTarget,
                          direction: str) -> tuple[Tile, ...]:
        routing = self._routing
        if (self.policy.max_segment_tiles == 1 or not self.policy.adaptive_move_ticks
                or self._pixels_per_tick is None or routing is None):
            return ()
        distances = self._route_distances(sample.pose.tile, target, routing)
        tile = sample.pose.tile
        segment = []
        for _ in range(self.policy.max_segment_tiles):
            nxt = _step(tile, direction)
            if (nxt not in routing.passable_tiles or nxt in self._no_progress_tiles
                    or distances.get(nxt) != distances.get(tile, -1) - 1):
                break
            segment.append(nxt)
            tile = nxt
            if tile in target.reach_tiles:
                break
        return tuple(segment) if len(segment) > 1 else ()

    def _guarded_path(self, sample: EpisodeSample, target: ApproachTarget, direction: str
                      ) -> tuple[tuple[Tile, ...], int, tuple[Tile, ...], tuple[Tile, ...]] | None:
        """fix92A / ADR 162: the routed path from the fresh pose as straight segments with 90-degree turns.

        Same rule as ``_straight_segment`` for every tile: observed-open (``passable_tiles``), not a no-progress tile
        of this episode, one step closer on the current BFS distance field. A segment ends at the target reach, at
        10 tiles, or where the next tile breaks that rule; the path then turns to the perpendicular direction that
        keeps the rule (longest straight run first, then the nearer reach, then the fixed direction order). Bounded
        by 8 segments, 30 tiles and the 180-frame cap (estimated from the measured speed plus fix89D's slack).
        Returns (first segment, ticks, the later stops, every tile in order), or None when fewer than two segments
        fit: the caller then keeps fix89D's straight segment. Nothing is inferred beyond the current observation.
        """
        routing = self._routing
        speed = self._pixels_per_tick
        pixel, start = sample.pose.pixel, sample.pose.tile
        if (not self.policy.adaptive_move_ticks or speed is None or not speed > 0 or routing is None
                or pixel is None or start is None or start in target.reach_tiles):
            return None
        centre = (pixel[0] + 32, pixel[1] + 16)
        if tuple(math.floor(p / 64) for p in centre) != start:
            return None  # inconsistent geometry cannot size a path
        distances = self._route_distances(start, target, routing)
        if not distances:
            return None

        def run(tile: Tile, heading: str, room: int) -> tuple[Tile, ...]:
            out: list[Tile] = []
            while len(out) < min(SEGMENT_GUARD_TILES_MAX, room):
                nxt = _step(tile, heading)
                if (nxt not in routing.passable_tiles or nxt in self._no_progress_tiles
                        or distances.get(nxt) != distances.get(tile, -1) - 1):
                    break
                out.append(nxt)
                tile = nxt
                if tile in target.reach_tiles:
                    break
            return tuple(out)

        segments: list[tuple[str, tuple[Tile, ...]]] = []
        tile, heading, total = start, direction, 0
        first = run(tile, heading, PATH_GUARD_TILES_MAX)
        if not first:
            return None
        segments.append((heading, first))
        total, tile = len(first), first[-1]
        while (len(segments) < PATH_GUARD_SEGMENTS_MAX and total < PATH_GUARD_TILES_MAX
               and tile not in target.reach_tiles):
            preferred = self._preferred_directions(tile, target)
            ranked = []
            for turn in _PERPENDICULAR[heading]:
                tiles = run(tile, turn, PATH_GUARD_TILES_MAX - total)
                if tiles:
                    ranked.append(((-len(tiles), _distance_to_reach(tiles[-1], target.reach_tiles),
                                    preferred.index(turn), _DIRECTIONS.index(turn)), turn, tiles))
            if not ranked:
                break
            ranked.sort(key=lambda item: item[0])
            _score, heading, tiles = ranked[0]
            segments.append((heading, tiles))
            total, tile = total + len(tiles), tiles[-1]

        dx, dy = _DELTA[direction]
        axis, sign = (0, dx) if dx else (1, dy)

        def frames(segs: list[tuple[str, tuple[Tile, ...]]]) -> int:
            stop = segs[0][1][-1]
            px = sign * (stop[axis] * 64 + 32 - centre[axis]) + sum(len(t) * 64 for _h, t in segs[1:])
            return math.ceil(px / speed) + SEGMENT_GUARD_SLACK_TICKS

        while len(segments) > 1 and frames(segments) > PATH_GUARD_TICKS_MAX:
            segments.pop()
        if len(segments) < 2 or sign * (segments[0][1][-1][axis] * 64 + 32 - centre[axis]) <= 0:
            return None
        return (segments[0][1], max(1, frames(segments)), tuple(t[-1] for _h, t in segments[1:]),
                tuple(t for _h, seg in segments for t in seg))

    @staticmethod
    def _path_feedback(pending: _PendingAttempt, after: EpisodeSample) -> bool:
        """fix92A / ADR 162: a path endpoint proves only itself: on a published path tile, consistent geometry,
        inside the corridor of a segment holding that tile. Never intermediate edges or walking speed."""
        proposal = pending.proposal
        tile = after.pose.tile
        before_pixel, after_pixel = pending.before.pose.pixel, after.pose.pixel
        if (tile not in proposal.path_tiles or before_pixel is None or after_pixel is None or proposal.segment_stop is None
                or tuple(math.floor(v / 64) for v in (after_pixel[0] + 32, after_pixel[1] + 16)) != tile):
            return False
        first = len(proposal.segment_tiles)
        if proposal.path_tiles.index(tile) < first:
            dx, dy = _DELTA[proposal.direction]
            px, py = after_pixel[0] - before_pixel[0], after_pixel[1] - before_pixel[1]
            if 0 < px * dx + py * dy <= first * 64 + 32 and abs(px * dy - py * dx) <= 8:
                return True
        cx, cy = after_pixel[0] + 32, after_pixel[1] + 16
        stops = (proposal.segment_stop, *proposal.path_next)
        for a, b in zip(stops, stops[1:]):
            if a[0] == b[0] == tile[0] and min(a[1], b[1]) <= tile[1] <= max(a[1], b[1]):
                if abs(cx - (a[0] * 64 + 32)) <= PATH_GUARD_TURN_PX:
                    return True
            if a[1] == b[1] == tile[1] and min(a[0], b[0]) <= tile[0] <= max(a[0], b[0]):
                if abs(cy - (a[1] * 64 + 32)) <= PATH_GUARD_TURN_PX:
                    return True
        return False

    @staticmethod
    def _segment_feedback(pending: _PendingAttempt, after: EpisodeSample) -> bool:
        proposal = pending.proposal
        if proposal.path_next:
            return LocalApproachController._path_feedback(pending, after)  # fix92A / ADR 162
        if not proposal.segment_tiles or after.pose.tile not in proposal.segment_tiles:
            return False
        before_pixel, after_pixel = pending.before.pose.pixel, after.pose.pixel
        if before_pixel is None or after_pixel is None:
            return False
        dx, dy = _DELTA[proposal.direction]
        px, py = after_pixel[0] - before_pixel[0], after_pixel[1] - before_pixel[1]
        # A small collision slide within the same published straight corridor
        # proves only its endpoint, never intermediate edges or walking speed.
        return (0 < px * dx + py * dy <= len(proposal.segment_tiles) * 64 + 32
                and abs(px * dy - py * dx) <= 8
                and tuple(math.floor(v / 64) for v in (after_pixel[0]+32, after_pixel[1]+16)) == after.pose.tile)

    def _movement_ticks(self, sample: EpisodeSample, direction: str, segment_length: int = 0) -> int:
        """A measured distance estimate is a probe; readback is the only arrival oracle.

        Start with the existing ten ticks. After clean cardinal feedback, aim for the next tile centre, capped
        at 24 ticks (0.4 s at 60 Hz), well below the old model fallback's 60. No input/time budget is refunded.
        P13 may size one to four explicitly open straight cells / 60 ticks. Its
        bounded multi-tile feedback proves an endpoint only, never traversal edges.
        Other callers retain the original one-tile feedback rule.
        """
        if not self.policy.adaptive_move_ticks or self._pixels_per_tick is None:
            return self.policy.move_ticks
        pixel, tile = sample.pose.pixel, sample.pose.tile
        if pixel is None or tile is None:
            return self.policy.move_ticks
        # Canonical motor pixel = Farmer.Position; TilePoint uses the 48x32 bounding-box centre (+32,+16).
        centre = (pixel[0] + 32, pixel[1] + 16)
        if tuple(math.floor(p / 64) for p in centre) != tile:
            return self.policy.move_ticks  # inconsistent geometry cannot size a longer press
        dx, dy = _DELTA[direction]
        axis, sign = (0, dx) if dx else (1, dy)
        distance = sign * ((tile[axis] + sign * max(1, segment_length)) * 64 + 32 - centre[axis])
        if self.policy.segment_guard and segment_length > 1:
            # fix89D / ADR 158: the mod ends the hold at the stop tile centre; the count is a bounded cap with slack.
            return max(1, min(SEGMENT_GUARD_TICKS_MAX,
                              math.ceil(distance / self._pixels_per_tick) + SEGMENT_GUARD_SLACK_TICKS))
        cap = 60 if self.policy.max_segment_tiles > 1 else 24
        return max(1, min(cap, math.floor(distance / self._pixels_per_tick) if segment_length > 1
                          else math.ceil(distance / self._pixels_per_tick)))

    def _choose_direction(
        self, sample: EpisodeSample, target: ApproachTarget
    ) -> tuple[str, bool] | None:
        tile = sample.pose.tile
        routing = self._routing
        if self._continue_direction is not None:
            direction = self._continue_direction
            if (
                self._failure_counts[self._failure_key(sample, direction)]
                < self.policy.repeated_failure_threshold
                and not (
                    routing is not None
                    and (
                        _step(tile, direction) in routing.blocked_tiles
                        or _step(tile, direction) in self._no_progress_tiles
                    )
                )
            ):
                return (direction, self._detour_pending)
            self._continue_direction = None

        if routing is not None:
            routed = self._choose_routed_direction(sample, target, routing)
            if routed is not None:
                return (routed, self._detour_pending)

        if self._detour_pending:
            direction = self._choose_detour_direction(sample, target)
            return (direction, True) if direction is not None else None

        current_distance = _distance_to_reach(tile, target.reach_tiles)
        ranked: list[tuple[tuple[object, ...], str]] = []
        priority = self._preferred_directions(tile, target)
        priority_index = {
            direction: index for index, direction in enumerate(priority)
        }
        known_blocked = routing.blocked_tiles if routing is not None else frozenset()
        for direction in _DIRECTIONS:
            next_tile = _step(tile, direction)
            if next_tile in known_blocked:
                continue  # FIXB15 D-B13-1: the greedy fallback never presses into a known-blocked cell
            new_distance = _distance_to_reach(next_tile, target.reach_tiles)
            if new_distance >= current_distance:
                continue
            failures = self._failure_counts[
                self._failure_key(sample, direction)
            ]
            if failures >= self.policy.repeated_failure_threshold:
                continue
            witnessed = (tile, next_tile) in self._episode.edges
            score = (
                0 if witnessed else 1,
                self._visited_counts[next_tile],
                new_distance,
                priority_index.get(direction, len(_DIRECTIONS)),
                _DIRECTIONS.index(direction),
            )
            ranked.append((score, direction))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return (ranked[0][1], False)

    def _choose_routed_direction(
        self,
        sample: EpisodeSample,
        target: ApproachTarget,
        routing: ApproachRouting,
    ) -> str | None:
        """First step of a bounded 4-neighbour shortest path, or None (greedy fallback).

        Known non-passable facts and this episode's no-progress tiles are avoided; unknown
        tiles are optimistic. Ties keep the greedy preference order.
        """
        tile = sample.pose.tile
        distances = self._route_distances(tile, target, routing)
        current = distances.get(tile)
        if current is None or current == 0:
            return None
        priority = self._preferred_directions(tile, target)
        ranked: list[tuple[tuple[object, ...], str]] = []
        for direction in _DIRECTIONS:
            next_tile = _step(tile, direction)
            distance = distances.get(next_tile)
            if distance is None or distance >= current:
                continue
            if (
                self._failure_counts[self._failure_key(sample, direction)]
                >= self.policy.repeated_failure_threshold
            ):
                continue
            witnessed = (tile, next_tile) in self._episode.edges
            score = (
                distance,
                0 if witnessed else 1,
                0 if next_tile in routing.passable_tiles else 1,
                self._visited_counts[next_tile],
                _distance_to_reach(next_tile, target.reach_tiles),
                priority.index(direction),
                _DIRECTIONS.index(direction),
            )
            ranked.append((score, direction))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _route_distances(
        self, start: Tile, target: ApproachTarget, routing: ApproachRouting
    ) -> dict[Tile, int]:
        """Reverse BFS from the reach tiles inside a bounded window; empty when ``start`` is
        not reached within ROUTING_MAX_NODES."""
        blocked = set(routing.blocked_tiles) | self._no_progress_tiles
        if target.reach_kind == ADJACENT_INTERACTION:
            blocked.add(target.target_tile)  # the interaction target occupies its own tile
        blocked.discard(start)  # the fresh observed pose stands here, whatever a stale fact says

        grid = routing.window

        def in_window(tile: Tile) -> bool:
            return tile[0] >= 0 and tile[1] >= 0 and (
                _chebyshev(tile, start) <= ROUTING_RADIUS
                or _chebyshev(tile, target.target_tile) <= ROUTING_RADIUS
                or (grid is not None and grid[0] <= tile[0] <= grid[2] and grid[1] <= tile[1] <= grid[3])
            )

        max_nodes = ROUTING_MAX_NODES if grid is None else ROUTING_MAX_NODES + ROUTING_WINDOW_MAX_NODES

        distances: dict[Tile, int] = {}
        queue: deque[Tile] = deque()
        for goal in sorted(target.reach_tiles):
            if goal not in blocked and in_window(goal):
                distances[goal] = 0
                queue.append(goal)
        while queue and start not in distances:
            node = queue.popleft()
            for direction in _DIRECTIONS:
                dx, dy = _DELTA[direction]
                previous = (node[0] - dx, node[1] - dy)  # pressing `direction` there lands on node
                if (
                    previous in distances
                    or previous in blocked
                    or not in_window(previous)
                ):
                    continue
                if len(distances) >= max_nodes:
                    return {}
                distances[previous] = distances[node] + 1
                queue.append(previous)
        return distances if start in distances else {}

    def _choose_detour_direction(
        self, sample: EpisodeSample, target: ApproachTarget
    ) -> str | None:
        blocked = self._detour_blocked_direction
        candidates = list(_PERPENDICULAR.get(blocked or "", _DIRECTIONS))
        tile = sample.pose.tile
        ranked: list[tuple[tuple[object, ...], str]] = []
        known_blocked = (self._routing.blocked_tiles if self._routing is not None else frozenset())
        for direction in candidates:
            if direction == blocked:
                continue
            next_tile = _step(tile, direction)
            if next_tile[0] < 0 or next_tile[1] < 0 or next_tile in known_blocked:
                continue
            failures = self._failure_counts[
                self._failure_key(sample, direction)
            ]
            if failures >= self.policy.repeated_failure_threshold:
                continue
            witnessed = (tile, next_tile) in self._episode.edges
            score = (
                self._visited_counts[next_tile],
                0 if witnessed else 1,
                _distance_to_reach(next_tile, target.reach_tiles),
                _DIRECTIONS.index(direction),
            )
            ranked.append((score, direction))
        if not ranked:
            return None
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    def _preferred_directions(
        self, tile: Tile, target: ApproachTarget
    ) -> tuple[str, ...]:
        anchor = min(
            target.reach_tiles,
            key=lambda goal: (_manhattan(tile, goal), goal[0], goal[1]),
        )
        dx, dy = anchor[0] - tile[0], anchor[1] - tile[1]
        axes: list[tuple[int, int, str]] = []
        if dx:
            axes.append((abs(dx), 0, "right" if dx > 0 else "left"))
        if dy:
            axes.append((abs(dy), 1, "down" if dy > 0 else "up"))
        axes.sort(key=lambda item: (-item[0], item[1]))
        preferred = [direction for _magnitude, _axis, direction in axes]
        for direction in _DIRECTIONS:
            if direction not in preferred:
                preferred.append(direction)
        return tuple(preferred)

    def _arm_replan(
        self, blocked_direction: str | None, reason: str
    ) -> bool:
        if self._replans_used >= self.policy.max_replans:
            return False
        self._replans_used += 1
        self._detour_pending = True
        self._detour_blocked_direction = blocked_direction
        self._continue_direction = None
        return True

    def _failure_key(
        self, sample: EpisodeSample, direction: str
    ) -> tuple[object, ...]:
        return (
            self._target_pin,
            sample.pose.tile,
            sample.pose.pixel,
            direction,
        )

    def _append_recent_tile(self, tile: Tile) -> None:
        if self._recent_tiles and self._recent_tiles[-1] == tile:
            return
        self._recent_tiles.append(tile)
        if len(self._recent_tiles) > self.policy.recent_tile_window:
            del self._recent_tiles[
                : len(self._recent_tiles) - self.policy.recent_tile_window
            ]

    def _has_oscillation(self) -> bool:
        if len(self._recent_tiles) < 4:
            return False
        a, b, c, d = self._recent_tiles[-4:]
        return a == c and b == d and a != b

    def _previous_tile(self) -> Tile | None:
        if len(self._recent_tiles) < 2:
            return None
        return self._recent_tiles[-2]

    def _append_evidence(
        self,
        pending: _PendingAttempt,
        *,
        after: EpisodeSample | None,
        admission_status: str,
        observation_status: str | None,
        edge: tuple[Tile, Tile] | None,
        reason: str,
    ) -> None:
        after_tile = (
            after.pose.tile if isinstance(after, EpisodeSample) else None
        )
        after_pixel = (
            after.pose.pixel if isinstance(after, EpisodeSample) else None
        )
        self._evidence.append(
            ApproachEvidence(
                attempt_id=pending.proposal.attempt_id,
                direction=pending.proposal.direction,
                admission_status=admission_status,
                observation_status=observation_status,
                before_tile=pending.before.pose.tile,
                after_tile=after_tile,
                before_pixel=pending.before.pose.pixel,
                after_pixel=after_pixel,
                directed_edge=edge,
                reason=reason,
            )
        )

    def _resource_status(self) -> str:
        return PARTIAL if self._progress_events > 0 else UNAVAILABLE

    def _blocked_status(self) -> str:
        return PARTIAL if self._progress_events > 0 else BLOCKED

    def _decision(
        self,
        status: str,
        reason: str,
        blocker: str | None,
        sample: EpisodeSample | None,
        target: ApproachTarget | None,
        *,
        proposal: MovementProposal | None = None,
    ) -> ApproachDecision:
        tile = sample.pose.tile if isinstance(sample, EpisodeSample) else None
        distance = (
            _distance_to_reach(tile, target.reach_tiles)
            if tile is not None and isinstance(target, ApproachTarget)
            else None
        )
        return ApproachDecision(
            status=status,
            reason=reason,
            terminal=status in _TERMINAL,
            blocker=blocker,
            proposal=proposal,
            target_ref=(
                target.target_ref
                if isinstance(target, ApproachTarget)
                else self._target_ref
            ),
            current_tile=tile,
            distance_to_reach=distance,
            proposals_used=self._proposals_used,
            replans_used=self._replans_used,
            progress_events=self._progress_events,
            evidence=tuple(self._evidence),
        )

    def _finish(
        self,
        status: str,
        reason: str,
        blocker: str | None,
        sample: EpisodeSample | None,
        target: ApproachTarget | None,
    ) -> ApproachDecision:
        decision = self._decision(status, reason, blocker, sample, target)
        if status not in _TERMINAL:
            raise AssertionError(f"_finish called with nonterminal status {status}")
        self._terminal = decision
        if self._episode.state == "ACTIVE":
            self._episode.close(
                f"local approach terminal: {status}/{blocker or 'none'}"
            )
        self._pending = None
        return decision


def _held_frames(proposal: MovementProposal, held_ticks: object) -> int | None:
    """Frames that moved the actor: the proposal's ticks, or for a guarded segment the mod's count (ADR 158)."""
    if proposal.segment_stop is None:
        return proposal.ticks
    if _plain_int(held_ticks) and 1 <= held_ticks <= proposal.ticks:
        return held_ticks
    return None


def _distance_to_reach(tile: Tile, goals: tuple[Tile, ...]) -> int:
    return min(_manhattan(tile, goal) for goal in goals)


def _manhattan(a: Tile, b: Tile) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _chebyshev(a: Tile, b: Tile) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _step(tile: Tile, direction: str) -> Tile:
    dx, dy = _DELTA[direction]
    return (tile[0] + dx, tile[1] + dy)


def _direction_between(src: Tile, dst: Tile | None) -> str | None:
    if dst is None:
        return None
    delta = (dst[0] - src[0], dst[1] - src[1])
    for direction, expected in _DELTA.items():
        if delta == expected:
            return direction
    return None


def _sample_shape_problem(sample: EpisodeSample) -> str | None:
    pose = sample.pose
    if not isinstance(pose, PoseReadback):
        return "pose bukan PoseReadback canonical"
    if type(pose.player_id) is not int or type(pose.screen_id) is not int:
        return "pose player_id/screen_id harus integer polos"
    if not _finite_number(pose.received_at):
        return "pose received_at harus angka finite"
    if not isinstance(pose.location, str) or not pose.location.strip():
        return "pose location harus string non-kosong"
    if (
        not isinstance(pose.tile, tuple)
        or len(pose.tile) != 2
        or not all(type(part) is int for part in pose.tile)
    ):
        return "pose tile harus tuple dua integer polos"
    if pose.pixel is not None and (
        not isinstance(pose.pixel, tuple)
        or len(pose.pixel) != 2
        or not all(_finite_number(part) for part in pose.pixel)
    ):
        return "pose pixel harus tuple dua angka finite atau None"
    return None


def _require_tile(value: object, name: str) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or not all(
            _plain_int(part) and part >= 0 for part in value
        )
    ):
        raise ValueError(f"{name} harus tuple dua integer polos >= 0")


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} harus string non-kosong")


def _plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _finite_nonnegative(value: object) -> bool:
    return _finite_number(value) and value >= 0
