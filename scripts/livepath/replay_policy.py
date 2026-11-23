"""Device-free execution of the CURRENT bridge's scheduling functions (ADR 188).

Compile only named function definitions, never module imports/top-level startup.
There is no saved copy of queue admission, expiry, W3 marking or dequeue policy.
Missing/changed seams fail loudly. Device, history and motor effects are inert.
"""
from __future__ import annotations

import ast
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
import queue
import threading
from types import SimpleNamespace as NS

import arti_voice_queue
from arti_live_safety import speech_turn_epoch
from arti_screen_privacy import STREAMER_TRIGGERS
from arti_wake import is_arti_wake_call
from arti_runtime import attention_bridge, attention_desk, chat_media, chat_rotation, mic_priority, ssn_chat, streamer_voice
from arti_runtime.asr_text import _ASR_LITERAL_PHANTOMS

FUNCTIONS = (
    '_normalize_voice_trigger', '_note_owner_speech', 'clear_trigger_queue',
    '_live7_game_session_stopped', '_live7_game_lane_trigger', '_speech_trigger_expired',
    '_last_reply_end_ts', '_mic_pasif_owner_marked', '_passive_owner_reply_required', '_drop_waiting_unmarked_mic',
    'queue_voice_trigger', '_dequeue_buffered_trigger_if_brain_ready', '_voice_reply_superseded',
    '_game_reaction_expired', '_reaksi_penting', '_game_trigger_waiting',
    '_own_play_can_wait', '_own_play_min_gap', '_queued_trigger_types', '_own_play_held_back', '_own_play_lane_busy',
    '_note_own_play_voiced', '_media_hold_allows', '_waiting_hold_pass', 'hold_media_playback',
    '_merge_waiting_yt_chat',
    '_make_voice_trigger_buffer', '_admit_quiet_viewer_chat', '_requeue_quiet_chat',
    '_retain_chat_trigger', 'is_bot_viewer',
    '_enqueue_legacy_voice', '_capture_allows_turn', '_viewer_yields_to_owner',
    '_viewer_ptt_gate_closed', '_requeue_quiet_chat_current',
)
CONSTANTS = ('_GAME_SESSION_LANE', '_OWN_PLAY_YIELD_KINDS', 'QUIET_CHAT_MAX_REQUEUES', 'VIEWER_PTT_GATE_MAX_SEC')


@lru_cache(maxsize=1)
def compiled_policy():
    path = Path(__file__).resolve().parents[2] / 'arti_bridge.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    nodes = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    constants = {n.targets[0].id: n for n in tree.body if isinstance(n, ast.Assign)
                 and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id in CONSTANTS}
    body = [constants[name] for name in CONSTANTS] + [nodes[name] for name in FUNCTIONS]
    # Execute only the message callback, never the reader's session lookup,
    # websocket connection, watcher thread or other worker initialization.
    callbacks = {n.name: n for n in nodes['ssn_chat_worker'].body if isinstance(n, ast.FunctionDef)}
    body.append(callbacks['on_line'])
    return compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec')


def bind(bridge, trigger_class):
    """Bind real function globals to this session's virtual bridge state."""
    def inert(*args, **kwargs):
        return None

    bridge.__dict__.update(
        __name__=__name__, VoiceTrigger=trigger_class, arti_voice_queue=arti_voice_queue,
        attention_bridge=attention_bridge, attention_desk=attention_desk,
        streamer_voice=streamer_voice, mic_priority=mic_priority, chat_rotation=chat_rotation,
        chat_media=chat_media, ssn_chat=ssn_chat, is_arti_wake_call=is_arti_wake_call,
        _quiet_chat=chat_media.QuietChat(), _chat_rotator=chat_rotation.Rotator(),
        _yt_pernah_chat=set(), _last_yt_trigger_by_viewer={},
        state=dict(last_trigger=0.0), last_addressed={},
        queue=queue, voice_trigger_queue=queue.Queue(),
        STREAMER_TRIGGERS=STREAMER_TRIGGERS,
        _last_own_play_voiced_at=None, _last_arti_reply_ts=0.0, last_arti_reply_text='',
        voice_listener_worker=NS(_last_tts_end=0), _media_playback_until=0.0,
        _host_mode=False, _reflex_playing=False, tts_stop_flag=False,
        _sd_gerbang=threading.RLock(), sd=NS(stop=inert),
        _desktop_ear_gate=NS(hold=inert), _request_alert_yield=inert,
        _note_screen_privacy=inert, _note_streamer_text_for_afk=inert,
        _note_stardew_owner_ingress=inert, _discard_stardew_capture=inert,
        request_asr_stream_restart=inert, add_to_history=inert, _mc_runner_active=lambda: False,
        _game_live=lambda: False,
        speech_turn_epoch=speech_turn_epoch, _ptt_capture_opened_at=None,
        replace=replace, _ASR_LITERAL_PHANTOMS=_ASR_LITERAL_PHANTOMS,
    )
    bridge._owner_speaking_now = lambda: bool(bridge.hotkey_active or
        attention_bridge.desk(bridge).owner_speaking(bridge.time.time()))
    exec(compiled_policy(), bridge.__dict__)
    return bridge
