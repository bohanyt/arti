"""The owner typing in the live chat from the channel's own account.

On a solo stream the owner may type in chat instead of using push-to-talk. Read as an ordinary viewer, those lines
would get chat priority, be droppable by the 2-deep chat queue, and in her record `viewer_said` be limited to one
kept line per ten minutes. Under `owner_chat_as_owner` a line from one of his handles (`owner_yt_handles`, else
`yt_default_viewer`; `arti_session_mode.owner_handles`) takes the owner path instead, the same as a PTT line:
- owner weight in the queue;
- his stop commands;
- the screen-privacy commands;
- "Streamer" in her history;
- the owner told-line path into her game record (`owner_said`).
A Super Chat or membership from his account keeps the paid path.
"""


def is_owner(viewer, config):
    from arti_session_mode import normalize_handle, owner_handles
    handles = owner_handles(config)
    return bool(handles) and normalize_handle(viewer or "") in handles


def take(bridge, viewer, text):
    """His chat line as his own speech: what a PTT transcript gets (the scripted-PTT branch's order)."""
    print(f"[YT Chat] {viewer} = akun Bohan sendiri: jalur pemilik", flush=True)
    bridge.add_to_history("Streamer", text)
    if bridge.CONFIG.get("stream_game_notes"):
        bridge._note_screen_privacy(text)
        from arti_runtime import stream_notes
        stream_notes.bridge_line(bridge, "owner", "ptt", text, owner_marked=True)
    bridge.queue_voice_trigger(text, trigger_type="ptt")
    return True
