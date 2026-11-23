"""Rehearsal only: the owner's double toggle ("bungkam") as a scripted console line (2026-11-18).

A normal PTT toggle never cuts her off (owner, 2026-10-24); the only owner action that stops her turn is a double
toggle within 2 s. The third rehearsal pass could not prove that the real turn/interrupt keeps the long voice thread
(ADR 200, V13), because a typed owner line only queues. ``bungkam(bridge)`` runs what ``arti_bridge``'s PTT
handlers run for a double toggle (toggle ON, then OFF with bungkam), in the same order, so the rehearsal reaches the
real cancel path: the in-flight API task is cancelled, her playing audio stops, the queue is cleared.

The bridge calls it only from ``text_input_worker`` under the scripted-PTT conditions (config flag, rehearsal
parent, env var, no live chat); ``mabar say --bungkam`` writes the line only into a rehearsal run.
"""

COMMAND = "bungkam"


def bungkam(bridge) -> None:
    # Toggle ON (the first press).
    bridge.arti_executors.note_user_cancel()  # a cancel after this is the user's, not a timeout
    bridge._ptt_attention_pause()
    # Toggle OFF within 2 s: bungkam.
    bridge.arti_executors.note_user_cancel()
    bridge._stop_voice_now(ptt=True)
    print("\n⚫⚫ [DOUBLE TOGGLE] Bungkam! (rehearsal script)")
    bridge._cancel_lamp_fallback()
    bridge.start_idle_animation()
