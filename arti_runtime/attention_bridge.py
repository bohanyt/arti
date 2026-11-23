"""ADR 172 bridge seams; no alternate voice lifecycle or inbox."""
import time
import uuid

from arti_live_safety import speech_turn_epoch
from arti_runtime import attention_desk as policy


def desk(bridge):
    inbox = bridge.voice_trigger_buffer
    state = getattr(inbox, "_attention_desk", None)
    if state is None:
        state = inbox._attention_desk = policy.AttentionDesk(deferred_limit=inbox.max_yt)
    return state


def audio(bridge, speaking, seconds):
    # Mabar's passive option is explicitly for headphones; VAD may observe the
    # owner's continuing floor during her speech, but selection still waits.
    if policy.enabled(bridge.CONFIG) and bridge.CONFIG.get("asr_pasif_reply_enabled", False):
        desk(bridge).audio(speaking, seconds, now=time.time())


async def dequeue(bridge):
    state, now = desk(bridge), time.time()
    if bridge._brain_busy or bridge.tts_is_playing or bridge.speech_control.held:
        return None
    pool = bridge.voice_trigger_buffer.snapshot()
    for raw in pool:
        if getattr(raw.text, "_host_story_guarded", False) and not raw.text.is_current():
            bridge.voice_trigger_buffer.take(raw)
        if raw.trigger_type in ("own_play", "read_in_game") and now-getattr(raw.text, "created_at", raw.enqueued_at) > policy.GAME_TTL_SEC:
            bridge.voice_trigger_buffer.take(raw)
    pool = bridge.voice_trigger_buffer.snapshot()
    signature = tuple(id(raw) for raw in pool)
    gap = policy.INGRESS_TICK_SEC if signature != state.last_signature else policy.TICK_SEC
    if now-state.last_tick < gap:
        return None
    state.last_tick, state.last_signature = now, signature
    busy = max((getattr(raw.text, "game_salience", 0) for raw in pool
                if now-getattr(raw.text, "created_at", now) <= policy.GAME_TTL_SEC), default=0)
    high_until = max((getattr(raw.text, "created_at", now)+policy.GAME_TTL_SEC for raw in pool
                      if getattr(raw.text, "game_salience", 0) >= policy.HIGH_SALIENCE), default=0)
    state.game_busy_until = max(getattr(state, "game_busy_until", 0), high_until)
    if now < state.game_busy_until:
        busy = max(busy, policy.HIGH_SALIENCE)
    relay = bridge._live7_help_relay
    status = getattr(relay, "_status_facts", None)
    native = getattr(status, "attention_native", {})
    busy = max(busy, policy.native_busy(native, now))
    scene = native.get("scene_state") or {}
    stamp = scene.get("observed_ts")
    fresh = type(stamp) in (int, float) and 0 <= now-stamp <= policy.GAME_TTL_SEC
    situation = dict.fromkeys(("cutscene", "dialogue", "fight", "walking", "menu", "nothing_special"))
    health = (native.get("own_state") or {}).get("health") or {}
    if type(health.get("observed_ts")) in (int, float) and 0 <= now-health["observed_ts"] <= policy.GAME_TTL_SEC:
        situation["health"] = health.get("value")
    if fresh:
        situation.update((scene.get("value") or {}).get("attention") or {})
    if fresh:
        situation["cutscene"] = (scene.get("value") or {}).get("event_active")
    menu = native.get("current_menu") or {}
    if type(menu.get("observed_ts")) in (int, float) and 0 <= now-menu["observed_ts"] <= policy.GAME_TTL_SEC:
        situation.update(menu=menu.get("value"), dialogue=menu.get("value") == "DialogueBox")
    story = getattr(relay, "_host_story", None)
    hosted = story.attention_situation() if story is not None else {}
    if hosted.get("cutscene"):
        situation.update(hosted)
    elif (not situation.get("cutscene") and state.scene_active and getattr(state, "scene_witness", None) == "host"
          and hosted.get("cutscene") is False):
        situation["cutscene"] = False
    if situation.get("cutscene"):
        state.scene_witness = situation.get("witness", "own game screen")
    situation.setdefault("lines", [dict(speaker=getattr(raw.text, "speaker", "UNKNOWN"),
        text=getattr(raw.text, "screen_text", "")) for raw in pool if raw.trigger_type == "read_in_game"])
    situation["lines"] = policy.bounded_lines(situation["lines"])
    situation["story"] = any(getattr(raw.text, "_host_story", False) or getattr(raw.text, "family", None) == "event_dialogue" for raw in pool)
    situation["since_last_spoke_sec"] = round(now-state.last_spoke, 2) if state.last_spoke is not None else None
    traits = policy.selected_traits(getattr(status, "attention_traits", []), situation)
    if situation.get("fight"):
        busy = max(busy, 0.95)
    epoch = bridge.speech_control.epoch
    async def framed_choice(facts):
        import arti_executors
        return await policy.fast_choice(facts, bridge.CONFIG, bridge.arti_http_util.groq_session(),
            lambda fn, *a, **kw: arti_executors.run("background", fn, *a, **kw),
            situation={k: v for k, v in situation.items() if k != "host_story"}, traits=traits)
    result = await state.tick(pool, now=now, config=bridge.CONFIG, busy=busy, choose=framed_choice, situation=situation)
    from scripts.livepath.bridge import emit
    emit(result.journal())  # kind/age/score/index only, no names/text
    if not bridge.speech_control.allows(epoch) or bridge.tts_is_playing or bridge._brain_busy:
        return None
    selected = result.choice
    if selected is None or (bridge.hotkey_active and selected.kind != "cut_in"):
        return None
    if selected.raw is not None:
        if not bridge.voice_trigger_buffer.take(selected.raw):
            return None
        trigger = bridge._normalize_voice_trigger(selected.raw)
        if selected.facts:
            if getattr(trigger.text, "_host_story_guarded", False):
                trigger.text.attention_facts, trigger.text.attention_candidate = selected.facts, selected
            else:
                from dataclasses import replace
                trigger = replace(trigger, text=policy.AttentionText(trigger.text, selected.facts, selected))
    else:
        trigger = bridge.VoiceTrigger(policy.AttentionText("[Attention] "+selected.facts, selected.facts, selected),
                                      selected.kind, queued_at=now, turn_id=uuid.uuid4().hex[:12])
    # Preserve own-play's string marker/direct-speech attributes without copying them.
    if not getattr(trigger.text, "attention_candidate", None):
        if type(trigger.text) is str:
            from dataclasses import replace
            trigger = replace(trigger, text=policy.AttentionText(trigger.text, candidate=selected))
        else:
            trigger.text.attention_candidate = selected
    host_story = getattr(selected, "host_story", None)
    if host_story is None:
        host_story = situation.get("host_story")
    if host_story is not None:
        trigger.text.host_story_context = host_story
    elif hasattr(trigger.text, "host_story_context"):
        trigger.text.host_story_context = None
    with bridge._brain_busy_lock:
        bridge._brain_busy, bridge._brain_busy_since = True, time.time()
    return trigger


def may_speak(bridge, trigger):
    if not policy.enabled(bridge.CONFIG):
        return True
    candidate = getattr(trigger.text, "attention_candidate", None)
    if trigger.trigger_type == "cut_in" or not (bridge.hotkey_active or desk(bridge).owner_speaking(time.time())):
        return True
    if candidate is not None and candidate.raw is not None:
        with bridge.speech_control.retaining(speech_turn_epoch.get()) as allowed:
            if allowed:
                bridge.voice_trigger_buffer.enqueue(candidate.raw)
    return False


def voiced(bridge, trigger):
    if not policy.enabled(bridge.CONFIG):
        return
    candidate = getattr(trigger.text, "attention_candidate", None)
    if candidate is not None:
        desk(bridge).voiced(candidate, now=time.time())
        from scripts.livepath.bridge import emit
        emit(dict(event="attention_voiced", kind=candidate.kind, deferred=candidate.deferred,
                  waiting=len(candidate.waiting), turn_id=trigger.turn_id,
                  deferred_turn_ids=[raw.turn_id for raw in candidate.waiting]))


def finish(bridge, trigger):
    """A promised return stays pending if generation/speech failed or was held."""
    if policy.enabled(bridge.CONFIG):
        candidate = getattr(trigger.text, "attention_candidate", None)
        if candidate is not None and candidate.raw is not None and id(candidate.raw) in desk(bridge).deferred:
            # The legacy passive-cancellation helper may have retained a wrapper
            # for this same event. Settle its original identity exactly once.
            for raw in bridge.voice_trigger_buffer.snapshot():
                if raw is not candidate.raw and getattr(raw.text, "attention_candidate", None) is candidate:
                    bridge.voice_trigger_buffer.take(raw)
            if not any(raw is candidate.raw for raw in bridge.voice_trigger_buffer.snapshot()):
                bridge.voice_trigger_buffer.enqueue(candidate.raw)
