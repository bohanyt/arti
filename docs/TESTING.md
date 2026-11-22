# Python tests

Use the project virtual environment. Install the runtime dependencies from `requirements.txt`, then the test-only
runner extensions:

    .\venv\Scripts\python.exe -m pip install -r requirements-test.txt

Parallel run:

    .\venv\Scripts\python.exe -m pytest tests -n auto --dist worksteal -q --tb=short

Serial run:

    .\venv\Scripts\python.exe -m pytest tests -q --tb=short

The project and its tests were run on Windows only. `requirements.txt` pins `onnxruntime-directml`, a Windows-only
package (the OCR of the story dubbing runs on DirectML); on Linux or macOS replace that line with the plain
`onnxruntime` of the same version before installing, and use `venv/bin/python` in place of the Windows interpreter
path. Some tests read Windows paths or call Windows tools and may fail elsewhere. Always keep pytest's exit code
when you redirect its output.

## What the tests cover

- The Python tests use inputs made for them. Some started as the shape of a real game state or session; the
  game's own text and recorded speech in them were replaced with invented text. Short game identifiers remain.
- Tests that replayed recorded sessions, provider exchanges or game captures, tests that build or read C# test
  projects, and the C# test projects themselves, are not in this repository.
- A few tests are timing-sensitive (they wait for a fake mod thread); on a busy machine run them again alone before
  treating a failure as real.
- Tests establish unit-level evidence. They do not prove behaviour in the real game.

## Timing changes

For a timing change, repeat the affected test files a few times in separate pytest runs. Unit doubles should publish
their declared observations explicitly or drive an injected clock.
