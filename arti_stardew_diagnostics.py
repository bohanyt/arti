"""Exception evidence for private runtime receipts (never captures frame locals)."""
import traceback


def exception_record(exc, *, phase):
    return dict(phase=phase, error=type(exc).__name__, message=str(exc),
                traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
