"""Console progress for Observer shutdown (tqdm optional)."""

from __future__ import annotations

from typing import Callable

ProgressCallback = Callable[[str, int, int, str], None]


def make_progress_callback(label: str = "Observer") -> ProgressCallback:
    try:
        from tqdm import tqdm

        bar = {"obj": None}

        def _cb(phase: str, current: int, total: int, message: str) -> None:
            desc = f"{label} {phase}"
            if bar["obj"] is None or getattr(bar["obj"], "desc", "") != desc:
                if bar["obj"] is not None:
                    bar["obj"].close()
                bar["obj"] = tqdm(total=max(total, 1), desc=desc, unit="step")
            if total > 0:
                bar["obj"].total = total
            bar["obj"].n = min(current, total)
            bar["obj"].set_postfix_str(message[:40])
            bar["obj"].refresh()
            if current >= total and total > 0:
                bar["obj"].close()
                bar["obj"] = None

        return _cb
    except ImportError:
        pass

    def _fallback(phase: str, current: int, total: int, message: str) -> None:
        pct = int(100 * current / total) if total else 0
        print(f"\r[{label}] {phase} {current}/{total} ({pct}%) — {message[:60]}", end="", flush=True)
        if total and current >= total:
            print()

    return _fallback
