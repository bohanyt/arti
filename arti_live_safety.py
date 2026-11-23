"""Live-only source fences and session-local owner speech control. No I/O."""
from __future__ import annotations
import re
from contextvars import ContextVar
from contextlib import contextmanager
import threading
import time

_BACKSTAGE = re.compile(
    r"\b(?:api|providers?|openrouter|groq|gemini|codex|chatgpt|astra|luna|"
    r"pytest|checkpoints?|orchestrat\w*|orkestrasi|latensi|latency|tokens?|"
    r"prompt|rag|embedding|whisper|smapi|git|commit|debug|terminal|powershell|"
    r"coding|kode|agen|agents?|model(?:s)?|file|berkas|tes(?:t|ts)?|"
    r"database|vault|sdk|obs|overlay)\b|\b(?:layout|tata letak|panel(?:-panel)?)\s+(?:siaran|stream(?:ing)?)\b|\barti_\w+|\w+\.(?:py|json|jsonl|md|cs)\b|"
    r"\b(?:idle|cursor|llm|tts|asr|scouter|observer)_\w+|[A-Z]:[\\/]",
    re.I,
)
def is_backstage(text: str) -> bool:
    return bool(_BACKSTAGE.search(str(text or "")))

def public_context(text: str) -> str:
    """Remove complete technical lines, before truncation or prompt assembly."""
    return "\n".join(line for line in str(text or "").splitlines()
                     if not is_backstage(line)).strip()

def public_summary(data: dict) -> dict:
    # Preserve model decisions/emotions; fence only text that could reach speech
    # or durable memory. Never persist internal commentary as stream facts.
    result=dict(data)
    for key in ("summary", "topic", "curious_hook", "curious_question", "screen_hint"):
        if key in result:
            result[key]=public_context(result.get(key) or "")
    if any(is_backstage(data.get(key) or "") for key in ("curious_hook", "curious_question", "screen_hint")):
        result["screen_relevant"] = False
        result["curious_worthy"] = False
    result["important_facts"]=[x for x in (result.get("important_facts") or [])
                               if isinstance(x,str) and not is_backstage(x)]
    return result

_PREFIX=r"(?:(?:eh|tolong) )?(?:arti )?"
_STOP=re.compile(_PREFIX+r"(?:diam|diem|shut up|berhenti (?:ngomong|bicara)|(?:gak|nggak|tidak) usah (?:ngomong|bicara))(?: dulu)?(?: ya)?")
_RESUME=re.compile(_PREFIX+r"(?:boleh (?:ngomong|bicara) lagi|lanjut (?:ngomong|bicara)|ngomong lagi)(?: ya)?")
_OWNER=frozenset({"mic","ptt","wake_word","mc_chat","mic_pasif"})

class SpeechControl:
    def __init__(self):
        self._lock=threading.RLock()
        self.held=False
        self.epoch=0
        self.changed_at=0.0
        self.owner_at=0.0
        self.owner_revision=0

    def note_owner_activity(self):
        """Supersede unspoken viewer/initiative replies without interrupting audio."""
        with self._lock:
            self.owner_at=time.time()
            self.owner_revision+=1

    def superseded(self, trigger_type, revision):
        with self._lock:
            return trigger_type in {"yt_chat", "curious"} and revision != self.owner_revision

    def command(self, text: str) -> str | None:
        text=" ".join(re.sub(r"[,.!?]+"," ",str(text or "").casefold()).split())
        if _STOP.fullmatch(text): return "stop"
        if _RESUME.fullmatch(text): return "resume"
        return None

    def note(self, text: str, trigger_type: str, viewer_name=None) -> str | None:
        if trigger_type not in _OWNER or viewer_name is not None: return None
        command=self.command(text)
        with self._lock:
            self.note_owner_activity()
            self.epoch+=1
            if command:
                self.held=command=="stop"
                self.changed_at=self.owner_at
        return command

    def allows(self, epoch: int | None=None) -> bool:
        with self._lock:
            return not self.held and (epoch is None or epoch==self.epoch)

    def interrupt(self, *, hold=False):
        """Invalidate work already dispatched by an explicit operator stop."""
        with self._lock:
            self.epoch += 1
            self.held = self.held or hold

    @contextmanager
    def retaining(self, epoch):
        """A stop either prevents this retry or clears it after it is queued."""
        with self._lock:
            yield self.allows(epoch)

speech_turn_epoch = ContextVar("speech_turn_epoch", default=None)
speech_control=SpeechControl()
