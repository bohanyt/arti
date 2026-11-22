"""fix55C (REVIEW2 #7): the soak preflight requires the deployed HOST helper to report GiftWhen "idle".

fix54K staged `GiftWhen="idle"` but the preflight checked only `helper_sha256` (scripts/free_play/soak.py), so an
old helper DLL (no GiftWhen setting) would keep the a39 moving-only gifts. The helper logs its parsed config in
the first `helper_started` row (ArtiHostHelper/ModEntry.cs Entry); an unknown key is dropped there. UNIT_TESTED."""
import inspect
import json

import pytest

from scripts.free_play import soak


def _host(tmp_path, *rows):
    (tmp_path / "host.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def _started(**config):
    return dict(ev="helper_started", actor="deterministic_host", run_id="run-1", sequence=1,
                facts=dict(config=dict(Enabled=True, RunId="run-1", GiftTimes=[900, 1300], **config), dll="mvid-1"))


def test_the_current_helper_reporting_idle_passes(tmp_path):
    host = _host(tmp_path, _started(GiftWhen="idle", GiftIdleSeconds=20), dict(ev="status"))
    assert soak.helper_started_preflight(host) == dict(gift_when="idle", gift_idle_seconds=20, dll="mvid-1")


def test_an_old_helper_without_gift_when_fails_closed(tmp_path):
    host = _host(tmp_path, _started())  # pre-fix54K Config: the staged GiftWhen key is dropped on read
    with pytest.raises(RuntimeError, match="helper_gift_when_not_idle"):
        soak.helper_started_preflight(host)


def test_a_moving_helper_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match=r"helper_gift_when_not_idle \(reported 'moving'\)"):
        soak.helper_started_preflight(_host(tmp_path, _started(GiftWhen="moving", GiftIdleSeconds=20)))


def test_only_the_first_helper_started_row_counts(tmp_path):
    host = _host(tmp_path, dict(ev="noise"), "torn{", _started(GiftWhen="moving"), _started(GiftWhen="idle"))
    with pytest.raises(RuntimeError, match="helper_gift_when_not_idle"):
        soak.helper_started_preflight(host)


def test_no_helper_started_row_or_no_log_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="helper_started_missing"):
        soak.helper_started_preflight(tmp_path)
    with pytest.raises(RuntimeError, match="helper_started_missing"):
        soak.helper_started_preflight(_host(tmp_path, dict(ev="status")))


def test_the_soak_checks_the_helper_after_host_load_and_before_any_farmhand():
    body = inspect.getsource(soak._run_soak)
    check = body.index("helper_started_preflight(host_out)")
    assert body.index('snapshot_exists(host_out / "status.json")') < check < body.index('["launch-farmhand"')
    assert soak.HELPER_GIFT_WHEN == "idle" and 'GiftWhen="idle"' in body  # the staged and checked value agree
