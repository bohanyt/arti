"""Load project .env + normalize API key env aliases."""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

_ALIASES: tuple[tuple[str, str], ...] = (
    ("CLOUDFLARE_API_KEY", "CLOUDFLARE_API_TOKEN"),
    ("OLLAMA_KEY", "OLLAMA_API_KEY"),
    ("ZHIPU_API_KEY", "ZAI_API_KEY"),
)


def load_project_env(root: Path | None = None) -> bool:
    """Load `.env` from repo root. Returns True if file was loaded."""
    base = root or _ROOT
    env_path = base / ".env"
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        _load_env_manual(env_path)
    _apply_aliases()
    return True


def _load_env_manual(env_path: Path) -> None:
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _apply_aliases() -> None:
    for src, dst in _ALIASES:
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]


def env_key_status(keys: list[str] | None = None) -> dict[str, str]:
    """Return SET/EMPTY per key (no secret values)."""
    names = keys or [
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
        "NVIDIA_API_KEY",
        "GEMINI_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "GITHUB_TOKEN",
        "ZAI_API_KEY",
        "OLLAMA_API_KEY",
    ]
    out: dict[str, str] = {}
    for k in names:
        v = (os.environ.get(k) or "").strip()
        out[k] = "SET" if v and v not in ("YOUR_GEMINI_API_KEY",) else "EMPTY"
    return out
