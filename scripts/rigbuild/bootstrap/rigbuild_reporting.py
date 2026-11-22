"""pytest plugin of the package's golden and page runs (ADR 245; the evidence folders' pkg95_reporting.py).

At session start it hands `tests.native_build` the hash-pinned C# probes the native chain just built
(`<evidence>/native-probe-cache.json`), so no pytest worker builds them again; each failure is appended to
`<evidence>/failure-alerts.jsonl` as it happens. Loaded only through PYTEST_PLUGINS by the build procedures.
"""
import hashlib
import json
import os
from pathlib import Path


def _evidence():
    value = os.environ.get("ARTI_RIGBUILD_EVIDENCE")
    return Path(value) if value else None


def pytest_sessionstart(session):
    evidence, checkout = _evidence(), os.environ.get("ARTI_RIGBUILD_CHECKOUT")
    if evidence is None or not checkout or not (evidence / "native-probe-cache.json").is_file():
        return
    from tests import native_build
    cache = json.loads((evidence / "native-probe-cache.json").read_text(encoding="utf8"))
    base = session.config._tmp_path_factory.getbasetemp()
    for project, row in cache["projects"].items():
        output = Path(row["path"])
        assert hashlib.sha256((output / (project + ".dll")).read_bytes()).hexdigest() == row["dll_sha256"]
        native_build._BUILT[((Path(checkout) / "stardew-mod" / project / (project + ".csproj")).resolve(), base)] = output


def pytest_runtest_logreport(report):
    evidence = _evidence()
    if evidence is not None and report.failed and not os.environ.get("PYTEST_XDIST_WORKER"):
        with (evidence / "failure-alerts.jsonl").open("a", encoding="utf8") as f:
            f.write(json.dumps(dict(node=report.nodeid, when=report.when, details=str(report.longrepr))) + "\n")
