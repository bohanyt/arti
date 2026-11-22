"""Measure-only experiment: independent chat generation, one serial mouth.

No production bridge imports or changes. Admission and lane-local selection
still use the real queue and classic bridge helpers. Finished replies use the
same priority picker, with no second dedup/overflow policy on generated text.
"""
from dataclasses import dataclass
import heapq

import arti_voice_queue as Q
from arti_runtime import attention_desk as AD


def slot_for(kind):
    return 1 if kind in AD.CHAT_KINDS else 0


class SlotInbox:
    def __init__(self, queue, slot):
        self.queue, self.slot = queue, slot

    def __len__(self):
        return len(self.queue)

    def snapshot(self):
        return self.queue.snapshot()

    def dequeue(self, *, skip_types=()):
        other_lane = {item.trigger_type for item in self.queue._items
                      if slot_for(item.trigger_type) != self.slot}
        return self.queue.dequeue(skip_types=tuple(other_lane | set(skip_types)))

    def dequeue_best(self, allowed):
        return self.queue.dequeue_best(lambda it: slot_for(it.trigger_type) == self.slot and allowed(it))


@dataclass(eq=False)
class Flight:
    state: tuple
    slot: int
    stage: str = 'generating'
    ready_at: float | None = None
    ready_item: object = None
    audio_index: int | None = None


async def run_parallel(worker, session, config, native, heap, schedule):
    if AD.enabled(config):
        raise ValueError('chat-slot experiments require the classic current picker')
    generators = [None, None]
    mouth = None
    flights = []
    # No generated reply is silently deduplicated or evicted here. Existing
    # speech/game expiry is rechecked explicitly, counted as cancellation.
    ready = worker.b._make_voice_trigger_buffer()
    ready.preserve_humans, ready.ttl_sec = True, float('inf')

    def invalid(flight, *, expiry=False):
        trigger, event, cost, revision, epoch = flight.state
        if not worker.b.speech_control.allows(epoch):
            return 'owner_control'
        if worker.b._voice_reply_superseded(trigger, revision):
            return 'superseded'
        if expiry and (worker.real_speech_expired(trigger) or worker.real_game_expired(trigger)):
            return 'expired'
        return None

    def cancel(flight, reason):
        nonlocal mouth
        if flight.stage == 'done':
            return
        if flight.ready_item is not None:
            ready.take(flight.ready_item)
        if flight.audio_index is not None:
            a, b, kind, event = worker.audio[flight.audio_index]
            worker.audio[flight.audio_index] = (a, min(b, worker.now), kind, event)
        worker.counts['cancelled'][flight.state[0].trigger_type] += 1
        worker.discard(flight.state, reason)
        if generators[flight.slot] is flight:
            generators[flight.slot] = None
        if mouth is flight:
            mouth = None
            worker.b.tts_is_playing = False
        flight.stage = 'done'

    def dispatch_mouth():
        nonlocal mouth
        if mouth is not None or worker.b.speech_control.held:
            return
        # Prune every ready reply, including low-priority ones that never win
        # the mouth. These are spent generations, not pending queue expiries.
        for item in ready.snapshot():
            flight = item.flight
            reason = invalid(flight, expiry=True)
            if reason:
                cancel(flight, 'ready_wait_' + reason)
        item = ready.dequeue_best(worker.b._media_hold_allows)
        if item is not None:
            mouth = item.flight
            mouth.stage = 'mouth'
            schedule(worker.now+mouth.state[2].synth, 'slot_audio', mouth)

    while heap:
        worker.now, _, tag, data = heapq.heappop(heap)
        if worker.now > session.end:
            break
        if tag == 'input':
            if data.type != 'vad':
                worker.b._brain_busy = any(generators) or mouth is not None
                worker.ingress(data)
                for flight in flights:
                    if flight.stage == 'done':
                        continue
                    owner_control = not worker.b.speech_control.allows(flight.state[4])
                    media_blocked = config.get('media_watch_mode') and not worker.b._media_hold_allows(flight.state[0])
                    if owner_control or media_blocked:
                        worker.b._retain_chat_trigger(flight.state[0])
                        cancel(flight, 'media_hold' if media_blocked else 'owner_control')
                if data.type == 'media_hold' and mouth is not None and mouth.audio_index is not None and worker.b.tts_stop_flag:
                    cancel(mouth, 'media_hold')
        elif tag == 'native':
            native.update(own_state={'health': {'observed_ts': worker.now,
                'value': {'current': data.get('hp'), 'max': data.get('max_hp')}}},
                current_menu={'observed_ts': worker.now, 'value': data.get('active_menu')})
        elif tag == 'slot_generated' and data.stage == 'generating':
            flight = data
            worker.generated(flight.state)
            generators[flight.slot] = None
            reason = invalid(flight)
            if reason:
                cancel(flight, 'generation_' + reason)
            elif not flight.state[2].speaks:
                flight.stage = 'done'
            else:
                trigger, event, cost, revision, epoch = flight.state
                flight.stage, flight.ready_at = 'ready', worker.now
                item = Q.QueuedVoiceTrigger(trigger.text, trigger.trigger_type, trigger.viewer_name,
                    worker.now, trigger.turn_id,
                    name_addressed=bool(event and ((trigger.owner_marked if config.get('mic_priority_mark')
                        and trigger.trigger_type == 'mic_pasif' else event.named))),
                    owner_marked=trigger.owner_marked, rotation_rank=trigger.rotation_rank)
                item.flight = flight
                flight.ready_item = item
                ready.enqueue(item)
            # Deferred dispatch sees all completions already queued at this
            # instant, so simultaneous readiness uses the real owner-first picker.
            schedule(worker.now, 'mouth')
        elif tag == 'slot_audio' and data.stage == 'mouth':
            if not worker.b.speech_control.allows(data.state[4]):
                cancel(data, 'owner_control')
            else:
                trigger, event, cost, revision, epoch = data.state
                data.audio_index = len(worker.audio)
                worker.audio.append((worker.now, min(session.end, worker.now+cost.play), trigger.trigger_type, event))
                worker.generation_records[id(data.state)].spoken = True
                worker.b.tts_is_playing = True
                schedule(worker.now+cost.play, 'slot_voiced', data)
        elif tag == 'slot_voiced' and data.stage == 'mouth':
            worker.b.tts_is_playing = False
            worker.b._last_arti_reply_ts = worker.now
            worker.b.last_arti_reply_text = data.state[2].reply_text
            if data.state[0].trigger_type == 'own_play':
                worker.b._note_own_play_voiced()
            data.stage = 'mouth_cleanup'
            schedule(worker.now+data.state[2].cleanup, 'slot_done', data)
        elif tag == 'slot_done' and data.stage == 'mouth_cleanup':
            data.stage, mouth = 'done', None
            schedule(worker.now, 'mouth')
        elif tag == 'mouth':
            dispatch_mouth()
        elif tag == 'tick':
            if not worker.b.speech_control.held:
                for slot in range(2):
                    if generators[slot] is not None:
                        continue
                    # Partition only admission to the two experimental slots.
                    # The bridge dequeue and Q.dequeue retain actual selection,
                    # replacement and expiry behavior within each lane.
                    worker.b.voice_trigger_buffer = SlotInbox(worker.q, slot)
                    worker.b._brain_busy = False
                    try:
                        trigger = worker.b._dequeue_buffered_trigger_if_brain_ready()
                    finally:
                        worker.b.voice_trigger_buffer = worker.q
                    if trigger is not None:
                        flight = Flight(worker.start_turn(trigger, session, config), slot)
                        flights.append(flight)
                        generators[slot] = flight
                        schedule(worker.now+flight.state[2].generation, 'slot_generated', flight)
            worker.b._brain_busy = any(generators) or mouth is not None
            schedule(worker.now, 'mouth')
            schedule(worker.now+.1, 'tick')
    worker.now = session.end
    for flight in flights:
        if flight.stage not in {'done', 'mouth_cleanup'}:
            cancel(flight, 'shutdown')
