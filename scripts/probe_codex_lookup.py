"""One bounded, manual Codex lookup probe. Run only on idle game-workstation."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--timeout-sec", type=float, default=9.0)
    args = parser.parse_args()
    tasks = subprocess.check_output(["tasklist"], text=True)
    if "StardewModdingAPI" in tasks:
        raise SystemExit("SMAPI running; probe refused")
    import arti_codex_role as cr
    from arti_web_lookup import call_codex_search
    cfg = {"codex_agent_enabled": True, "web_lookup_codex_enabled": True,
           "codex_bin": args.codex_bin,
           "web_lookup_codex_model": "gpt-6-luna", "web_lookup_codex_effort": "low",
           "web_lookup_codex_timeout_sec": args.timeout_sec, "telemetry_enabled": False}
    stages = {}
    def factory(**kw):
        t0 = time.monotonic()
        client = cr._real_factory(role="lookup", **kw)
        stages["startup_sec"] = round(time.monotonic() - t0, 3)
        class Traced:
            close = client.close
            lookup_tool_policy = client.lookup_tool_policy
            def thread_start(self, **kwargs):
                thread = client.thread_start(**kwargs)
                class Thread:
                    def run(self, *a, **k):
                        result = thread.run(*a, **k)
                        print(json.dumps({"items": [getattr(getattr(i, "root", i), "type", None)
                                                     for i in result.items],
                                          "raw_answer": result.final_response}, ensure_ascii=False))
                        return result
                return Thread()
        return Traced()
    cr._client_factory = factory
    started = time.monotonic()
    try:
        r = call_codex_search("harga emas hari ini", cfg)
        print(json.dumps({"latency_sec": round(time.monotonic() - started, 3), **stages,
                          "model": r.model, "search_performed": r.search_performed,
                          "snippet": r.content, "sources": [s.__dict__ for s in r.sources]},
                         ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"latency_sec": round(time.monotonic() - started, 3),
                          **stages, "error": str(exc)}))
        raise
    finally:
        cr.shutdown()


if __name__ == "__main__":
    main()
