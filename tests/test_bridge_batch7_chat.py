"""YouTube recovery uses fake HTTP and a bounded fake sleep, never YouTube."""
import json
from types import SimpleNamespace

import pytest

import arti_bridge as bridge


TOKEN = "synthetic-continuation-token-0001"


class EndWorker(BaseException):
    pass


def run_worker(monkeypatch, *, polls=(), pages=None, previous="", limit=12):
    calls = SimpleNamespace(get=0, post=0, sleeps=[])
    pages = pages or [{"contents": {"liveChatRenderer": {"continuation": TOKEN}}}]
    polls = list(polls)

    class Session:
        headers = {}
        cookies = SimpleNamespace(set=lambda *a, **k: None)

        def get(self, *a, **k):
            calls.get += 1
            data = pages[min(calls.get - 1, len(pages) - 1)]
            return SimpleNamespace(raise_for_status=lambda: None,
                                   text="var ytInitialData = " + json.dumps(data) + ";")

        def post(self, *a, **k):
            calls.post += 1
            result = polls[min(calls.post - 1, len(polls) - 1)] if polls else {}
            if isinstance(result, Exception):
                raise result
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    def sleep(seconds):
        calls.sleeps.append(seconds)
        if len(calls.sleeps) >= limit:
            raise EndWorker

    monkeypatch.setitem(bridge.CONFIG, "youtube_chat_enabled", True)
    monkeypatch.setitem(bridge.CONFIG, "youtube_video_id", "abc123DEF_-")
    monkeypatch.setitem(bridge.CONFIG, "youtube_chat_reinit_empty_polls", 3)
    monkeypatch.setattr(bridge, "_previous_youtube_video_id", previous, raising=False)
    monkeypatch.setattr(bridge, "_yt_chat_running", True)
    monkeypatch.setattr(bridge.requests, "Session", Session)
    monkeypatch.setattr(bridge.time, "sleep", sleep)
    with pytest.raises(EndWorker):
        bridge.youtube_chat_worker()
    return calls


def test_missing_continuation_reinitializes_with_capped_backoff(monkeypatch):
    calls = run_worker(monkeypatch)
    assert calls.get >= 3
    assert calls.post == calls.get
    assert calls.sleeps[:3] == [3, 6, 12]
    assert max(calls.sleeps) <= 60


@pytest.mark.parametrize("result", [
    RuntimeError("synthetic HTTP error"),
    {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": TOKEN, "timeoutMs": 3000}}],
        "actions": [],
    }}},
])
def test_errors_and_empty_stale_tokens_reinitialize(monkeypatch, result):
    calls = run_worker(monkeypatch, polls=[result])
    assert calls.get >= 2
    assert calls.post >= calls.get


def test_quiet_chat_with_valid_token_refreshes_without_backoff(monkeypatch, capsys):
    # 2026-10-24 morning test live: 148x "stale" re-inits with a valid token;
    # the 3->60 s backoff made quiet-chat replies up to a minute late.
    empty = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": TOKEN, "timeoutMs": 3000}}],
        "actions": [],
    }}}
    calls = run_worker(monkeypatch, polls=[empty])
    assert calls.get >= 2 and calls.post >= calls.get
    assert set(calls.sleeps) == {3}
    output = capsys.readouterr().out
    assert "refresh token" in output
    assert "Continuation kosong/stale" not in output


def test_startup_reused_session_id_is_loud_fail_without_wizard(monkeypatch, capsys):
    run_worker(monkeypatch, previous="abc123DEF_-", limit=2)
    output = capsys.readouterr().out
    assert "[FAIL]" in output and "sesi sebelumnya" in output


@pytest.mark.parametrize("page", [{}, {"other": {"continuation": TOKEN}}, {"contents": {"liveChatRenderer": {
    "isReplay": True, "continuation": TOKEN,
}}}])
def test_startup_not_live_or_invalid_is_fail_and_keeps_retrying(monkeypatch, capsys, page):
    calls = run_worker(monkeypatch, pages=[page])
    assert calls.get == 12 and calls.post == 0
    assert max(calls.sleeps) <= 300
    assert "[FAIL]" in capsys.readouterr().out


def test_nonempty_poll_resets_empty_counter(monkeypatch):
    empty = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": TOKEN, "timeoutMs": 3000}}],
        "actions": [],
    }}}
    polls = []
    for index in range(4):
        polls.extend([empty, empty, {"continuationContents": {"liveChatContinuation": {
            "continuations": [{"timedContinuationData": {"continuation": TOKEN, "timeoutMs": 3000}}],
            # A non-text, service command exercises parse/dedup without any
            # history, viewers, LLM, or real donation callbacks.
            "actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "id": f"synthetic-{index}", "authorName": {"simpleText": "synthetic"},
                "message": {"runs": [{"text": "!synthetic"}]},
            }}}}],
        }}}])
    calls = run_worker(monkeypatch, polls=polls)
    assert calls.get == 1 and calls.post == 12


def test_previous_session_id_is_captured_before_wizard_save(monkeypatch, tmp_path):
    path = tmp_path / "live_session.json"
    path.write_text(json.dumps({"youtube_video_id": "previous-ID"}), encoding="utf-8")
    monkeypatch.setattr(bridge, "LIVE_SESSION_PATH", str(path))
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG))
    monkeypatch.setattr(bridge, "_previous_youtube_video_id", "", raising=False)
    monkeypatch.setattr(bridge, "resolve_asr_input_device", lambda: (None, "fake mic"))
    assert bridge.load_live_session()
    bridge.CONFIG["youtube_video_id"] = "new-video-ID"
    bridge.save_live_session()
    assert bridge._previous_youtube_video_id == "previous-ID"
    assert json.loads(path.read_text(encoding="utf-8"))["youtube_video_id"] == "new-video-ID"
