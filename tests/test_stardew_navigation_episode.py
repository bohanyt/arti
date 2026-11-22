import ast
import inspect

import pytest

import arti_stardew_navigation_episode as episode_module
from arti_stardew_navigation_episode import (
    BINDING_FENCED,
    CAPACITY_EXHAUSTED,
    CLASSIFIER_NO_EDGE,
    DUPLICATE_OBSERVATION,
    EDGE_ADMITTED,
    EPISODE_NOT_ACTIVE,
    EPOCH_FENCED,
    GAP_EXCEEDED,
    LOCATION_FENCED,
    NOT_FIRST_AFTER_RELEASE,
    OUT_OF_ORDER_SAMPLE,
    STALE_SAMPLE,
    SUPERSEDED,
    EpisodeBinding,
    EpisodePolicy,
    EpisodeSample,
    NavigationEpisode,
    SampleBoundary,
)
from arti_stardew_navigation_observed import PoseReadback
from arti_stardew_route import FOUND, UNREACHABLE


def _episode(
    *,
    episode_id: str = "ep-1",
    epoch: str = "rx-1",
    location_key: str = "Farm#instance-1",
    player_id: int = 77,
    screen_id: int = 0,
    started_at: float = 100.0,
    max_gap: float = 1.0,
    max_age: float = 0.5,
    max_episode_age: float = 10.0,
    max_attempts: int = 16,
    max_edges: int = 16,
) -> NavigationEpisode:
    return NavigationEpisode(
        EpisodeBinding(
            episode_id=episode_id,
            receiver_epoch_id=epoch,
            location_key=location_key,
            player_id=player_id,
            screen_id=screen_id,
            started_at=started_at,
        ),
        EpisodePolicy(
            max_before_after_gap=max_gap,
            max_after_age=max_age,
            max_episode_age=max_episode_age,
            max_attempts=max_attempts,
            max_edges=max_edges,
        ),
    )


def _sample(
    tile: tuple[int, int],
    *,
    seq: int,
    at: float,
    epoch: str = "rx-1",
    location_key: str = "Farm#instance-1",
    raw_location: str = "Farm",
    player_id: int = 77,
    screen_id: int = 0,
    pixel: tuple[float, float] | None = None,
) -> EpisodeSample:
    return EpisodeSample(
        pose=PoseReadback(
            player_id=player_id,
            screen_id=screen_id,
            received_at=at,
            location=raw_location,
            tile=tile,
            pixel=pixel,
        ),
        receiver_epoch_id=epoch,
        location_key=location_key,
        accepted_seq=seq,
    )


def _boundary(
    seq: int,
    *,
    at: float,
    epoch: str = "rx-1",
    location_key: str = "Farm#instance-1",
) -> SampleBoundary:
    return SampleBoundary(
        receiver_epoch_id=epoch,
        location_key=location_key,
        last_accepted_sample_seq=seq,
        at=at,
    )


def _admit(
    episode: NavigationEpisode,
    *,
    attempt_id: str = "attempt-1",
    direction: str = "right",
    before_tile: tuple[int, int] = (10, 10),
    after_tile: tuple[int, int] = (11, 10),
    before_seq: int = 1,
    admitted_seq: int = 1,
    release_seq: int = 2,
    after_seq: int = 3,
    before_at: float = 100.0,
    admitted_at: float = 100.1,
    release_at: float = 100.3,
    after_at: float = 100.4,
    now: float = 100.45,
    before_epoch: str = "rx-1",
    admitted_epoch: str = "rx-1",
    release_epoch: str = "rx-1",
    after_epoch: str = "rx-1",
    before_location_key: str = "Farm#instance-1",
    boundary_location_key: str = "Farm#instance-1",
    after_location_key: str = "Farm#instance-1",
    before_raw_location: str = "Farm",
    after_raw_location: str = "Farm",
    before_player: int = 77,
    after_player: int = 77,
    before_screen: int = 0,
    after_screen: int = 0,
):
    return episode.admit_observation(
        attempt_id=attempt_id,
        direction=direction,
        before=_sample(
            before_tile,
            seq=before_seq,
            at=before_at,
            epoch=before_epoch,
            location_key=before_location_key,
            raw_location=before_raw_location,
            player_id=before_player,
            screen_id=before_screen,
        ),
        admitted=_boundary(
            admitted_seq,
            at=admitted_at,
            epoch=admitted_epoch,
            location_key=boundary_location_key,
        ),
        released=_boundary(
            release_seq,
            at=release_at,
            epoch=release_epoch,
            location_key=boundary_location_key,
        ),
        after=_sample(
            after_tile,
            seq=after_seq,
            at=after_at,
            epoch=after_epoch,
            location_key=after_location_key,
            raw_location=after_raw_location,
            player_id=after_player,
            screen_id=after_screen,
        ),
        current_monotonic=now,
    )


def test_fresh_same_epoch_one_tile_witness_is_admitted_directed_only() -> None:
    episode = _episode()

    result = _admit(episode)

    assert result.status == EDGE_ADMITTED
    assert result.new_edge is True
    assert result.edge == ((10, 10), (11, 10))
    assert episode.edges == frozenset({((10, 10), (11, 10))})
    assert ((11, 10), (10, 10)) not in episode.edges


def test_duplicate_witness_does_not_create_new_evidence() -> None:
    episode = _episode()
    first = _admit(episode, attempt_id="a")
    duplicate = _admit(episode, attempt_id="b")

    assert first.status == EDGE_ADMITTED
    assert duplicate.status == DUPLICATE_OBSERVATION
    assert duplicate.new_edge is False
    assert episode.attempt_count == 1
    assert episode.edge_count == 1


def test_before_after_gap_over_caller_bound_is_rejected() -> None:
    episode = _episode(max_gap=0.2)

    result = _admit(episode)

    assert result.status == GAP_EXCEEDED
    assert episode.edges == frozenset()


def test_after_must_still_be_fresh_at_caller_current_monotonic() -> None:
    episode = _episode(max_age=0.1)

    result = _admit(episode, now=100.6)

    assert result.status == STALE_SAMPLE
    assert episode.edges == frozenset()


def test_receiver_epoch_change_fences_episode_and_adds_no_edge() -> None:
    episode = _episode()

    result = _admit(episode, after_epoch="rx-2")

    assert result.status == EPOCH_FENCED
    assert episode.state == SUPERSEDED
    assert episode.edges == frozenset()
    assert _admit(episode, attempt_id="later").status == EPISODE_NOT_ACTIVE


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"after_player": 78}, BINDING_FENCED),
        ({"after_screen": 1}, BINDING_FENCED),
        ({"before_location_key": "Farm#other"}, LOCATION_FENCED),
    ],
)
def test_actor_screen_or_source_location_key_mismatch_fails_closed(
    kwargs: dict[str, object],
    expected: str,
) -> None:
    episode = _episode()

    result = _admit(episode, **kwargs)

    assert result.status == expected
    assert episode.edges == frozenset()


def test_location_change_fences_local_episode_without_edge_or_causal_claim() -> None:
    episode = _episode()

    result = _admit(
        episode,
        after_tile=(0, 12),
        after_location_key="BusStop#instance-1",
        after_raw_location="BusStop",
    )

    assert result.status == LOCATION_FENCED
    assert result.edge is None
    assert result.location_transition is not None
    assert result.location_transition.before_location_key == "Farm#instance-1"
    assert result.location_transition.after_location_key == "BusStop#instance-1"
    assert "door/warp claim" in result.reason
    assert episode.edges == frozenset()
    assert _admit(episode, attempt_id="later").status == EPISODE_NOT_ACTIVE


def test_expired_or_superseded_episode_rejects_later_evidence() -> None:
    expired = _episode(max_episode_age=0.2)
    expired_result = _admit(expired, now=100.3)
    assert expired_result.status == EPISODE_NOT_ACTIVE
    assert expired.edges == frozenset()

    superseded = _episode(episode_id="ep-superseded")
    superseded.supersede()
    assert _admit(superseded).status == EPISODE_NOT_ACTIVE
    assert superseded.edges == frozenset()


def test_first_after_release_and_monotonic_boundaries_are_required() -> None:
    not_first = _episode()
    result = _admit(not_first, after_seq=4)
    assert result.status == NOT_FIRST_AFTER_RELEASE
    assert not_first.edges == frozenset()

    out_of_order = _episode(episode_id="ep-order")
    result = _admit(out_of_order, before_seq=0, admitted_seq=1)
    assert result.status == OUT_OF_ORDER_SAMPLE
    assert out_of_order.edges == frozenset()


def test_attempt_capacity_fails_closed_without_eviction() -> None:
    episode = _episode(max_attempts=1, max_edges=2)

    first = _admit(
        episode,
        after_tile=(10, 10),
        attempt_id="no-progress",
    )
    second = _admit(
        episode,
        attempt_id="would-be-edge",
        before_tile=(10, 10),
        after_tile=(11, 10),
        before_seq=4,
        admitted_seq=4,
        release_seq=5,
        after_seq=6,
        before_at=100.5,
        admitted_at=100.55,
        release_at=100.6,
        after_at=100.7,
        now=100.75,
    )

    assert first.status == CLASSIFIER_NO_EDGE
    assert second.status == CAPACITY_EXHAUSTED
    assert episode.attempt_count == 1
    assert episode.edges == frozenset()


def test_edge_capacity_fails_closed_and_keeps_old_evidence() -> None:
    episode = _episode(max_attempts=3, max_edges=1)
    first = _admit(episode, attempt_id="edge-1")
    second = _admit(
        episode,
        attempt_id="edge-2",
        before_tile=(11, 10),
        after_tile=(12, 10),
        before_seq=4,
        admitted_seq=4,
        release_seq=5,
        after_seq=6,
        before_at=100.5,
        admitted_at=100.55,
        release_at=100.6,
        after_at=100.7,
        now=100.75,
    )

    assert first.status == EDGE_ADMITTED
    assert second.status == CAPACITY_EXHAUSTED
    assert episode.edges == frozenset({((10, 10), (11, 10))})
    assert episode.edge_count == 1


def test_route_kernel_sees_only_current_episode_witnessed_directed_edges() -> None:
    episode = _episode()
    _admit(episode, attempt_id="right-1")
    _admit(
        episode,
        attempt_id="right-2",
        before_tile=(11, 10),
        after_tile=(12, 10),
        before_seq=4,
        admitted_seq=4,
        release_seq=5,
        after_seq=6,
        before_at=100.5,
        admitted_at=100.55,
        release_at=100.6,
        after_at=100.7,
        now=100.75,
    )

    forward = episode.query_route(
        start=(10, 10),
        goals={(12, 10)},
        max_path_length=2,
        current_monotonic=100.8,
    )
    reverse = episode.query_route(
        start=(12, 10),
        goals={(10, 10)},
        max_path_length=2,
        current_monotonic=100.85,
    )

    assert forward.status == FOUND
    assert forward.route is not None
    assert forward.route.steps == ("right", "right")
    assert reverse.status == UNREACHABLE
    assert reverse.evidence_limited is True
    assert "not collision truth" in reverse.reason


def test_edges_never_leak_between_episodes() -> None:
    old = _episode(episode_id="old")
    _admit(old)

    current = _episode(episode_id="current")
    _admit(
        current,
        attempt_id="left",
        direction="left",
        before_tile=(11, 10),
        after_tile=(10, 10),
    )

    assert old.edges == frozenset({((10, 10), (11, 10))})
    assert current.edges == frozenset({((11, 10), (10, 10))})


def test_module_imports_no_command_provider_network_game_or_native_layer() -> None:
    tree = ast.parse(inspect.getsource(episode_module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    banned_prefixes = (
        "arti_stardew_command",
        "arti_stardew_runtime",
        "arti_bridge",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "subprocess",
        "stardew-mod",
    )
    assert not any(
        name == prefix or name.startswith(prefix + ".")
        for name in imported
        for prefix in banned_prefixes
    )
