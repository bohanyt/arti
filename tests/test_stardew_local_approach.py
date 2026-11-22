import pytest

from arti_stardew_local_approach import (
    ADJACENT_INTERACTION,
    ARRIVED,
    BLOCKED,
    CANCELLED,
    CLEANUP_CONFIRMED,
    CLEANUP_UNCERTAIN,
    DIRECT_TARGET,
    NO_PROGRESS,
    PARTIAL,
    PROGRESS,
    PROPOSED,
    REPLAN_REQUIRED,
    UNCERTAIN,
    UNAVAILABLE,
    ApproachBinding,
    ApproachPolicy,
    ApproachTarget,
    LocalApproachController,
)
from arti_stardew_navigation_episode import (
    EpisodeBinding,
    EpisodePolicy,
    EpisodeSample,
    SampleBoundary,
)
from arti_stardew_navigation_observed import PoseReadback

EPOCH = "rx-1"
LOCATION_KEY = "Farm#instance-1"
SESSION = "session-1"
AREA = "farm-east"
PLAYER = 77
SCREEN = 0
MISSING = object()


def _sample(
    tile=(1, 1),
    *,
    seq=1,
    at=100.0,
    pixel=None,
    epoch=EPOCH,
    location_key=LOCATION_KEY,
    raw_location="Farm",
    player=PLAYER,
    screen=SCREEN,
):
    if pixel is None:
        pixel = (float(tile[0] * 64), float(tile[1] * 64))
    return EpisodeSample(
        pose=PoseReadback(
            player_id=player,
            screen_id=screen,
            received_at=at,
            location=raw_location,
            tile=tile,
            pixel=pixel,
        ),
        receiver_epoch_id=epoch,
        location_key=location_key,
        accepted_seq=seq,
    )


def _target(
    tile=(3, 1),
    *,
    revision="r1",
    reach_kind=DIRECT_TARGET,
    reach_tiles=None,
    valid_until=200.0,
    session=SESSION,
    epoch=EPOCH,
    location_key=LOCATION_KEY,
    area=AREA,
):
    if reach_tiles is None:
        reach_tiles = (tile,)
    return ApproachTarget(
        target_ref="target-1",
        target_revision=revision,
        session_id=session,
        receiver_epoch_id=epoch,
        location_key=location_key,
        area_id=area,
        observed_at=99.9,
        valid_until=valid_until,
        target_tile=tile,
        reach_kind=reach_kind,
        reach_tiles=tuple(reach_tiles),
    )


def _controller(
    *,
    initial=None,
    target=MISSING,
    move_ticks=1,
    max_proposals=32,
    failures=2,
    max_replans=1,
    parent_budget=32,
    deadline=180.0,
    max_after_age=5.0,
):
    if initial is None:
        initial = _sample()
    if target is MISSING:
        target = _target()
    binding = ApproachBinding(
        session_id=SESSION,
        area_id=AREA,
        episode=EpisodeBinding(
            episode_id="ep-211",
            receiver_epoch_id=EPOCH,
            location_key=LOCATION_KEY,
            player_id=PLAYER,
            screen_id=SCREEN,
            started_at=99.0,
        ),
    )
    evidence_policy = EpisodePolicy(
        max_before_after_gap=2.0,
        max_after_age=max_after_age,
        max_episode_age=200.0,
        max_attempts=64,
        max_edges=64,
    )
    return LocalApproachController(
        binding=binding,
        evidence_policy=evidence_policy,
        policy=ApproachPolicy(
            move_ticks=move_ticks,
            max_proposals=max_proposals,
            repeated_failure_threshold=failures,
            max_replans=max_replans,
        ),
        initial_sample=initial,
        target=target,
        deadline_at=deadline,
        parent_proposal_budget=parent_budget,
    )


class FakeExecutor:
    """No-game executor: every proposal is followed by actual synthetic readback."""

    def __init__(self, sample, *, blocked_tiles=(), pixel_only_attempts=()):
        self.current = sample
        self.blocked_tiles = set(blocked_tiles)
        self.pixel_only_attempts = set(pixel_only_attempts)
        self.proposals = []
        self.distance_log = []

    def sync_external(self, sample):
        self.current = sample

    def execute(
        self,
        controller,
        proposal,
        target,
        *,
        cleanup=CLEANUP_CONFIRMED,
        after_epoch=EPOCH,
        after_location_key=LOCATION_KEY,
        raw_location="Farm",
        after_player=PLAYER,
        after_screen=SCREEN,
        after_seq_offset=1,
        now_offset=0.08,
        after_since_release=0.01,
        after_tile_override=None,
    ):
        assert proposal.before_accepted_seq == self.current.accepted_seq
        self.proposals.append(proposal)
        before = self.current
        admitted_at = before.pose.received_at + 0.01
        released_at = before.pose.received_at + 0.02
        admitted = SampleBoundary(EPOCH, LOCATION_KEY, before.accepted_seq, admitted_at)
        released = SampleBoundary(EPOCH, LOCATION_KEY, before.accepted_seq, released_at)

        delta = {
            "right": (1, 0),
            "left": (-1, 0),
            "up": (0, -1),
            "down": (0, 1),
        }[proposal.direction]
        candidate = (before.pose.tile[0] + delta[0], before.pose.tile[1] + delta[1])
        if proposal.attempt_id in self.pixel_only_attempts:
            after_tile = before.pose.tile
            bp = before.pose.pixel or (0.0, 0.0)
            after_pixel = (bp[0] + delta[0] * 8.0, bp[1] + delta[1] * 8.0)
        elif candidate in self.blocked_tiles:
            after_tile = before.pose.tile
            after_pixel = before.pose.pixel
        else:
            after_tile = candidate
            after_pixel = (float(after_tile[0] * 64), float(after_tile[1] * 64))

        if after_tile_override is not None:
            after_tile = after_tile_override
            after_pixel = (float(after_tile[0] * 64), float(after_tile[1] * 64))
        after = _sample(
            after_tile,
            seq=before.accepted_seq + after_seq_offset,
            at=released_at + after_since_release,
            pixel=after_pixel,
            epoch=after_epoch,
            location_key=after_location_key,
            raw_location=raw_location,
            player=after_player,
            screen=after_screen,
        )
        self.current = after
        return controller.consume_feedback(
            proposal_id=proposal.attempt_id,
            admitted=admitted,
            released=released,
            after=after,
            current_target=target,
            current_monotonic=before.pose.received_at + now_offset,
            current_session_id=SESSION,
            current_area_id=AREA,
            permission_granted=True,
            cleanup_status=cleanup,
        )


def _next(controller, executor, target, *, remaining=32, cancelled=False, now=None):
    if now is None:
        now = executor.current.pose.received_at + 0.05
    return controller.next_step(
        current_sample=executor.current,
        current_target=target,
        current_monotonic=now,
        current_session_id=SESSION,
        current_area_id=AREA,
        permission_granted=True,
        remaining_parent_budget=remaining,
        cancelled=cancelled,
    )


def _run(controller, executor, target, *, remaining=32, max_cycles=80):
    last = None
    for _ in range(max_cycles):
        step = _next(controller, executor, target, remaining=remaining)
        if step.terminal:
            return step
        if step.status == REPLAN_REQUIRED:
            last = step
            continue
        assert step.status == PROPOSED, step
        before_distance = step.distance_to_reach
        feedback = executor.execute(controller, step.proposal, target)
        executor.distance_log.append((before_distance, feedback.distance_to_reach, step.proposal))
        last = feedback
        if feedback.terminal:
            return feedback
        assert feedback.status in {PROGRESS, NO_PROGRESS, REPLAN_REQUIRED}
    raise AssertionError(f"controller did not terminate; last={last}")


def test_open_geometry_reaches_fresh_target_from_observed_feedback():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    result = _run(controller, executor, target)
    assert result.status == ARRIVED
    assert result.current_tile == (3, 1)
    assert result.proposals_used == 2
    assert result.progress_events == 2
    assert all(p.evidence_kind == "CANDIDATE_PROBE" for p in executor.proposals)


def test_adjacent_interaction_arrives_at_explicit_observed_pose_not_occupied_tile():
    target = _target(
        (3, 1),
        reach_kind=ADJACENT_INTERACTION,
        reach_tiles=((2, 1), (3, 0), (3, 2), (4, 1)),
    )
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    result = _run(controller, executor, target)
    assert result.status == ARRIVED
    assert result.current_tile == (2, 1)
    assert result.current_tile != target.target_tile


def test_interaction_contract_rejects_target_tile_as_adjacent_arrival():
    with pytest.raises(ValueError, match="occupied target tile"):
        _target((3, 1), reach_kind=ADJACENT_INTERACTION, reach_tiles=((3, 1),))


def test_obstacle_detour_temporarily_increases_distance_then_arrives():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, blocked_tiles={(2, 1)})
    result = _run(controller, executor, target)
    assert result.status == ARRIVED
    assert result.replans_used == 1
    assert any(
        before is not None and after is not None and after > before
        for before, after, _proposal in executor.distance_log
    ), executor.distance_log
    assert any(p.replan_index == 1 for p in executor.proposals)


def test_pixel_only_motion_is_progress_but_never_a_directed_edge():
    target = _target((2, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, pixel_only_attempts={"ep-211-approach-01"})
    first = _next(controller, executor, target)
    assert first.status == PROPOSED
    feedback = executor.execute(controller, first.proposal, target)
    assert feedback.status == PROGRESS
    assert feedback.current_tile == (1, 1)
    assert controller.witnessed_edges == frozenset()
    assert feedback.evidence[-1].observation_status == "SUBTILE_PROGRESS"
    second = _next(controller, executor, target)
    assert second.status == PROPOSED
    assert second.proposal.direction == first.proposal.direction


def test_repeated_no_motion_gets_one_replan_then_blocks_without_collision_claim():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, blocked_tiles={(2, 1), (1, 0), (1, 2)})
    result = _run(controller, executor, target)
    assert result.status == BLOCKED
    assert result.replans_used == 1
    assert result.blocker == "NO_PROGRESS_THRESHOLD"
    assert "unknown" in result.reason
    assert controller.witnessed_edges == frozenset()


def test_same_pose_heartbeat_does_not_erase_no_progress_history():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, blocked_tiles={(2, 1)})
    first = _next(controller, executor, target)
    one = executor.execute(controller, first.proposal, target)
    assert one.status == NO_PROGRESS
    heartbeat = _sample(
        (1, 1),
        seq=executor.current.accepted_seq + 1,
        at=executor.current.pose.received_at + 0.1,
        pixel=executor.current.pose.pixel,
    )
    executor.sync_external(heartbeat)
    second = _next(controller, executor, target)
    assert second.status == PROPOSED
    two = executor.execute(controller, second.proposal, target)
    assert two.status == REPLAN_REQUIRED
    assert two.blocker == "NO_PROGRESS_THRESHOLD"


def test_observed_a_b_a_b_oscillation_arms_only_replan_and_no_passive_edge():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    first = _next(controller, executor, target)
    feedback = executor.execute(controller, first.proposal, target)
    assert feedback.status == PROGRESS
    assert controller.witnessed_edges == frozenset({((1, 1), (2, 1))})
    external_back = _sample(
        (1, 1),
        seq=executor.current.accepted_seq + 1,
        at=executor.current.pose.received_at + 0.1,
    )
    executor.sync_external(external_back)
    second = _next(controller, executor, target)
    assert second.status == PROPOSED
    assert second.proposal.direction == "right"
    assert second.proposal.historically_witnessed is True
    oscillation = executor.execute(controller, second.proposal, target)
    assert oscillation.status == REPLAN_REQUIRED
    assert oscillation.blocker == "OSCILLATION"
    assert oscillation.replans_used == 1
    assert ((2, 1), (1, 1)) not in controller.witnessed_edges


def test_target_change_after_input_is_partial_not_arrival():
    target = _target((2, 1))
    changed = _target((2, 1), revision="r2")
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, changed)
    assert result.status == PARTIAL
    assert result.blocker == "TARGET_CHANGED"
    assert result.status != ARRIVED


def test_target_removed_after_input_stops_with_partial_evidence():
    target = _target((2, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    before = executor.current
    admitted = SampleBoundary(EPOCH, LOCATION_KEY, before.accepted_seq, 100.01)
    released = SampleBoundary(EPOCH, LOCATION_KEY, before.accepted_seq, 100.02)
    after = _sample((2, 1), seq=2, at=100.03)
    result = controller.consume_feedback(
        proposal_id=step.proposal.attempt_id,
        admitted=admitted,
        released=released,
        after=after,
        current_target=None,
        current_monotonic=100.08,
        current_session_id=SESSION,
        current_area_id=AREA,
        permission_granted=True,
        cleanup_status=CLEANUP_CONFIRMED,
    )
    assert result.status == PARTIAL
    assert result.blocker == "TARGET_REMOVED"
    assert result.status != ARRIVED


@pytest.mark.parametrize(
    ("mutator", "expected_blocker"),
    [
        (lambda kwargs: kwargs.update(after_epoch="rx-2"), "EPOCH_FENCED"),
        (lambda kwargs: kwargs.update(after_screen=1), "BINDING_FENCED"),
        (lambda kwargs: kwargs.update(after_seq_offset=2), "NOT_FIRST_AFTER_RELEASE"),
        (lambda kwargs: kwargs.update(now_offset=10.0), "STALE_SAMPLE"),
    ],
)
def test_reconnected_torn_out_of_order_or_stale_feedback_is_uncertain(mutator, expected_blocker):
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target, max_after_age=1.0)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    kwargs = {}
    mutator(kwargs)
    result = executor.execute(controller, step.proposal, target, **kwargs)
    assert result.status == UNCERTAIN
    assert result.blocker == expected_blocker
    assert result.proposal is None


def test_after_sample_in_the_release_clock_tick_is_progress():
    """Live 2026-10-07 (M1 attempt 10q): the first post-release frame carried the release's own
    timestamp (Windows clock resolution 15.6 ms); the sequence proves the order."""
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, target, after_since_release=0.0)
    assert result.status == PROGRESS, result


def test_after_sample_timed_before_release_is_still_uncertain():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, target, after_since_release=-0.005)
    assert result.status == UNCERTAIN and result.blocker == "OUT_OF_ORDER_SAMPLE", result


def test_one_tile_sideways_slide_is_progress_without_an_edge():
    """Live 2026-10-07 (M1 attempt 10z9): pressing into the chest slid the farmer one tile sideways
    around the corner. Fresh observed pose, wrong direction: progress, never an edge."""
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    assert step.proposal.direction == "right"
    result = executor.execute(controller, step.proposal, target, after_tile_override=(1, 0))
    assert result.status == PROGRESS and result.blocker == "SLIDE_DISPLACEMENT", result
    nxt = _next(controller, executor, target)
    assert nxt.status == PROPOSED, nxt


def test_multi_tile_jump_is_still_uncertain():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, target, after_tile_override=(3, 3))
    assert result.status == UNCERTAIN, result


def test_location_transition_is_partial_not_arrival_or_door_proof():
    target = _target((2, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(
        controller,
        step.proposal,
        target,
        after_location_key="BusStop#instance-1",
        raw_location="BusStop",
    )
    assert result.status == PARTIAL
    assert result.blocker == "LOCATION_CHANGED"
    assert "not arrival" in result.reason


def test_uncertain_cleanup_stops_and_never_replays_or_admits_edge():
    target = _target((2, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, target, cleanup=CLEANUP_UNCERTAIN)
    assert result.status == UNCERTAIN
    assert result.blocker == "UNCERTAIN_CLEANUP"
    assert controller.witnessed_edges == frozenset()
    later = _next(controller, executor, target)
    assert later.status == UNCERTAIN
    assert later.proposal is None
    assert later.proposals_used == 1


def test_lower_parent_budget_wins_and_never_resets_inside_approach():
    target = _target((5, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target, parent_budget=10)
    executor = FakeExecutor(initial)
    first = _next(controller, executor, target, remaining=10)
    feedback = executor.execute(controller, first.proposal, target)
    assert feedback.status == PROGRESS
    result = _next(controller, executor, target, remaining=0)
    assert result.status == PARTIAL
    assert result.blocker == "BUDGET_EXHAUSTED"
    assert result.proposals_used == 1


def test_fixed_parent_cap_is_not_reset_by_repeated_remaining_values():
    target = _target((5, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target, parent_budget=1)
    executor = FakeExecutor(initial)
    first = _next(controller, executor, target, remaining=99)
    feedback = executor.execute(controller, first.proposal, target)
    assert feedback.status == PROGRESS
    result = _next(controller, executor, target, remaining=99)
    assert result.status == PARTIAL
    assert result.blocker == "BUDGET_EXHAUSTED"
    assert result.proposals_used == 1


def test_cancellation_prevents_any_proposal():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    result = _next(controller, executor, target, cancelled=True)
    assert result.status == CANCELLED
    assert result.proposal is None
    assert result.proposals_used == 0


def test_missing_or_expired_target_is_explicitly_unavailable():
    initial = _sample((1, 1))
    missing = _controller(initial=initial, target=None)
    executor = FakeExecutor(initial)
    missing_result = missing.next_step(
        current_sample=initial,
        current_target=None,
        current_monotonic=100.1,
        current_session_id=SESSION,
        current_area_id=AREA,
        permission_granted=True,
        remaining_parent_budget=32,
    )
    assert missing_result.status == UNAVAILABLE
    assert missing_result.blocker == "MISSING_TARGET"
    expired_target = _target((3, 1), valid_until=100.0)
    expired = _controller(initial=initial, target=expired_target)
    expired_result = _next(expired, executor, expired_target, now=100.1)
    assert expired_result.status == UNAVAILABLE
    assert expired_result.blocker == "TARGET_EXPIRED"


def test_already_at_reach_uses_fresh_pose_without_any_command_lifecycle():
    target = _target((1, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    result = _next(controller, executor, target)
    assert result.status == ARRIVED
    assert result.proposals_used == 0
    assert result.evidence == ()


def test_witness_is_directed_only_and_reverse_is_never_synthesized():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial)
    first = _next(controller, executor, target)
    executor.execute(controller, first.proposal, target)
    assert ((1, 1), (2, 1)) in controller.witnessed_edges
    assert ((2, 1), (1, 1)) not in controller.witnessed_edges


def test_no_progress_does_not_infer_a_blocked_edge_or_reverse_edge():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, blocked_tiles={(2, 1)})
    step = _next(controller, executor, target)
    result = executor.execute(controller, step.proposal, target)
    assert result.status == NO_PROGRESS
    assert controller.witnessed_edges == frozenset()
    assert result.evidence[-1].directed_edge is None


def test_source_policy_defaults_to_32_and_rejects_more_than_40_proposals_and_out_of_range_ticks():
    assert ApproachPolicy(move_ticks=1).max_proposals == 32
    with pytest.raises(ValueError, match="1..40"):
        ApproachPolicy(move_ticks=1, max_proposals=41)
    with pytest.raises(ValueError, match="1..60"):
        ApproachPolicy(move_ticks=0)
    with pytest.raises(ValueError, match="1..60"):
        ApproachPolicy(move_ticks=61)


def _assert_detour_behavior_arrives():
    target = _target((3, 1))
    initial = _sample((1, 1))
    controller = _controller(initial=initial, target=target)
    executor = FakeExecutor(initial, blocked_tiles={(2, 1)})
    result = _run(controller, executor, target)
    assert result.status == ARRIVED


def test_negative_mutation_disabling_detour_is_killed_by_behavioral_oracle(monkeypatch):
    monkeypatch.setattr(
        LocalApproachController,
        "_choose_detour_direction",
        lambda self, sample, target: None,
    )
    with pytest.raises(AssertionError):
        _assert_detour_behavior_arrives()


def _next_with_sample(controller, sample, target=None, *, now=None):
    if target is None:
        target = _target()
    if now is None:
        now = max(100.0, sample.pose.received_at)
    return controller.next_step(
        current_sample=sample,
        current_target=target,
        current_monotonic=now,
        current_session_id=SESSION,
        current_area_id=AREA,
        permission_granted=True,
        remaining_parent_budget=32,
    )


def test_same_accepted_seq_changed_tile_cannot_false_arrive():
    controller = _controller()
    torn = _sample(tile=(3, 1), seq=1, at=100.0)
    result = _next_with_sample(controller, torn, now=100.0)
    assert result.status == UNCERTAIN
    assert result.blocker == "SAMPLE_IDENTITY_CONFLICT"
    assert result.status != ARRIVED
    assert result.proposal is None
    assert result.proposals_used == 0


def test_same_accepted_seq_changed_pixel_fails_closed_before_proposal():
    controller = _controller()
    torn = _sample(tile=(1, 1), seq=1, at=100.0, pixel=(72.0, 64.0))
    result = _next_with_sample(controller, torn, now=100.0)
    assert result.status == UNCERTAIN
    assert result.blocker == "SAMPLE_IDENTITY_CONFLICT"
    assert result.proposal is None
    assert result.proposals_used == 0


def test_same_accepted_seq_changed_timestamp_fails_closed_before_proposal():
    controller = _controller()
    torn = _sample(tile=(1, 1), seq=1, at=100.1)
    result = _next_with_sample(controller, torn, now=100.1)
    assert result.status == UNCERTAIN
    assert result.blocker == "SAMPLE_IDENTITY_CONFLICT"
    assert result.proposal is None


@pytest.mark.parametrize(
    "torn",
    [
        _sample(seq=1, at=100.0, raw_location="FarmHouse"),
        _sample(seq=1, at=100.0, epoch="rx-2"),
        _sample(seq=1, at=100.0, location_key="Farm#instance-2"),
        _sample(seq=1, at=100.0, player=PLAYER + 1),
        _sample(seq=1, at=100.0, screen=SCREEN + 1),
    ],
)
def test_same_accepted_seq_changed_metadata_or_binding_fails_closed(torn):
    controller = _controller()
    result = _next_with_sample(controller, torn, now=100.0)
    assert result.status == UNCERTAIN
    assert result.blocker == "SAMPLE_IDENTITY_CONFLICT"
    assert result.proposal is None


def test_identical_same_accepted_sample_reuse_remains_legal():
    controller = _controller()
    identical = _sample(tile=(1, 1), seq=1, at=100.0)
    result = _next_with_sample(controller, identical, now=100.0)
    assert result.status == PROPOSED
    assert result.proposal is not None
    assert result.proposal.direction == "right"
    assert result.proposals_used == 1


def test_initial_sample_before_episode_start_is_unavailable_before_proposal():
    initial = _sample(at=98.5)
    controller = _controller(initial=initial)
    result = _next_with_sample(controller, initial, now=100.0)
    assert result.status == UNAVAILABLE
    assert result.blocker == "INITIAL_POSE_BEFORE_EPISODE"
    assert result.proposal is None
    assert result.proposals_used == 0


@pytest.mark.parametrize(
    "initial",
    [
        _sample(at=float("nan")),
        _sample(player=True),
        _sample(tile=(1.0, 1)),
        _sample(pixel=(float("nan"), 64.0)),
    ],
)
def test_invalid_initial_pose_shape_is_unavailable_before_proposal(initial):
    controller = _controller(initial=initial)
    result = controller.next_step(
        current_sample=initial,
        current_target=_target(),
        current_monotonic=100.0,
        current_session_id=SESSION,
        current_area_id=AREA,
        permission_granted=True,
        remaining_parent_budget=32,
    )
    assert result.status == UNAVAILABLE
    assert result.blocker == "INVALID_INITIAL_POSE"
    assert result.proposal is None
    assert result.proposals_used == 0


def test_initial_binding_mismatch_is_unavailable_before_proposal():
    initial = _sample(epoch="rx-2")
    controller = _controller(initial=initial)
    result = _next_with_sample(controller, initial, now=100.0)
    assert result.status == UNAVAILABLE
    assert result.blocker == "INITIAL_BINDING_MISMATCH"
    assert result.proposal is None
    assert result.proposals_used == 0


def test_discard_unpublished_preserves_spent_budget_and_all_traversal_state(monkeypatch):
    import pickle
    target=_target((8,1));controller=_controller(target=target);executor=FakeExecutor(_sample(),blocked_tiles={(3,1)})
    for _ in range(3):
        decision=_next(controller,executor,target)
        executor.execute(controller,decision.proposal,target)
    pending=_next(controller,executor,target)
    assert pending.status==PROPOSED
    original=controller._pending
    def no_feedback(**kwargs):pytest.fail("unpublished disposal is not observation feedback")
    monkeypatch.setattr(controller._episode,"admit_observation",no_feedback)
    preserved={k:pickle.dumps(v) for k,v in vars(controller).items() if k not in {"_pending","_evidence","_episode"}}
    episode_before=dict(vars(controller._episode))
    evidence_before=tuple(controller._evidence)
    result=controller.discard_unpublished(pending.proposal.attempt_id,"prepublication_context_changed")
    assert result.status=="UNPUBLISHED_DISCARDED" and not result.terminal
    assert controller._pending is None
    assert {k:pickle.dumps(v) for k,v in vars(controller).items() if k not in {"_pending","_evidence","_episode"}}==preserved
    assert vars(controller._episode)==episode_before
    assert tuple(controller._evidence)[:-1]==evidence_before
    audit=result.evidence[-1]
    assert audit.attempt_id==original.proposal.attempt_id and audit.admission_status=="UNPUBLISHED_DISCARDED"
    assert audit.after_tile is audit.after_pixel is audit.directed_edge is audit.observation_status is None
    again=_next(controller,executor,target,remaining=32-result.proposals_used)
    assert again.status==PROPOSED and again.proposals_used==result.proposals_used+1
    assert again.proposal.attempt_id!=pending.proposal.attempt_id
    assert again.proposal.direction==pending.proposal.direction


@pytest.mark.parametrize("fault",["wrong_id","no_pending","terminal","wrong_reason"])
def test_discard_unpublished_rejects_without_clearing_or_audit(fault):
    target=_target((8,1));controller=_controller(target=target);executor=FakeExecutor(_sample())
    if fault=="terminal":
        assert _next(controller,executor,target,cancelled=True).terminal
        proposal_id="missing"
    elif fault=="no_pending":proposal_id="missing"
    else:proposal_id=_next(controller,executor,target).proposal.attempt_id
    pending=controller._pending;evidence=tuple(controller._evidence);spent=controller._proposals_used
    result=controller.discard_unpublished("wrong" if fault=="wrong_id" else proposal_id,
        "unknown" if fault=="wrong_reason" else "prepublication_context_changed")
    assert result is None and controller._pending is pending
    assert tuple(controller._evidence)==evidence and controller._proposals_used==spent
