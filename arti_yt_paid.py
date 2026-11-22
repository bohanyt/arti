"""Bounded per-video ledger of accepted thank-you queues, not proof of speech."""
import json
import os
from pathlib import Path
import tempfile
import threading


class ThankedPaidMessages:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._write_warned = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(
                not isinstance(video, str) or not isinstance(ids, list)
                or any(not isinstance(msg_id, str) for msg_id in ids)
                for video, ids in data.items()
            ):
                raise ValueError("invalid paid-message ledger")
            self._entries = {video: list(dict.fromkeys(ids))[-500:]
                             for video, ids in list(data.items())[-5:]}
        except (OSError, ValueError) as exc:
            self._entries = {}
            print(f"[YT Paid] Ledger kosong: file hilang/rusak ({type(exc).__name__}).")

    def contains(self, video_id: str, message_id: str) -> bool:
        with self._lock:
            return message_id in self._entries.get(video_id, ())

    def record(self, video_id: str, message_id: str) -> None:
        if not video_id or not message_id:
            return
        with self._lock:
            ids = self._entries.pop(video_id, [])
            self._entries[video_id] = [i for i in ids if i != message_id][-499:] + [message_id]
            self._entries = dict(list(self._entries.items())[-5:])
            tmp = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=self.path.name + ".", suffix=".tmp", delete=False,
                ) as stream:
                    tmp = stream.name
                    json.dump(self._entries, stream, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self.path)
                tmp = None
            except OSError as exc:
                if not self._write_warned:
                    print(f"[YT Paid] Ledger gagal disimpan ({type(exc).__name__}).")
                    self._write_warned = True
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
