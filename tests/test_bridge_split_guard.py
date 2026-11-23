"""Guard the bridge split against back-imports and drifting re-exports."""

import ast
import builtins
import importlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_WATCHER = Path("arti_runtime/watcher_v2.py")
_MAIN_CHECK = ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body)
_WATCHER_FALLBACK = ast.dump(ast.parse("transcribe is None", mode="eval").body)
_WATCHER_IMPORT = ast.dump(ast.parse("from arti_bridge import transcribe_audio").body[0])

# Extend this table as more bridge helpers move.
_REEXPORTS = {
    "arti_runtime.speech_timing": (
        "resample_audio",
        "HNS_PER_SECOND",
        "_parse_word_boundary",
        "_PUNCTUATION_PHRASE",
        "_PUNCTUATION_CLAUSE",
        "MIN_PHRASE_CHARS",
        "MAX_PHRASE_CHARS",
        "_split_into_phrases",
        "_estimate_phrase_timings",
    ),
    "arti_runtime.reply_text": (
        "_TTS_EXPRESSION_TAG_RE",
        "_ANY_ANGLE_TAG_RE",
        "strip_tts_expression_tags",
        "clean_ai_reply",
        "_BAD_ARTICULATION_PATTERNS",
        "_sentence_is_bad_articulation",
        "filter_meta_history_talk",
        "_trim_stream_reply",
        "_truncate_reply_length",
        "_runaway_clause_cut",
        "_sentences_or_empty",
        "_CJK_RE",
        "_shorten_viewer_handles",
    ),
    "arti_runtime.asr_text": (
        "_ASR_LITERAL_PHANTOMS",
        "_ASR_BARE_THANKS",
        "_ASR_ALWAYS_NOISE",
        "_ASR_NOISE_SUBSTRINGS",
        "_normalize_asr_text",
        "filter_whisper_hallucination",
    ),
    "arti_runtime.yt_paid_parse": ("extract_membership", "extract_superchat"),
    "arti_runtime.groq_chat": (
        "_groq_fallback_chain", "_groq_model_hilang",
    ),
    "arti_runtime.startup_wizard": ("_WIZARD_PERSIST_KEYS",),
}


def _product_sources():
    # Root modules and the runtime package. The legacy launcher exists to load
    # the bridge, and operator entry points under scripts/ and tools/ do too.
    yield from (path for path in _ROOT.glob("*.py") if path.name != "hermes_vtuber_bridge.py")
    yield from (_ROOT / "arti_runtime").rglob("*.py")


def _module_nodes(tree, path):
    for statement in tree.body:
        # The standalone health command is outside product import paths.
        # The rest of bridge_health.py is a library and remains in scope.
        if (path == Path("bridge_health.py") and isinstance(statement, ast.If)
                and ast.dump(statement.test) == _MAIN_CHECK):
            for node in statement.orelse:
                yield from ast.walk(node)
        else:
            yield from ast.walk(statement)


def _watcher_import_exception(tree):
    # watcher_v2._audio has the CLI ASR fallback for when no transcriber was injected.
    # Match one statement in that branch so a second import still fails.
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name == "_audio":
            for statement in function.body:
                if isinstance(statement, ast.If) and ast.dump(statement.test) == _WATCHER_FALLBACK:
                    return next((node for node in statement.body if ast.dump(node) == _WATCHER_IMPORT), None)
    return None


def _imports_bridge(node):
    if isinstance(node, ast.Import):
        return any("arti_bridge" in alias.name.split(".") for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return ("arti_bridge" in (node.module or "").split(".")
                or any(alias.name == "arti_bridge" for alias in node.names))
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in {"__import__", "import_module"}:
            target = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "name"), None,
            )
            return isinstance(target, ast.Constant) and target.value == "arti_bridge"
    return False


def test_product_modules_do_not_import_the_bridge():
    violations = []
    for source in sorted(_product_sources()):
        path = source.relative_to(_ROOT)
        tree = ast.parse(source.read_bytes(), filename=str(path))
        allowed = _watcher_import_exception(tree) if path == _WATCHER else None
        for node in _module_nodes(tree, path):
            if node is not allowed and _imports_bridge(node):
                violations.append(f"{path.as_posix()}:{node.lineno}: {ast.unparse(node)}")
    assert not violations, "Product modules must not load arti_bridge:\n" + "\n".join(violations)


def _check_reexports():
    # The child uses the bridge's test-config path and pytest's null log sink.
    # Skip the project env loader and keep logging synchronous during import.
    with (
        patch("arti_env.load_project_env", return_value=False),
        patch("arti_buffered_writer.BatchedWriter", side_effect=lambda stream: stream),
        patch.object(threading.Thread, "start", side_effect=AssertionError("Bridge import started a thread")),
    ):
        bridge = importlib.import_module("arti_bridge")
    assert bridge.CONFIG["bridge_test_mode"] is True

    for module_name, names in _REEXPORTS.items():
        module = importlib.import_module(module_name)
        for name in names:
            assert getattr(bridge, name) is getattr(module, name), f"arti_bridge.{name} is not {module_name}.{name}"

    _check_prompt_wrappers(bridge)
    _check_groq_wrappers(bridge)
    _check_wizard_wrappers(bridge)


def _check_prompt_wrappers(bridge):
    from arti_runtime import prompt_blocks

    clock, safety = object(), object()
    memories = ["sample"]
    with (
        patch.object(bridge, "time", clock),
        patch.object(bridge, "arti_live_safety", safety),
        patch.object(prompt_blocks, "build_origin_context") as origin,
        patch.object(prompt_blocks, "build_today_block") as today,
        patch.object(prompt_blocks, "build_startup_memory_block") as memory,
    ):
        for config in ({"arti_debut_label": "first"}, {"arti_debut_label": "second"}):
            with patch.object(bridge, "CONFIG", config):
                assert bridge.build_origin_context({}) is origin.return_value
                assert bridge.build_today_block() is today.return_value
                assert bridge.build_startup_memory_block(memories) is memory.return_value
                origin.assert_called_with(config)
                today.assert_called_with(config, time=clock)
                memory.assert_called_with(memories, config, arti_live_safety=safety)

        explicit = {"arti_debut_label": "explicit"}
        bridge.build_origin_context(explicit)
        bridge.build_today_block(explicit)
        origin.assert_called_with(explicit)
        today.assert_called_with(explicit, time=clock)


def _check_groq_wrappers(bridge):
    from arti_runtime import groq_chat

    assert not hasattr(groq_chat, "_groq_health_tl")
    assert not hasattr(groq_chat, "_GROQ_TANPA_FALLBACK")

    def definition_lines(path):
        tree = ast.parse(Path(path).read_bytes())
        return [
            node.lineno for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "groq_chat_completion"
        ]

    for module in (bridge, groq_chat):
        first, second = definition_lines(module.__file__)
        assert module._groq_chat_completion_inti.__code__.co_firstlineno == first
        assert module.groq_chat_completion.__code__.co_firstlineno == second

    calls = (
        ("_remove_system_prompt_block", ("system", "marker"), None,
         ("re", "_SYSTEM_PROMPT_BLOCK_MARKERS", "_SYSTEM_PROMPT_KEEP_MARKERS")),
        ("_split_stardew_status", ("system",), "config", ("re",)),
        ("pick_groq_model", ("user", None, 0), "default_config", ("pick_groq_model",)),
        ("_trim_llm_payload", ("system", "user", 14000), None,
         ("_split_stardew_status", "_remove_system_prompt_block", "_SYSTEM_PROMPT_BLOCK_MARKERS", "SOUL_MARKER", "re")),
        ("_openrouter_after_groq", ("system", "user", {}, 503), None,
         ("screen_privacy", "_groq_health_tl", "arti_provider_health", "arti_openrouter")),
        ("_groq_chat_completion_inti", ("model", "system", "user", None), "default_config",
         ("_openrouter_after_groq", "_groq_fallback_chain", "screen_privacy", "live_max_tokens_for_trigger",
          "REPLY_THINKING_HEADROOM_TOKENS", "requests", "arti_reply_policy", "_trim_llm_payload", "_groq_model_hilang")),
        ("groq_chat_completion", ("model", "system", "user", None), "default_config",
         ("arti_provider_health", "_groq_chat_completion_inti", "_openrouter_after_groq",
          "_groq_health_tl", "_GROQ_TANPA_FALLBACK")),
    )
    for phase in (1, 2):
        config = {"test_phase": phase}
        with (
            patch.object(bridge, "CONFIG", config),
            patch.object(bridge, "requests", object()),
            patch.object(bridge, "_groq_health_tl", object()),
            patch.object(bridge, "_GROQ_TANPA_FALLBACK", object()),
        ):
            for name, args, config_key, dependencies in calls:
                expected = {dependency: getattr(bridge, dependency) for dependency in dependencies}
                if config_key:
                    expected[config_key] = config
                with patch.object(groq_chat, name) as implementation:
                    assert getattr(bridge, name)(*args) is implementation.return_value, name
                implementation.assert_called_once_with(*args, **expected)


def _check_wizard_wrappers(bridge):
    from arti_runtime import startup_wizard

    calls = (
        ("_wizard_input", ("prompt", "default"), ("sys", "input"), {}),
        ("prompt_live_session_setup", (), (
            "resolve_asr_input_device", "_wizard_input", "_extract_yt_video_id", "sys", "bridge_health", "print",
        ), {"config": "CONFIG"}),
        ("startup_wizard", (), (
            "arti_vault_rag", "prompt_live_session_setup", "socket", "sys", "input",
            "_supertone_preflight", "arti_vts_auth", "save_live_session", "print",
        ), {"config": "CONFIG", "script_path": "__file__"}),
        ("_extract_yt_video_id", ("video",), ("re",), {}),
        ("_save_config_to_file", (), ("_WIZARD_PERSIST_KEYS", "os", "json", "open", "print"),
         {"config": "CONFIG", "root_path": "_SCRIPT_DIR"}),
    )
    for phase in (1, 2):
        with (
            patch.object(bridge, "CONFIG", {"test_phase": phase}),
            patch.object(bridge, "__file__", f"bridge-{phase}.py"),
            patch.object(bridge, "_SCRIPT_DIR", f"root-{phase}"),
            patch.object(bridge, "save_live_session", object()),
            patch.object(bridge, "resolve_asr_input_device", object()),
            patch.object(bridge, "_supertone_preflight", object()),
        ):
            for name, args, dependencies, bindings in calls:
                expected = {key: getattr(bridge, key, getattr(builtins, key, None)) for key in dependencies}
                expected.update({key: getattr(bridge, binding) for key, binding in bindings.items()})
                with patch.object(startup_wizard, name) as implementation:
                    assert getattr(bridge, name)(*args) is implementation.return_value, name
                implementation.assert_called_once_with(*args, **expected)


def test_bridge_reexports_match_their_source_modules(tmp_path):
    config = tmp_path / "bridge-test-config.json"
    config.write_text("{}", encoding="utf-8")
    env = dict(os.environ, ARTI_BRIDGE_TEST_CONFIG=str(config), PYTHONIOENCODING="utf-8")
    env.pop("ARTI_REHEARSAL_CONFIG", None)

    # A fresh interpreter contains the tee and module state without launching
    # the bridge as __main__ or affecting other tests' bridge instance.
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "from tests.test_bridge_split_guard import _check_reexports; _check_reexports()"],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
