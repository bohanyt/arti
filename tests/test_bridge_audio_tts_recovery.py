"""Batch 5 recovery contracts. No devices, network, or real subprocesses."""

import asyncio
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

import arti_voice_dsp as dsp
import arti_bridge as bridge


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(bridge, "CONFIG", dict(bridge.CONFIG))
    monkeypatch.setattr(bridge.sd, "query_devices", Mock(return_value=[]))
    monkeypatch.setattr(bridge.sd, "play", Mock())
    monkeypatch.setattr(bridge.sd, "wait", Mock())
    monkeypatch.setattr(bridge.subprocess, "Popen", Mock(side_effect=AssertionError("real spawn forbidden")))
    monkeypatch.setattr(bridge, "tts_is_playing", False)
    monkeypatch.setattr(bridge.subtitle_runtime, "enabled", False)


def playback(monkeypatch, tmp_path):
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"fake wav")
    monkeypatch.setattr(bridge.sf, "read", lambda _: (np.zeros(480), 48000))
    eng = bridge.TTSEngine()
    return eng, wav


@pytest.mark.parametrize("engine", ["supertone", "edge_tts"])
@pytest.mark.parametrize("edge", ["expiry", "revoked"])
def test_host_reply_expires_during_synthesis_before_playback(monkeypatch, tmp_path, engine, edge):
    import os

    from arti_runtime.host_story import HostStoryRelay
    from tests.test_host_story import presence, revoke_presence, row, write

    if edge == "revoked" and os.name != "nt":
        pytest.skip("Windows HOST event")

    now = [100.]
    relay = HostStoryRelay(tmp_path / "host_story.jsonl", clock=lambda: now[0])
    write(relay.path, row(1, "line", text="A fresh HOST letter", utc_ms=100_000), utc_ms=100_000)
    sent = []
    assert relay.tick(lambda text, **kw: sent.append(text) or True)
    [story] = sent
    eng, wav = playback(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "tts", eng)
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    bridge.CONFIG["tts_engine"] = engine

    def expire():
        if edge == "revoked":
            heartbeat = relay.presence_path.read_bytes()
            revoke_presence(relay.path)
            assert relay.presence_path.read_bytes() == heartbeat
        else:
            now[0] = 161.
            presence(relay.path, utc_ms=161_000)

    async def acquire(_text):
        expire()
        return str(wav), []

    async def stream():
        expire()
        yield {"type": "audio", "data": b"fake mp3"}

    monkeypatch.setattr(eng, "_acquire_supertone", acquire)
    monkeypatch.setattr(bridge.edge_tts, "Communicate", lambda *args: SimpleNamespace(stream=stream))
    assert story.is_current()
    result = asyncio.run(bridge._speak_reply_chunks(["I heard the letter."], bridge.screen_privacy.epoch,
                                                   host_story=(story,)))
    assert not result
    bridge.sd.play.assert_not_called()


@pytest.mark.parametrize("error", [bridge.sd.PortAudioError("Invalid device"), ValueError("Invalid device")])
def test_device_reenumerated_once_and_configured_name_retained(monkeypatch, tmp_path, error):
    devices = Mock(side_effect=[
        [{"name": "Cable", "max_output_channels": 2}],
        [{"name": "mic", "max_output_channels": 0}, {"name": "Cable", "max_output_channels": 2}],
    ])
    bridge.CONFIG["virtual_cable_name"] = "Cable"
    monkeypatch.setattr(bridge.sd, "query_devices", devices)
    eng, wav = playback(monkeypatch, tmp_path)
    bridge.CONFIG["virtual_cable_name"] = "changed after startup"
    bridge.sd.play.side_effect = [error, None, None]
    asyncio.run(eng._play_wav(str(wav), "halo", [], owns_temp=False))
    asyncio.run(eng._play_wav(str(wav), "lagi", [], owns_temp=False))
    assert [c.kwargs["device"] for c in bridge.sd.play.call_args_list] == [0, 1, 1]
    assert devices.call_count == 2
    assert not bridge.tts_is_playing


def test_missing_device_falls_back_loudly_once_and_retries_once(monkeypatch, tmp_path, capsys):
    eng, wav = playback(monkeypatch, tmp_path)
    capsys.readouterr()
    bridge.sd.play.side_effect = bridge.sd.PortAudioError("Invalid device")
    asyncio.run(eng._play_wav(str(wav), "halo", [], owns_temp=True))
    assert bridge.sd.play.call_count == 2
    assert bridge.sd.play.call_args.kwargs["device"] is None
    assert not wav.exists()
    assert not bridge.tts_is_playing
    # The startup warning already covered this missing cable.
    assert "DEFAULT OUTPUT" not in capsys.readouterr().out


def test_device_disappearing_logs_one_loud_fallback(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bridge.sd, "query_devices", Mock(side_effect=[
        [{"name": bridge.CONFIG["virtual_cable_name"], "max_output_channels": 2}], [], [],
    ]))
    eng, wav = playback(monkeypatch, tmp_path)
    bridge.sd.play.side_effect = [bridge.sd.PortAudioError("device"), None,
                                  bridge.sd.PortAudioError("device"), None]
    asyncio.run(eng._play_wav(str(wav), "halo", [], owns_temp=False))
    asyncio.run(eng._play_wav(str(wav), "lagi", [], owns_temp=False))
    assert capsys.readouterr().out.count("DEFAULT OUTPUT") == 1


def test_non_device_error_is_not_retried(monkeypatch, tmp_path):
    eng, wav = playback(monkeypatch, tmp_path)
    bridge.sd.play.side_effect = RuntimeError("bad data")
    asyncio.run(eng._play_wav(str(wav), "halo", [], owns_temp=False))
    assert bridge.sd.play.call_count == 1
    assert bridge.sd.query_devices.call_count == 1


def test_edge_path_also_recovers_rejected_device(monkeypatch):
    eng = bridge.TTSEngine()

    async def audio_stream():
        yield {"type": "audio", "data": b"fake mp3"}

    monkeypatch.setattr(bridge.edge_tts, "Communicate",
                        lambda *args: SimpleNamespace(stream=audio_stream))
    monkeypatch.setattr(bridge.sf, "read", lambda _: (np.zeros(480), 48000))
    bridge.sd.play.side_effect = [bridge.sd.PortAudioError("Invalid device"), None]
    asyncio.run(eng._speak_edge_tts("halo"))
    assert bridge.sd.play.call_count == 2
    assert not bridge.tts_is_playing


@pytest.mark.parametrize("engine", ["supertone", "edge_tts"])
def test_stream_stop_acknowledges_drain_during_playback_tail(monkeypatch, tmp_path, engine):
    from arti_live_safety import SpeechControl
    from scripts.livepath import bridge as rehearsal

    eng, wav = playback(monkeypatch, tmp_path)
    bridge.CONFIG.update(tts_engine=engine, rehearsal_parent_pid=None)
    monkeypatch.setattr(bridge, "tts", eng)
    monkeypatch.setattr(bridge, "speech_control", SpeechControl())
    monkeypatch.setattr(bridge, "voice_trigger_queue", bridge.queue.Queue())
    monkeypatch.setattr(bridge, "voice_trigger_buffer", bridge.arti_voice_queue.VoiceTriggerQueue())
    monkeypatch.setattr(bridge, "tts_stop_flag", False)
    monkeypatch.setattr(bridge.sd, "stop", Mock())
    monkeypatch.setattr(bridge, "_subtitle_broadcast_status", AsyncMock())
    monkeypatch.setattr(bridge.arti_expression_runtime, "apply_turn_end", AsyncMock())
    monkeypatch.setattr(eng, "_acquire_supertone", AsyncMock(return_value=(str(wav), [])))
    monkeypatch.setenv("ARTI_REHEARSAL_CONFIG", str(tmp_path / "rehearsal-config.json"))
    monkeypatch.setattr(rehearsal, "_idle_since", None)

    async def stream():
        yield {"type": "audio", "data": b"fake mp3"}

    async def tail(delay):
        assert delay == 0.3 and bridge.tts_is_playing
        (tmp_path / "bridge-stop-request").touch()
        await asyncio.Event().wait()

    async def answer(*_args):
        return await bridge._speak_reply_chunks(["One clip."], bridge.screen_privacy.epoch)

    monkeypatch.setattr(bridge.edge_tts, "Communicate", lambda *_: SimpleNamespace(stream=stream))
    monkeypatch.setattr(bridge.asyncio, "sleep", tail)
    monkeypatch.setattr(bridge, "_handle_voice_trigger", answer)
    asyncio.run(bridge._handle_voice_trigger_traced(bridge.VoiceTrigger("question", "mic"), [], "persona"))
    bridge.sd.play.assert_called_once()
    bridge.sd.stop.assert_called_once()
    assert not bridge.tts_is_playing
    assert rehearsal.drain_stop(tmp_path, pending=0, busy=bridge.tts_is_playing, clock=lambda: 1)
    with pytest.raises(KeyboardInterrupt):
        rehearsal.drain_stop(tmp_path, pending=0, busy=bridge.tts_is_playing, clock=lambda: 4)
    assert (tmp_path / "bridge-drained").is_file()


def test_midplay_error_and_late_cleanup_error_never_repeat_via_edge(monkeypatch, tmp_path):
    eng, wav = playback(monkeypatch, tmp_path)
    bridge.CONFIG["tts_engine"] = "supertone"
    eng._acquire_supertone = AsyncMock(return_value=(str(wav), []))
    eng._speak_edge_tts = AsyncMock()
    bridge.sd.wait.side_effect = bridge.sd.PortAudioError("device disconnected midplay")
    # Simulate a failure escaping the playback tail AFTER audio has started.
    monkeypatch.setattr(bridge.os.path, "exists", Mock(side_effect=OSError("cleanup failed")))
    asyncio.run(eng.speak("kalimat yang sama"))
    eng._speak_edge_tts.assert_not_awaited()
    assert bridge.sd.play.call_count == 1
    assert not bridge.tts_is_playing


def test_supertone_cooldown_then_revival(monkeypatch, capsys):
    now = [10.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])
    bridge.CONFIG.update(tts_engine="supertone", supertonic_revival_probe_sec=600)
    eng = bridge.TTSEngine()
    eng._acquire_supertone = AsyncMock(side_effect=[OSError("spawn failed"), ("clip.wav", [])])
    eng._speak_edge_tts = AsyncMock()
    eng._play_wav = AsyncMock()

    async def run():
        await eng.speak("satu")
        for _ in range(20):
            await eng.speak("dua")
        assert eng._acquire_supertone.await_count == 1
        now[0] += 600
        await eng.speak("pulih")

    asyncio.run(run())
    assert eng._acquire_supertone.await_count == 2
    assert eng._speak_edge_tts.await_count == 21
    assert capsys.readouterr().out.count("voice fallback to edge-tts") == 1


def test_prewarm_failure_parks_supertone(monkeypatch, capsys):
    bridge.CONFIG["tts_engine"] = "supertone"
    eng = bridge.TTSEngine()
    eng.supertone.ensure_alive = AsyncMock(side_effect=OSError("spawn"))
    eng._acquire_supertone = AsyncMock()
    eng._speak_edge_tts = AsyncMock()

    async def run():
        await eng.prewarm_supertone()
        await eng.speak("halo")

    asyncio.run(run())
    eng._acquire_supertone.assert_not_awaited()
    assert capsys.readouterr().out.count("voice fallback to edge-tts") == 1


def test_missing_interpreter_preflight_fails_once_without_spawn(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "_supertone_preflight_failed", False, raising=False)
    monkeypatch.setattr(bridge, "_resolve_venv312_python", Mock(side_effect=FileNotFoundError("venv312")))
    assert bridge._supertone_preflight() is False
    assert bridge._supertone_preflight() is False
    assert capsys.readouterr().out.count("[FAIL] Supertone") == 1
    bridge.subprocess.Popen.assert_not_called()


def fake_process(banner=None):
    return SimpleNamespace(
        poll=Mock(return_value=None), kill=Mock(), wait=Mock(return_value=0),
        stdin=io.StringIO(), stdout=io.StringIO(banner or ""), stderr=io.StringIO("warning\n"),
    )


def test_respawn_reaps_old_process_before_new_spawn_and_drains_stderr(monkeypatch):
    old = fake_process()
    new = fake_process(json.dumps({"type": "ready", "ok": True}) + "\n")
    monkeypatch.setattr(bridge, "_resolve_venv312_python", lambda: "fake-python")
    events = []
    drained = threading.Event()

    class Stderr:
        def __iter__(self):
            yield "fake model warning\n"
            drained.set()

    new.stderr = Stderr()
    old.kill.side_effect = lambda: events.append("kill")
    old.wait.side_effect = lambda timeout: events.append("wait")

    def spawn(*args, **kwargs):
        assert events == ["kill", "wait"]
        assert kwargs["stderr"] is bridge.subprocess.PIPE
        return new

    monkeypatch.setattr(bridge.subprocess, "Popen", spawn)
    manager = bridge.SupertoneProcess()
    manager.proc = old
    asyncio.run(manager.ensure_alive())
    assert manager.proc is new and manager._ready
    assert drained.wait(1), "stderr must be consumed without a synth request"


@pytest.mark.parametrize("banner", ["not json\n", '{"type":"ready","ok":false}\n'])
def test_bad_banner_kills_and_waits_child(monkeypatch, banner):
    proc = fake_process(banner)
    monkeypatch.setattr(bridge, "_resolve_venv312_python", lambda: "fake-python")
    monkeypatch.setattr(bridge.subprocess, "Popen", Mock(return_value=proc))
    manager = bridge.SupertoneProcess()
    with pytest.raises((bridge.SupertoneError, json.JSONDecodeError)):
        asyncio.run(manager.ensure_alive())
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()
    assert not manager._ready


@pytest.mark.parametrize("reply", ["", "bad json\n", '{"id":99,"ok":true}\n'])
def test_broken_protocol_reaps_child_before_cooldown(reply):
    proc = fake_process(reply)
    manager = bridge.SupertoneProcess()
    manager.proc, manager._ready = proc, True
    with pytest.raises((bridge.SupertoneError, json.JSONDecodeError)):
        asyncio.run(manager.request({"type": "synthesize"}, timeout=1))
    proc.kill.assert_called_once()
    proc.wait.assert_called_once()
    assert not manager._ready


def test_dsp_clips_before_pcm_write(monkeypatch, tmp_path):
    monkeypatch.setattr(dsp, "aktif", lambda _: True)
    monkeypatch.setattr(dsp, "_lebarkan_range", lambda data, sr, k: data * 1.4)
    result = dsp.poles_suara(np.array([0.9, -0.9]), 48000, {"supertonic_range_factor": 1.4})
    assert result.dtype == np.float32
    assert np.array_equal(result, [1.0, -1.0])
    path = tmp_path / "clipped.wav"
    bridge.sf.write(str(path), result, 48000, subtype="PCM_16")
    stored, _ = bridge.sf.read(str(path))
    assert stored[0] > 0.99 and stored[1] == -1.0


@pytest.mark.parametrize("failure", ["post", "write", "success"])
def test_groq_temp_wav_removed_on_every_path(monkeypatch, tmp_path, failure):
    bridge.CONFIG["groq_api_key"] = ""
    monkeypatch.setattr(bridge.tempfile, "tempdir", str(tmp_path))
    local = Mock(return_value=([SimpleNamespace(text="lokal")], None))
    monkeypatch.setattr(bridge, "local_whisper_model", SimpleNamespace(transcribe=local))
    # Merged design: _groq_asr_with_deadline reads response.text like requests.Response.
    response = SimpleNamespace(status_code=200, text="", json=lambda: {"text": "cloud"})
    post = Mock(return_value=response)
    if failure == "post":
        post.side_effect = OSError("offline")
    monkeypatch.setattr(bridge.arti_http_util, "groq_session", lambda: SimpleNamespace(post=post))
    import arti_api_telemetry as telemetry
    monkeypatch.setattr(telemetry, "record_call", Mock())
    if failure == "write":
        monkeypatch.setattr(bridge.sf, "write", Mock(side_effect=OSError("disk")))
    result = bridge.transcribe_audio(np.zeros(1600), api_key="gsk_fake_unit_only", quiet=True)
    assert result == ("cloud" if failure == "success" else "lokal")
    assert list(tmp_path.iterdir()) == []
