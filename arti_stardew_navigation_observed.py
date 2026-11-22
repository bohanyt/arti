"""Fail-closed traversal evidence from accepted Stardew motor poses.

This is an observation classifier, not a collision oracle or movement driver.
It preserves #132: no standability/occupancy fact, guessed crossing, reverse
edge, or command completion is treated as arrival proof.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

Tile = tuple[int, int]
Pixel = tuple[float, float]

CONFIRMED_LOCAL_STEP = "CONFIRMED_LOCAL_STEP"
LOCATION_CHANGED = "LOCATION_CHANGED"
SUBTILE_PROGRESS = "SUBTILE_PROGRESS"
NO_TILE_PROGRESS = "NO_TILE_PROGRESS"
AMBIGUOUS_DISPLACEMENT = "AMBIGUOUS_DISPLACEMENT"
STALE_READBACK = "STALE_READBACK"
IDENTITY_CHANGED = "IDENTITY_CHANGED"
INVALID_INPUT = "INVALID_INPUT"

_DIRECTION_DELTA: dict[str, Tile] = {
    "left": (-1, 0),
    "right": (1, 0),
    "up": (0, -1),
    "down": (0, 1),
}


@dataclass(frozen=True)
class PoseReadback:
    """Ephemeral pose evidence; not a second state store.

    `received_at` is the receiver-local monotonic acceptance time (currently
    `StardewState.diterima_pada`), not a sequence number that may reset on a
    reconnect.
    """

    player_id: int
    screen_id: int
    received_at: float
    location: str
    tile: Tile
    pixel: Pixel | None = None


@dataclass(frozen=True)
class LocalTraversalWitness:
    direction: str
    before: PoseReadback
    after: PoseReadback

    @property
    def edge(self) -> tuple[Tile, Tile]:
        return (self.before.tile, self.after.tile)


@dataclass(frozen=True)
class LocationTransitionObservation:
    attempted_direction: str
    before: PoseReadback
    after: PoseReadback


@dataclass(frozen=True)
class TraversalObservation:
    status: str
    reason: str
    local_step: LocalTraversalWitness | None = None
    location_change: LocationTransitionObservation | None = None

    @property
    def edge(self) -> tuple[Tile, Tile] | None:
        return self.local_step.edge if self.local_step is not None else None


def observe_cardinal_traversal(
    before: PoseReadback,
    after: PoseReadback,
    direction: str,
) -> TraversalObservation:
    """Classify one bounded movement attempt from independent pose readback.

    Callers must obtain `after` from a fresh accepted motor state after input
    release.  Lifecycle completion may bound the attempt, but never proves the
    physical effect classified here.
    """
    if direction not in _DIRECTION_DELTA:
        return _result(INVALID_INPUT, f"direction tidak dikenal: {direction!r}")
    for name, pose in (("before", before), ("after", after)):
        error = _pose_error(pose, name)
        if error is not None:
            return _result(INVALID_INPUT, error)

    if (after.player_id, after.screen_id) != (before.player_id, before.screen_id):
        return _result(IDENTITY_CHANGED, "player_id/screen_id berubah; tidak ada edge")
    if after.received_at <= before.received_at:
        return _result(STALE_READBACK, "after readback tidak strictly later")

    if after.location != before.location:
        return TraversalObservation(
            status=LOCATION_CHANGED,
            reason=(
                f"lokasi berubah {before.location!r}->{after.location!r}; "
                "observed transition saja, bukan local edge atau causal warp claim"
            ),
            location_change=LocationTransitionObservation(direction, before, after),
        )

    delta = (after.tile[0] - before.tile[0], after.tile[1] - before.tile[1])
    expected = _DIRECTION_DELTA[direction]
    if delta == expected:
        return TraversalObservation(
            status=CONFIRMED_LOCAL_STEP,
            reason=f"fresh cardinal displacement {before.tile}->{after.tile}",
            local_step=LocalTraversalWitness(direction, before, after),
        )
    if delta == (0, 0):
        if before.pixel is not None and after.pixel is not None and before.pixel != after.pixel:
            return _result(SUBTILE_PROGRESS, "pixel berubah tetapi tile belum berubah")
        return _result(NO_TILE_PROGRESS, "tile tidak berubah; jangan infer blocked edge")
    return _result(
        AMBIGUOUS_DISPLACEMENT,
        f"tile delta {delta} bukan tepat satu langkah {direction} {expected}",
    )


def _result(status: str, reason: str) -> TraversalObservation:
    return TraversalObservation(status=status, reason=reason)


def _pose_error(pose: object, name: str) -> str | None:
    if not isinstance(pose, PoseReadback):
        return f"{name} bukan PoseReadback"
    if type(pose.player_id) is not int or type(pose.screen_id) is not int:
        return f"{name} identity harus integer polos"
    if not _finite(pose.received_at):
        return f"{name}.received_at harus finite"
    if not isinstance(pose.location, str) or not pose.location:
        return f"{name}.location harus string non-kosong"
    if not _pair_valid(pose.tile, integer=True):
        return f"{name}.tile harus tuple dua integer polos"
    if pose.pixel is not None and not _pair_valid(pose.pixel, integer=False):
        return f"{name}.pixel harus tuple dua angka finite"
    return None


def _pair_valid(value: object, *, integer: bool) -> bool:
    if not isinstance(value, tuple) or len(value) != 2:
        return False
    if integer:
        return all(type(item) is int for item in value)
    return all(_finite(item) for item in value)


def _finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )
