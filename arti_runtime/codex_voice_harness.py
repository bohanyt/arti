"""V13: the voice lane's Codex child without the coding-agent harness (ADR 200, "[recorded remark omitted]").

`codex debug prompt-input` (codex-cli 0.169.1, gpt-6-luna) renders about 65k characters of model-visible harness
before her own prompt: tool definitions (code mode, collaboration/spawn_agent, MCP, browser, plugins) 30.5k, the
"You are Codex" text 18.4k (already replaced by the thread's `base_instructions`), skills 11.7k, the multi-agent role
3.5k and the environment context 1k. The voice child also loads the owner's `~/.codex/config.toml` MCP servers. Her
voice uses no Codex tool, so these overrides remove all of it; the thread's `base_instructions` and
`developer_instructions` stay the only instructions.

The multi-agent role text comes from the model catalog entry (`multi_agent_version`), not from a feature flag, so the
voice child gets a one-model catalog copied from the Codex cache without that field. Without a usable cache entry the
catalog is skipped (logged once) and the role text stays; everything else is still removed.

Voice lane only: other Codex roles (`arti_codex_role`, the Stardew decision client) own their overrides.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

# Every feature whose tools or text reach the model-visible input of a plain read-only thread (measured, 0.169.1).
LEAN_FEATURES = (
    "apps", "plugins", "remote_plugin", "browser_use", "browser_use_external", "computer_use", "image_generation",
    "view_image", "shell_tool", "goals", "multi_agent", "skill_search", "tool_suggest", "sleep_tool", "in_app_browser",
    "hooks", "skill_mcp_dependency_install", "code_mode_host", "code_mode_host_grpc", "deferred_tool_world_state",
    "incremental_tools", "incremental_tools_legacy", "prediction_preserve_parent_tools", "guardian_approval",
    "tool_call_mcp_elicitation",
)
LEAN_SETTINGS = (
    "mcp_servers={}",  # the owner's config.toml MCP servers never start for her voice
    "skills.enabled=false", "skills.bundled.enabled=false", "skills.include_instructions=false",
    "include_permissions_instructions=false", "include_collaboration_mode_instructions=false",
    # cwd/shell/date of the scratch folder; the bridge's own blocks carry her clock and day.
    "include_environment_context=false",
)
CATALOG_FIELDS_DROPPED = ("multi_agent_version",)

_catalog_lock = threading.Lock()
_catalogs: dict = {}
_catalog_warned: set = set()


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def voice_model_catalog(model: str, *, home: Path | None = None) -> str | None:
    """Path of a one-model catalog for `model` without the multi-agent role, or None (logged once) when the Codex
    cache has no entry. Written once per process into its own temp folder, never into the child's empty cwd."""
    home = Path(home) if home is not None else codex_home()
    key = (str(home), model)
    with _catalog_lock:
        cached = _catalogs.get(key)
        if cached is not None and Path(cached).is_file():
            return cached
        try:
            data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
            entry = next(dict(m) for m in data.get("models", []) if m.get("slug") == model)
        except (OSError, ValueError, StopIteration, AttributeError, TypeError) as exc:
            if key not in _catalog_warned:
                _catalog_warned.add(key)
                print(f"[Codex] voice harness: no catalog entry for {model} ({type(exc).__name__}); "
                      "the multi-agent role text stays")
            return None
        for name in CATALOG_FIELDS_DROPPED:
            entry.pop(name, None)
        folder = Path(tempfile.mkdtemp(prefix="arti-codex-voice-catalog-"))
        path = folder / "voice-models.json"
        path.write_text(json.dumps({"models": [entry]}), encoding="utf-8")
        _catalogs[key] = str(path)
        return str(path)


DEFAULT_WINDOW = 272000
_window_cache: dict = {}


def child_context_window(model: str, *, home: Path | None = None) -> tuple[int, str]:
    """(window, source) the voice child gets from its own Codex config, without starting it (ADR 200, V13).

    `model_context_window` in `config.toml` (top level, as the owner sets it), capped at the catalog's
    `max_context_window`; else the catalog's `context_window`; else 272000. Read once per process. The lane lowers it
    further to what a turn's token usage reports (`ThreadTokenUsage.model_context_window`)."""
    home = Path(home) if home is not None else codex_home()
    key = (str(home), model)
    with _catalog_lock:
        if key in _window_cache:
            return _window_cache[key]
    entry = {}
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
        entry = next((dict(m) for m in data.get("models", []) if m.get("slug") == model), {})
    except (OSError, ValueError, AttributeError, TypeError):
        entry = {}
    configured = None
    try:
        import tomllib  # noqa: PLC0415 — 3.11+

        configured = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("model_context_window")
    except (OSError, ValueError):
        configured = None
    maximum = entry.get("max_context_window")
    if isinstance(configured, int) and configured > 0:
        result = (min(configured, maximum) if isinstance(maximum, int) and maximum > 0 else configured), "config.toml"
    elif isinstance(entry.get("context_window"), int) and entry["context_window"] > 0:
        result = entry["context_window"], "catalog"
    else:
        result = DEFAULT_WINDOW, "default"
    with _catalog_lock:
        _window_cache[key] = result
    return result


def lean_overrides(model: str, *, home: Path | None = None) -> tuple:
    """Config overrides for the voice child: no tools, no skills, no environment text, her model as the default."""
    out = [*LEAN_SETTINGS, *(f"features.{name}=false" for name in LEAN_FEATURES), "model=" + json.dumps(model)]
    catalog = voice_model_catalog(model, home=home)
    if catalog:
        out.append("model_catalog_json=" + json.dumps(catalog))
    return tuple(out)
