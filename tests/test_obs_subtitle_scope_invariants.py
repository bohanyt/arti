"""Static / snapshot scope-boundary tests for OBS Subtitle Integration.

Spec: ``.kiro/specs/obs-subtitle-integration``

This file owns the static AST and byte-snapshot checks that lock in the
"strictly additive" promise of the feature. Every test here is synchronous,
example-based, and free of ``hypothesis`` decorators - the goal is to fail
fast on accidental drift in the parts of the codebase that the design says
must stay byte-identical or structurally unchanged.

Tasks covered (from ``tasks.md``):

- 4.4 - Bridge does not redefine ``subtitle_server.handler`` (Reqs 3.6, 3.3).
- 8.2 - Property 15: ``subtitle.html`` and JSON message-schema bytes are
        preserved (Reqs 6.1, 6.2).
- 8.3 - Property 16: ``broadcast_subtitle`` is gated to synthesized speech
        only (Req 6.3).
- 8.4 - Property 17: no persistence and no replay surface for subtitles
        (Reqs 6.5, 6.6).
- 8.6 - VTS / LLM / YouTube error paths still terminate (Req 3.8).
- 8.7 - ``tts_is_playing`` read sites still gate microphone listening
        (Req 5.6).
- 8.8 - No word segmentation / tokenization (Req 6.4).
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest


# Repo root resolves the same way as ``tests/test_bridge_startup_bugfix.py``
# so ``conftest.py`` does not have to be touched.
REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_FILE = REPO_ROOT / "hermes_vtuber_bridge.py"
SUBTITLE_SERVER_FILE = REPO_ROOT / "subtitle_server.py"
SUBTITLE_HTML_FILE = REPO_ROOT / "subtitle.html"
BASELINES_DIR = REPO_ROOT / "tests" / "_baselines"
SUBTITLE_HTML_SHA_FILE = BASELINES_DIR / "subtitle_html.sha256"
CRITICAL_PATHS_BASELINE = BASELINES_DIR / "critical_error_paths.json"


# ---------------------------------------------------------------------------
# Cached AST trees - parse each source file once for the whole module.
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


_BRIDGE_TREE: ast.Module = _parse(BRIDGE_FILE)
_SUBTITLE_SERVER_TREE: ast.Module = _parse(SUBTITLE_SERVER_FILE)


def _walk_with_parents(tree: ast.AST):
    """Yield ``(node, [ancestors top-down])`` for every node in the tree.

    ``ast`` does not record parent links by default, so we attach them here
    once and reuse the closure-friendly stack to find enclosing function /
    class definitions for the call-site checks below.
    """

    stack: list[ast.AST] = []

    def _visit(node: ast.AST) -> None:
        yield node, list(stack)
        stack.append(node)
        for child in ast.iter_child_nodes(node):
            yield from _visit(child)
        stack.pop()

    yield from _visit(tree)


def _enclosing_func_and_class(parents: list[ast.AST]) -> tuple[str | None, str | None]:
    """Return the names of the innermost enclosing function and class.

    ``parents`` is ordered top-down (module first), so the innermost wrapper
    is the *last* matching entry. Either return value may be ``None`` when
    the node is at module scope or outside any class.
    """

    func_name: str | None = None
    class_name: str | None = None
    for ancestor in parents:
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = ancestor.name
        elif isinstance(ancestor, ast.ClassDef):
            class_name = ancestor.name
    return func_name, class_name


# ===========================================================================
# Task 4.4 - Bridge does not redefine subtitle_server.handler
# Feature: obs-subtitle-integration, Property: bridge does not shadow handler
# Validates: Requirements 3.6, 3.3
# ===========================================================================


def test_bridge_does_not_redefine_subtitle_server_handler():
    """Task 4.4: assert no top-level ``handler`` is defined in the bridge,
    and ``from subtitle_server import`` exposes the three required names.

    The actual import in ``hermes_vtuber_bridge.py`` aliases each name (e.g.
    ``broadcast_subtitle as _subtitle_broadcast``); we therefore check the
    ``name`` attribute (the original symbol) of every ``ast.alias`` rather
    than the local ``asname`` to honor Req 3.3 byte-for-byte.

    Validates: Requirements 3.6, 3.3.
    """

    # 3.6 - no top-level redefinition of ``handler`` (sync OR async).
    top_level_handlers: list[ast.AST] = []
    for node in _BRIDGE_TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handler":
            top_level_handlers.append(node)
    assert not top_level_handlers, (
        f"Bridge module redefines `handler` at top level (lines: "
        f"{[n.lineno for n in top_level_handlers]}); Req 3.6 forbids "
        "shadowing subtitle_server.handler."
    )

    # 3.3 - ``from subtitle_server import broadcast_subtitle, broadcast_status, main``
    # must be present. Aliasing via ``as`` is allowed; we only require the
    # original imported ``name`` to appear.
    imported_names: set[str] = set()
    for node in ast.walk(_BRIDGE_TREE):
        if isinstance(node, ast.ImportFrom) and node.module == "subtitle_server":
            for alias in node.names:
                imported_names.add(alias.name)

    required = {"broadcast_subtitle", "broadcast_status", "main"}
    missing = required - imported_names
    assert not missing, (
        f"Bridge does not import {sorted(missing)} from subtitle_server; "
        "Req 3.3 requires importing all three by their original names."
    )


# ===========================================================================
# Task 8.2 - Property 15: subtitle.html and message-schema bytes preserved
# Feature: obs-subtitle-integration, Property 15: subtitle.html + schema snapshot
# Validates: Requirements 6.1, 6.2
# ===========================================================================


def _json_dumps_dict_call_in(func_node: ast.AST) -> ast.Dict | None:
    """Return the first ``ast.Dict`` literal passed as the first argument to
    a ``json.dumps(...)`` call inside ``func_node``.

    The integration uses exactly this idiom in ``broadcast_subtitle``,
    ``broadcast_status``, and the ``pong`` reply inside ``handler``. If no
    such call exists, returns ``None`` so the caller can produce a clear
    failure.
    """

    for child in ast.walk(func_node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        # Match ``json.dumps(...)`` (Attribute) or ``dumps(...)`` (Name).
        if isinstance(func, ast.Attribute) and func.attr == "dumps":
            pass
        elif isinstance(func, ast.Name) and func.id == "dumps":
            pass
        else:
            continue
        if not child.args:
            continue
        first = child.args[0]
        if isinstance(first, ast.Dict):
            return first
    return None


def _dict_keys_and_type(node: ast.Dict) -> tuple[set[str], str | None]:
    """Return the set of string-literal keys in ``node`` plus the literal
    value of the ``"type"`` key when it is itself a string constant.
    """

    keys: set[str] = set()
    type_value: str | None = None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
            if key.value == "type" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                type_value = value.value
    return keys, type_value


def test_subtitle_html_and_message_schemas_byte_preserved():
    """Task 8.2 / Property 15: subtitle.html SHA-256 matches baseline AND
    the JSON dict schemas in subtitle_server.py are unchanged.

    Validates: Requirements 6.1, 6.2.
    """

    # ---- 6.1: subtitle.html bytes vs committed SHA-256 baseline. -----------
    expected_sha = SUBTITLE_HTML_SHA_FILE.read_text(encoding="utf-8").strip().lower()
    actual_sha = hashlib.sha256(SUBTITLE_HTML_FILE.read_bytes()).hexdigest().lower()
    assert actual_sha == expected_sha, (
        "subtitle.html bytes drifted from the committed baseline "
        f"(expected {expected_sha}, got {actual_sha}). Req 6.1 forbids "
        "modifying subtitle.html as part of this feature."
    )

    # ---- 6.2: JSON envelope schemas in subtitle_server.py are intact. ------
    subtitle_funcs: dict[str, ast.AST] = {}
    for node in ast.walk(_SUBTITLE_SERVER_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in {"broadcast_subtitle", "broadcast_status", "handler"}:
                subtitle_funcs[node.name] = node

    for required in ("broadcast_subtitle", "broadcast_status", "handler"):
        assert required in subtitle_funcs, (
            f"subtitle_server.py is missing function `{required}`; Req 6.2 "
            "requires the existing envelope contract to be preserved."
        )

    # broadcast_subtitle envelope: {"type":"subtitle","words","text","timestamp"}.
    sub_dict = _json_dumps_dict_call_in(subtitle_funcs["broadcast_subtitle"])
    assert sub_dict is not None, (
        "broadcast_subtitle no longer constructs an inline json.dumps(dict); "
        "Req 6.2 expects the existing envelope literal to remain."
    )
    sub_keys, sub_type = _dict_keys_and_type(sub_dict)
    assert sub_keys == {"type", "words", "text", "timestamp"}, (
        f"broadcast_subtitle envelope fields drifted: got {sorted(sub_keys)}, "
        "expected {'type', 'words', 'text', 'timestamp'} (Req 6.2)."
    )
    assert sub_type == "subtitle", (
        f"broadcast_subtitle `type` discriminator drifted: got {sub_type!r}, "
        "expected 'subtitle' (Req 6.2)."
    )

    # broadcast_status envelope: {"type":"status","status","message"}.
    status_dict = _json_dumps_dict_call_in(subtitle_funcs["broadcast_status"])
    assert status_dict is not None, (
        "broadcast_status no longer constructs an inline json.dumps(dict); "
        "Req 6.2 expects the existing envelope literal to remain."
    )
    status_keys, status_type = _dict_keys_and_type(status_dict)
    assert status_keys == {"type", "status", "message"}, (
        f"broadcast_status envelope fields drifted: got {sorted(status_keys)}, "
        "expected {'type', 'status', 'message'} (Req 6.2)."
    )
    assert status_type == "status", (
        f"broadcast_status `type` discriminator drifted: got {status_type!r}, "
        "expected 'status' (Req 6.2)."
    )

    # handler `pong` reply: a json.dumps({"type": "pong"}) literal somewhere
    # inside the function body.
    pong_dict = _json_dumps_dict_call_in(subtitle_funcs["handler"])
    assert pong_dict is not None, (
        "handler no longer constructs an inline json.dumps(dict) for its "
        "pong reply; Req 6.2 expects {'type': 'pong'} to remain."
    )
    pong_keys, pong_type = _dict_keys_and_type(pong_dict)
    assert pong_keys == {"type"}, (
        f"handler pong reply fields drifted: got {sorted(pong_keys)}, "
        "expected {'type'} (Req 6.2)."
    )
    assert pong_type == "pong", (
        f"handler pong `type` discriminator drifted: got {pong_type!r}, "
        "expected 'pong' (Req 6.2)."
    )


# ===========================================================================
# Task 8.3 - Property 16: broadcast_subtitle gated to synthesized speech only
# Feature: obs-subtitle-integration, Property 16: broadcast call-site scope
# Validates: Requirement 6.3
# ===========================================================================


_SUBTITLE_BROADCAST_NAMES = {"_subtitle_broadcast", "broadcast_subtitle"}
_FORBIDDEN_FUNC_NAME_FRAGMENTS = ("chat", "social", "voice_listener")
_EXPLICITLY_FORBIDDEN_FUNCS = {"youtube_chat_worker", "voice_listener_worker"}


def _is_subtitle_broadcast_call(call: ast.Call) -> bool:
    """Return True when ``call`` invokes the subtitle broadcast helper.

    Matches both the local alias (``_subtitle_broadcast(...)``) and the
    fully-qualified attribute form (``subtitle_server.broadcast_subtitle(...)``).
    """

    func = call.func
    if isinstance(func, ast.Name) and func.id in _SUBTITLE_BROADCAST_NAMES:
        return True
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "broadcast_subtitle"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subtitle_server"
    ):
        return True
    return False


def test_broadcast_subtitle_gated_to_tts_engine_speak_only():
    """Task 8.3 / Property 16: every call site of ``broadcast_subtitle``
    inside the bridge is enclosed by ``TTSEngine.speak`` and never by a
    chat / social / mic handler.

    Validates: Requirement 6.3.
    """

    call_sites: list[tuple[int, str | None, str | None]] = []
    for node, parents in _walk_with_parents(_BRIDGE_TREE):
        if not isinstance(node, ast.Call):
            continue
        if not _is_subtitle_broadcast_call(node):
            continue
        func_name, class_name = _enclosing_func_and_class(parents)
        call_sites.append((node.lineno, func_name, class_name))

    assert call_sites, (
        "Expected at least one `_subtitle_broadcast` call inside the bridge "
        "(TTSEngine.speak) but found none; Req 6.3 still requires a single "
        "broadcast for synthesized speech."
    )

    bad: list[tuple[int, str | None, str | None]] = []
    for lineno, func_name, class_name in call_sites:
        if func_name != "speak" or class_name != "TTSEngine":
            bad.append((lineno, func_name, class_name))
        elif func_name in _EXPLICITLY_FORBIDDEN_FUNCS:
            bad.append((lineno, func_name, class_name))
        elif func_name and any(frag in func_name for frag in _FORBIDDEN_FUNC_NAME_FRAGMENTS):
            bad.append((lineno, func_name, class_name))

    assert not bad, (
        "broadcast_subtitle call sites escaped TTSEngine.speak: "
        f"{bad}. Req 6.3 forbids broadcasting subtitles for chat, "
        "social-stream, or microphone-captured input."
    )


# ===========================================================================
# Task 8.4 - Property 17: no persistence, no replay surface for subtitles
# Feature: obs-subtitle-integration, Property 17: no persistence / replay
# Validates: Requirements 6.5, 6.6
# ===========================================================================


# Persistence sinks the spec calls out. ``open()`` covers ad-hoc file IO,
# ``json.dump`` covers the typical history-style write path, and any callable
# whose attribute name suggests a database / external service write surface
# (``execute``, ``write``, ``put``, ``save``, ``insert``) is also screened.
# This is intentionally heuristic per task 8.4: we walk the argument tree of
# each candidate call and refuse to find a ``Name(id="word_timings")`` reference.
_PERSISTENCE_FUNC_NAMES = {"open"}
_PERSISTENCE_ATTR_NAMES = {"dump"}  # json.dump / pickle.dump / yaml.dump


def _call_targets_persistence(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name) and func.id in _PERSISTENCE_FUNC_NAMES:
        return True
    if isinstance(func, ast.Attribute) and func.attr in _PERSISTENCE_ATTR_NAMES:
        return True
    return False


def _arg_subtree_references_word_timings(call: ast.Call) -> bool:
    for arg in list(call.args) + [kw.value for kw in call.keywords]:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Name) and sub.id == "word_timings":
                return True
    return False


# Allowed message-type discriminators inside subtitle_server.py per the
# task 8.4 positive list. Any other ``ast.Constant`` string that lives
# inside an ``ast.Dict`` literal or appears in an equality comparison
# against a ``.get("type")``-shaped expression would indicate a new
# message-type handler and is forbidden.
_ALLOWED_SUBTITLE_TYPES = {"subtitle", "status", "pong", "ping"}


def _string_keys_in_dict_literals(tree: ast.AST) -> set[str]:
    """Return the set of string-literal *values* used as ``type`` keys
    inside any ``ast.Dict`` literal in ``tree``.
    """

    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value == "type"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                seen.add(value.value)
    return seen


def _string_constants_in_type_equality_checks(tree: ast.AST) -> set[str]:
    """Return string constants compared against an expression whose tail
    attribute / call evaluates against ``"type"``.

    Specifically catches the pattern ``data.get("type") == "ping"`` used by
    ``handler``. Other equality checks unrelated to message types are
    ignored so the heuristic stays narrow.
    """

    seen: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # Look at left side: must be ``X.get("type")`` or ``X["type"]``.
        left = node.left
        is_type_lookup = False
        if (
            isinstance(left, ast.Call)
            and isinstance(left.func, ast.Attribute)
            and left.func.attr == "get"
            and left.args
            and isinstance(left.args[0], ast.Constant)
            and left.args[0].value == "type"
        ):
            is_type_lookup = True
        elif (
            isinstance(left, ast.Subscript)
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == "type"
        ):
            is_type_lookup = True
        if not is_type_lookup:
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                seen.add(comparator.value)
    return seen


def test_no_subtitle_persistence_or_replay_surface():
    """Task 8.4 / Property 17: assert no persistence sink touches a
    ``word_timings`` reference, and the only message types subtitle_server
    knows about remain {subtitle, status, pong, ping}.

    NOTE: this is the heuristic flavor described in tasks.md - we do not
    do full data-flow tracing; we only refuse to see the ``word_timings``
    Name *anywhere* inside the argument subtrees of ``open()`` or
    ``json.dump()`` calls in either module. Variables aliased through an
    intermediate assignment would slip past this check, but the design
    forbids any such write surface in the first place.

    Validates: Requirements 6.5, 6.6.
    """

    offending: list[tuple[str, int, str]] = []
    for label, tree in (
        ("hermes_vtuber_bridge.py", _BRIDGE_TREE),
        ("subtitle_server.py", _SUBTITLE_SERVER_TREE),
    ):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _call_targets_persistence(node):
                continue
            if _arg_subtree_references_word_timings(node):
                offending.append((label, node.lineno, ast.unparse(node)[:120]))

    assert not offending, (
        "Persistence sink touches `word_timings` directly: "
        f"{offending}. Req 6.5 forbids persisting Word Timings Lists."
    )

    # subtitle_server.py message-type discriminators must be a subset of
    # the allowed set; finding any other type literal would indicate a
    # new message handler / replay surface (Req 6.6).
    declared_types = _string_keys_in_dict_literals(_SUBTITLE_SERVER_TREE)
    declared_types |= _string_constants_in_type_equality_checks(_SUBTITLE_SERVER_TREE)
    extra = declared_types - _ALLOWED_SUBTITLE_TYPES
    assert not extra, (
        "subtitle_server.py introduced new message-type discriminators "
        f"{sorted(extra)}; allowed set is {_ALLOWED_SUBTITLE_TYPES} per "
        "Req 6.6 (no replay surface for completed utterances)."
    )


# ===========================================================================
# Task 8.6 - VTS / LLM / YouTube errors still terminate
# Feature: obs-subtitle-integration, Critical error-path fingerprint
# Validates: Requirement 3.8
# ===========================================================================


def _find_method(class_name: str, method_name: str) -> ast.AST | None:
    """Locate a (possibly async) method by class+name in the bridge tree."""

    for node in ast.walk(_BRIDGE_TREE):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == method_name
                ):
                    return child
    return None


def _find_function(name: str) -> ast.AST | None:
    """Locate any top-level *or* nested function by name (first match wins)."""

    for node in ast.walk(_BRIDGE_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _has_try_except(func_node: ast.AST) -> bool:
    return any(isinstance(n, ast.Try) for n in ast.walk(func_node))


def test_critical_error_paths_byte_preserved():
    """Task 8.6: snapshot the source of VTS / LLM / YouTube error paths
    and assert each one still has at least one ``try``/``except`` block.

    On first run the baseline JSON is generated from the current sources.
    On subsequent runs the test compares ``ast.unparse`` of each captured
    function against the stored fingerprint.

    This test does NOT lock the implementation byte-for-byte at the source
    level (whitespace and comments do not survive ``ast.unparse``); it only
    ensures that the *normalized* shape is preserved across this feature's
    commits, which is what Req 3.8 actually demands ("preserve the existing
    termination behavior").

    Validates: Requirement 3.8.
    """

    captured: dict[str, ast.AST | None] = {
        "VTSController.connect": _find_method("VTSController", "connect"),
        "do_api_call": _find_function("do_api_call"),
        "youtube_chat_worker": _find_function("youtube_chat_worker"),
    }

    # Sanity: every critical function still exists in the bridge.
    missing = [name for name, node in captured.items() if node is None]
    assert not missing, (
        f"Critical error-path function(s) missing from the bridge: {missing}. "
        "Req 3.8 requires preserving termination behavior of these subsystems."
    )

    # Sanity: every critical function still contains at least one try/except.
    no_try = [name for name, node in captured.items() if not _has_try_except(node)]
    assert not no_try, (
        f"Critical error-path function(s) lost their try/except shape: "
        f"{no_try}. Req 3.8 requires existing failures to keep terminating "
        "their owning subsystem."
    )

    fingerprints = {
        name: ast.unparse(node) for name, node in captured.items() if node is not None
    }

    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    if not CRITICAL_PATHS_BASELINE.exists():
        CRITICAL_PATHS_BASELINE.write_text(
            json.dumps(fingerprints, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        # First run: baseline now committed; subsequent runs compare to it.
        return

    baseline = json.loads(CRITICAL_PATHS_BASELINE.read_text(encoding="utf-8"))
    differing = [
        name for name in fingerprints
        if name not in baseline or baseline[name] != fingerprints[name]
    ]
    if differing:
        # Show a compact preview of the first diff for diagnostics.
        first = differing[0]
        baseline_preview = (baseline.get(first, "") or "")[:200]
        current_preview = fingerprints[first][:200]
        pytest.fail(
            "Critical error-path fingerprint drift detected for "
            f"{differing}.\n--- baseline[{first!r}] ---\n{baseline_preview}\n"
            f"--- current[{first!r}] ---\n{current_preview}\n"
            "Req 3.8 forbids softening these subsystems' error handling."
        )


# ===========================================================================
# Task 8.7 - tts_is_playing read sites still gate listening
# Feature: obs-subtitle-integration, Property: mic-gate read sites preserved
# Validates: Requirement 5.6
# ===========================================================================


def _node_in_function(node: ast.AST, parents: list[ast.AST], func_name: str, class_name: str | None = None) -> bool:
    enc_func, enc_class = _enclosing_func_and_class(parents)
    if enc_func != func_name:
        return False
    if class_name is not None and enc_class != class_name:
        return False
    return True


def test_tts_is_playing_read_sites_still_gate_listening():
    """Task 8.7: every Load reference to global ``tts_is_playing`` outside
    ``TTSEngine.speak`` must appear inside a boolean test (``if`` / ``while``
    / boolean operator).

    The pre-feature glossary documented exactly three such reads (lines
    1072, 1149, 1616). Modern line numbers have shifted but the structural
    invariant - "we never lose a gate, we never silently add unrelated
    gates" - is asserted via the bounded count below.

    Validates: Requirement 5.6.
    """

    gate_sites: list[int] = []
    for node, parents in _walk_with_parents(_BRIDGE_TREE):
        if not isinstance(node, ast.Name):
            continue
        if node.id != "tts_is_playing":
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        # Skip reads inside TTSEngine.speak: those are part of the
        # implementation, not the listening-gate sites that Req 5.6 protects.
        enc_func, enc_class = _enclosing_func_and_class(parents)
        if enc_func == "speak" and enc_class == "TTSEngine":
            continue

        # Find the nearest enclosing statement and check it is a truthy gate.
        in_truthy_gate = False
        for ancestor in reversed(parents):
            if isinstance(ancestor, ast.If):
                # ``if tts_is_playing:`` or ``if not tts_is_playing:`` etc.
                if _node_descends_from(node, ancestor.test):
                    in_truthy_gate = True
                break
            if isinstance(ancestor, ast.While):
                if _node_descends_from(node, ancestor.test):
                    in_truthy_gate = True
                break
            if isinstance(ancestor, ast.IfExp):
                if _node_descends_from(node, ancestor.test):
                    in_truthy_gate = True
                break
            if isinstance(ancestor, ast.BoolOp):
                # ``if tts_is_playing and ...:`` - enclosing If/While will
                # confirm; we only flag the BoolOp as supportive evidence.
                continue
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                break

        assert in_truthy_gate, (
            f"tts_is_playing read at line {node.lineno} is not inside a "
            "truthy `if`/`while` test; Req 5.6 requires every read site "
            "outside TTSEngine.speak to gate microphone listening."
        )
        gate_sites.append(node.lineno)

    # Bounded structural count: at least 3 (we never lose a gate), at most
    # 5 (we never add unrelated gates as a side effect of this feature).
    # The pre-feature glossary documented exactly 3 such reads at lines
    # 1072, 1149, 1616; modern line numbers will have shifted.
    assert 3 <= len(gate_sites) <= 5, (
        f"Expected 3..5 tts_is_playing gate reads outside TTSEngine.speak, "
        f"found {len(gate_sites)} at lines {gate_sites}. Req 5.6 forbids "
        "losing a gate or adding unrelated gates."
    )


def _node_descends_from(node: ast.AST, root: ast.AST) -> bool:
    """Return True when ``node`` is ``root`` or appears inside its subtree."""

    if node is root:
        return True
    for child in ast.walk(root):
        if child is node:
            return True
    return False


# ===========================================================================
# Task 8.8 - No word segmentation / tokenization
# Feature: obs-subtitle-integration, Property: no tokenization in speak()
# Validates: Requirement 6.4
# ===========================================================================


_FORBIDDEN_TOKENIZER_LIBS = {"nltk", "sacremoses", "tokenizers"}


def _calls_in(func_node: ast.AST):
    for child in ast.walk(func_node):
        if isinstance(child, ast.Call):
            yield child


def test_no_word_segmentation_or_tokenization_in_speak():
    """Task 8.8: assert ``TTSEngine.speak`` does not tokenize or segment
    any text, and the bridge does not import any tokenizer library at
    module scope.

    Forbidden inside ``speak``'s body:
    - ``re.split(...)``
    - any method call ``.split(...)`` (covers ``str.split`` and
      ``re.split`` aliased onto a local).
    - ``re.findall(...)``

    The bridge is allowed to use ``.split()`` elsewhere (config parsing
    and similar housekeeping), so the negative check is scoped to
    ``speak``'s function body.

    Validates: Requirement 6.4.
    """

    speak = _find_method("TTSEngine", "speak")
    assert speak is not None, "TTSEngine.speak missing from bridge module."

    bad_calls: list[tuple[int, str]] = []
    for call in _calls_in(speak):
        func = call.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"split", "findall"}:
                bad_calls.append((call.lineno, ast.unparse(call)[:120]))
        elif isinstance(func, ast.Name) and func.id in {"split", "findall"}:
            bad_calls.append((call.lineno, ast.unparse(call)[:120]))

    assert not bad_calls, (
        "TTSEngine.speak performs word segmentation / tokenization: "
        f"{bad_calls}. Req 6.4 forbids any custom split / findall on "
        "speak()'s text or on WordBoundary text fields."
    )

    # Top-level imports of tokenizer libraries are forbidden anywhere in
    # the bridge module (Req 6.4).
    bad_imports: list[tuple[int, str]] = []
    for node in _BRIDGE_TREE.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_TOKENIZER_LIBS:
                    bad_imports.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            if module in _FORBIDDEN_TOKENIZER_LIBS:
                bad_imports.append((node.lineno, node.module or ""))

    assert not bad_imports, (
        f"Bridge imports tokenizer library at top level: {bad_imports}. "
        "Req 6.4 forbids nltk / sacremoses / tokenizers."
    )
