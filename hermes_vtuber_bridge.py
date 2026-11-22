"""Compat shim (one cycle): `hermes_vtuber_bridge` was renamed to `arti_bridge`.

Aliases the module object itself (not `import *`) so monkeypatching module
globals through either name hits the same module.
"""
import importlib
import sys

if __name__ == "__main__":
    import runpy

    runpy.run_module("arti_bridge", run_name="__main__", alter_sys=True)
else:
    sys.modules[__name__] = importlib.import_module("arti_bridge")
