from arti_stardew_navigation_observed import (
    AMBIGUOUS_DISPLACEMENT,
    CONFIRMED_LOCAL_STEP,
    IDENTITY_CHANGED,
    INVALID_INPUT,
    LOCATION_CHANGED,
    NO_TILE_PROGRESS,
    STALE_READBACK,
    SUBTILE_PROGRESS,
    PoseReadback,
    observe_cardinal_traversal,
)
from arti_stardew_route import FOUND, UNREACHABLE, cari_rute_kardinal


def _pose(
    tile: tuple[int, int],
    *,
    received_at: float,
    location: str = "Farm",
    player_id: int = 77,
    screen_id: int = 0,
    pixel: tuple[float, float] | None = None,
) -> PoseReadback:
    return PoseReadback(
        player_id=player_id,
        screen_id=screen_id,
        received_at=received_at,
        location=location,
        tile=tile,
        pixel=pixel,
    )


def test_exact_requested_cardinal_delta_yields_one_directed_witness() -> None:
    before = _pose((10, 10), received_at=100.0, pixel=(640.0, 640.0))
    after = _pose((11, 10), received_at=100.2, pixel=(704.0, 640.0))

    result = observe_cardinal_traversal(before, after, "right")

    assert result.status == CONFIRMED_LOCAL_STEP
    assert result.edge == ((10, 10), (11, 10))
    assert result.local_step is not None
    assert result.local_step.direction == "right"
    assert result.location_change is None


def test_witness_does_not_infer_reverse_edge() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0),
        _pose((11, 10), received_at=2.0),
        "right",
    )

    assert result.edge == ((10, 10), (11, 10))
    assert result.edge != ((11, 10), (10, 10))


def test_same_tile_with_pixel_motion_is_subtile_progress_without_edge() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0, pixel=(640.0, 640.0)),
        _pose((10, 10), received_at=2.0, pixel=(656.0, 640.0)),
        "right",
    )

    assert result.status == SUBTILE_PROGRESS
    assert result.edge is None


def test_same_tile_without_observed_pixel_motion_is_no_tile_progress() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0, pixel=(640.0, 640.0)),
        _pose((10, 10), received_at=2.0, pixel=(640.0, 640.0)),
        "right",
    )

    assert result.status == NO_TILE_PROGRESS
    assert result.edge is None


def test_wrong_direction_is_ambiguous_and_never_becomes_edge() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0),
        _pose((10, 11), received_at=2.0),
        "right",
    )

    assert result.status == AMBIGUOUS_DISPLACEMENT
    assert result.edge is None


def test_multi_tile_displacement_is_ambiguous_and_never_becomes_edge() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0),
        _pose((12, 10), received_at=2.0),
        "right",
    )

    assert result.status == AMBIGUOUS_DISPLACEMENT
    assert result.edge is None


def test_equal_or_older_receipt_time_is_stale_readback() -> None:
    equal = observe_cardinal_traversal(
        _pose((10, 10), received_at=2.0),
        _pose((11, 10), received_at=2.0),
        "right",
    )
    older = observe_cardinal_traversal(
        _pose((10, 10), received_at=2.0),
        _pose((11, 10), received_at=1.9),
        "right",
    )

    assert equal.status == STALE_READBACK
    assert equal.edge is None
    assert older.status == STALE_READBACK
    assert older.edge is None


def test_actor_or_screen_identity_change_never_becomes_edge() -> None:
    actor_changed = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0, player_id=77),
        _pose((11, 10), received_at=2.0, player_id=78),
        "right",
    )
    screen_changed = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0, screen_id=0),
        _pose((11, 10), received_at=2.0, screen_id=1),
        "right",
    )

    assert actor_changed.status == IDENTITY_CHANGED
    assert actor_changed.edge is None
    assert screen_changed.status == IDENTITY_CHANGED
    assert screen_changed.edge is None


def test_location_change_is_observed_transition_not_local_edge_or_warp_claim() -> None:
    before = _pose((42, 5), received_at=1.0, location="Farm")
    after = _pose((0, 12), received_at=2.0, location="BusStop")

    result = observe_cardinal_traversal(before, after, "right")

    assert result.status == LOCATION_CHANGED
    assert result.edge is None
    assert result.local_step is None
    assert result.location_change is not None
    assert result.location_change.before == before
    assert result.location_change.after == after
    assert result.location_change.attempted_direction == "right"


def test_invalid_direction_fails_closed() -> None:
    result = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0),
        _pose((11, 10), received_at=2.0),
        "upright",
    )

    assert result.status == INVALID_INPUT
    assert result.edge is None


def test_two_witnessed_edges_feed_route_kernel_without_reverse_inference() -> None:
    first = observe_cardinal_traversal(
        _pose((10, 10), received_at=1.0),
        _pose((11, 10), received_at=2.0),
        "right",
    )
    second = observe_cardinal_traversal(
        _pose((11, 10), received_at=3.0),
        _pose((12, 10), received_at=4.0),
        "right",
    )
    edges = {edge for edge in (first.edge, second.edge) if edge is not None}
    nodes = {(10, 10), (11, 10), (12, 10)}

    forward = cari_rute_kardinal(nodes, edges, (10, 10), {(12, 10)}, 2)
    reverse = cari_rute_kardinal(nodes, edges, (12, 10), {(10, 10)}, 2)

    assert forward.status == FOUND
    assert forward.steps == ("right", "right")
    assert reverse.status == UNREACHABLE
    assert reverse.found is False
