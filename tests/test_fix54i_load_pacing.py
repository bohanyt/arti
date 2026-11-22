"""fix54I: leased moves and evidence bounds under machine load (a39 clip-ops windows, median CPU 90%).

Shapes come from a39 (free-play-run-example, RESEARCH_A39_UNANALYSED_2026-11-03 §4):
- D0016:J805 `lease_expired:LeaseExpired`: ACQUIRE written, request admitted after the fixed 1500 ms lease ended.
- FH `background N consumed h=52/58 release_reason=DEADLINE`: the 1.0 s request deadline cut normal holds.
- D0008:J1043 `GAP_EXCEEDED` after `PressCompleted` (25 ticks, 42,31 -> 42,33 reached): the fixed 3 s
  before->after bound counted the lease-owned command time that load had stretched.
- D0003/D0009/D0012 `runtime_not_ready:READY` after `no_progress_reobserve` on the same run.
"""

import json
from types import SimpleNamespace

import pytest

import arti_stardew_command as command
from arti_runtime.move_pacing import MAX_COMMAND_EXTENSION_S, MAX_LEASE_MS, MovePacing
from arti_stardew_live7 import NOT_READY, classify
from arti_stardew_live7_recovery import Live7RecoveryMixin
from arti_stardew_navigation_episode import (
    EDGE_ADMITTED,
    GAP_EXCEEDED,
    EpisodeBinding,
    EpisodePolicy,
    EpisodeSample,
    NavigationEpisode,
    SampleBoundary,
)
from arti_stardew_navigation_observed import PoseReadback


def test_unloaded_pacing_keeps_the_old_constants():
    pacing = MovePacing()
    assert pacing.lease_ms(60) == 1500
    assert pacing.admission_s(1500, 1.4) == 1.0
    assert pacing.result_wait_s(1.0) == 2.0
    for _ in range(8):  # quiet machine: ~60 ms hand-off, ~0.3 s press overhead
        pacing.observe_ack(0.06)
        pacing.observe_press(10, 0.3 + 10 / 60)
    assert pacing.lease_ms(10) == 1500
    assert pacing.admission_s(pacing.lease_ms(10), 1.44) == 1.0


def test_a39_load_sizes_a_lease_that_covers_the_handoff_and_hold():
    pacing = MovePacing()
    pacing.observe_ack(1.1)  # D0016:J805: admission landed > 1.5 s after ACQUIRE
    pacing.observe_press(25, 2.4)  # D0008:J1043-shape lease-owned interval under clip-ops ffmpeg
    lease = pacing.lease_ms(58)
    assert 1100 + 1600 + 58 * 1000 // 60 <= lease <= MAX_LEASE_MS
    admission = pacing.admission_s(lease, (lease - 1100) / 1000)
    assert admission > 1.0  # the hold is no longer cut by the 1.0 s deadline (h=52/58 DEADLINE)
    assert admission <= (lease - 1100) / 1000  # never past the live lease
    assert pacing.result_wait_s(admission) >= admission + 1.0


def test_pacing_is_bounded_and_rejects_garbage():
    pacing = MovePacing()
    for value in (float("nan"), float("inf"), -3, True, "2"):
        pacing.observe_ack(value)
        pacing.observe_press(10, value)
    pacing.observe_press(0, 1.0)
    assert pacing.lease_ms() == 1500
    pacing.observe_ack(60.0)  # a frozen machine never earns more than the native bound
    assert pacing.lease_ms() == MAX_LEASE_MS
    assert pacing.admission_s(10_000, 99.0) == MAX_LEASE_MS / 1000 - 0.5
    for _ in range(8):  # the window forgets an old outlier once load ends
        pacing.observe_ack(0.05)
    assert pacing.lease_ms(10) == 1500


def _session():
    return SimpleNamespace(mentah={"runtime_instance_id": "rt-a39"}, session_id="sess-a39", player_id=1100000000000000003,
                           player_name="arti", screen_id=0)


def test_control_carries_only_a_bounded_sized_lease(tmp_path):
    common = dict(folder=tmp_path, sesi=_session(), owner_id="g1-owner-x", generation=7, control_seq=1)
    command.tulis_kontrol(command.KIND_ACQUIRE, **common)
    assert "lease_ms" not in json.loads((tmp_path / command.NAMA_CONTROL).read_text(encoding="utf-8"))
    command.tulis_kontrol(command.KIND_ACQUIRE, lease_ms=2900, **common)
    assert json.loads((tmp_path / command.NAMA_CONTROL).read_text(encoding="utf-8"))["lease_ms"] == 2900
    for bad in (1499, 4001, 2000.0, True):
        with pytest.raises(command.KesalahanPerintah):
            command.tulis_kontrol(command.KIND_RENEW, lease_ms=bad, **common)
    with pytest.raises(command.KesalahanPerintah):
        command.tulis_kontrol(command.KIND_RELEASE, lease_ms=2000, **common)


def _gap_case(extension: float, *, released_at: float = 103.45):
    episode = NavigationEpisode(EpisodeBinding("goal-1459a0359a20", "rx-a39", "Farm#1", 77, 0, 100.0),
                                EpisodePolicy(3, 2, 300, max_command_extension=extension))

    def sample(tile, seq, at):
        return EpisodeSample(PoseReadback(77, 0, at, "Farm", tile, None), "rx-a39", "Farm#1", seq)

    # 42,31 -> 42,32 down; before is the latest sample at admission, after the first one after release.
    return episode.admit_observation(
        attempt_id="j1-332485ddc031", direction="down", before=sample((42, 31), 4418, 100.2),
        admitted=SampleBoundary("rx-a39", "Farm#1", 4418, 100.25),
        released=SampleBoundary("rx-a39", "Farm#1", 4435, released_at),
        after=sample((42, 32), 4436, released_at + 0.15), current_monotonic=released_at + 0.2)


def test_a39_gap_exceeded_shape_is_admitted_from_the_measured_command_interval():
    assert _gap_case(0.0).status == GAP_EXCEEDED  # base: fixed 3 s bound
    assert _gap_case(MAX_COMMAND_EXTENSION_S).status == EDGE_ADMITTED


def test_gap_extension_stays_bounded():
    # A command interval longer than the bound still cannot launder an arbitrarily old before sample.
    assert _gap_case(MAX_COMMAND_EXTENSION_S, released_at=100.25 + MAX_COMMAND_EXTENSION_S + 3.5).status == GAP_EXCEEDED


class _Runtime(Live7RecoveryMixin):
    pass


def test_leg_without_reason_names_the_preceding_recovery():
    runtime = _Runtime()
    session = SimpleNamespace(session_id="skills-1", runs=[object(), object()])
    assert runtime._live7_leg_end_reason(session, "READY") == "leg_ended_no_reason"
    runtime.__dict__["_live7_last_recovery_cause"] = ("skills-1", 2, "no_progress_reobserve")
    assert runtime._live7_leg_end_reason(session, "READY") == "recovered_after:no_progress_reobserve"
    session.runs.append(object())  # a later run never inherits an older cause
    assert runtime._live7_leg_end_reason(session, "READY") == "leg_ended_no_reason"
    assert runtime._live7_leg_end_reason(session, "PAUSED_RECOVERY") == "runtime_not_ready:PAUSED_RECOVERY"


@pytest.mark.parametrize("reason", ["recovered_after:no_progress_reobserve", "leg_ended_no_reason"])
def test_renamed_leg_end_keeps_its_not_ready_class(reason):
    clean = {"input_released": True, "menu": None, "free": True}
    assert classify(("fail", reason), clean, None, status="READY") == NOT_READY
    from scripts.live7.live7_common import classify as table_class
    assert table_class(reason, False) == "NOT_READY"
