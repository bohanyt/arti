"""Guarded real app-server catalog inspection; never run on operator-workstation.

Uses the production startup checker. A disposable authless loopback provider
captures and rejects the request before inference. It never records headers or
prompt bodies. Exit 2 means the catalog is not search-only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def tool_names(tools, prefix=""):
    if not isinstance(tools, list):
        return ["<missing catalog>"]
    names = []
    for tool in tools:
        if tool.get("type") == "namespace":
            names.extend(tool_names(tool.get("tools"), prefix + tool["name"] + "."))
        else:
            names.append(prefix + (tool.get("name") or tool.get("type", "<unknown>")))
    return names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema-output", type=Path)
    parser.add_argument("--negative-control", action="store_true",
                        help="Deliberately add request_user_input; the startup check must reject it.")
    args = parser.parse_args()
    if "StardewModdingAPI" in subprocess.check_output(["tasklist"], text=True):
        raise SystemExit("SMAPI running")
    import openai_codex as oc
    import arti_codex_role as cr
    from arti_codex_lookup_policy import LookupToolInspector, LookupToolPolicyError

    with tempfile.TemporaryDirectory(prefix="arti-codex-role-tools-") as cwd:
        with LookupToolInspector("gpt-6-luna", timeout_sec=8) as inspector:
            overrides = cr.role_overrides("lookup") + (
                cr._lookup_model_catalog(cwd, "gpt-6-luna"), *inspector.provider_overrides())
            if args.negative_control:
                overrides += ('tools.experimental_request_user_input.enabled=true',)
            client = cr._spawn_codex(oc, oc.CodexConfig(
                codex_bin=args.codex_bin, cwd=cwd, config_overrides=overrides))
            policy = None
            error = None
            try:
                policy = inspector.inspect(
                    client, approval_mode=oc.ApprovalMode.deny_all, sandbox=oc.Sandbox.read_only,
                    cwd=cwd, base_instructions=cr.BASE_INSTRUCTIONS,
                    developer_instructions=cr.LOOKUP_INSTRUCTIONS)
            except LookupToolPolicyError as exc:
                error = str(exc)
            finally:
                client.close()
                cr.shutdown()
            names = tool_names(inspector._tools)
            result = {
                "app_server_version": subprocess.check_output([args.codex_bin, "--version"], text=True).strip(),
                "smapi_guard": "absent",
                "inspection": "real app-server request captured by production startup checker",
                "registered_tools": names, "registered_tool_count": len(names),
                "only_web_search": policy is not None, "error": error,
                "negative_control": args.negative_control,
                "web_parameter_names": list(policy.parameter_names) if policy else [],
                "tool_schema_sha256": policy.schema_sha256 if policy else None,
                "role_source_sha256": hashlib.sha256(Path(cr.__file__).read_bytes()).hexdigest(),
                "policy_source_sha256": hashlib.sha256(
                    (Path(cr.__file__).parent / "arti_codex_lookup_policy.py").read_bytes()).hexdigest(),
                "request_user_input_switch": "tools.experimental_request_user_input.enabled="
                    + str(args.negative_control).lower(),
                "apply_patch_tool_type": None,
            }
            rendered = json.dumps(result, ensure_ascii=False, indent=2)
            print(rendered)
            if args.output:
                args.output.write_text(rendered + "\n", encoding="utf-8")
            if args.schema_output:
                args.schema_output.write_text(json.dumps(inspector._tools, indent=2) + "\n", encoding="utf-8")
    return 0 if policy is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
