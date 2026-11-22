import json
import os
from unittest.mock import patch

import pytest

from scripts.free_play import file_io


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_reader_contention_recovers_and_persistent_failure_is_bounded(winerror):
    error = OSError("sharing failure")
    error.winerror = winerror
    with patch.object(file_io, "_read_text", side_effect=[error, '{"ready":true}']) as read, \
            patch.object(file_io.time, "sleep") as sleep:
        assert file_io.read_json("snapshot") == {"ready": True}
        assert read.call_count == 2 and sleep.call_count == 1
    with patch.object(file_io, "_read_text", side_effect=error) as read, patch.object(file_io.time, "sleep") as sleep:
        with pytest.raises(OSError):
            file_io.read_json("snapshot")
        assert read.call_count == 6 and sleep.call_count == 5


def test_partial_or_missing_snapshot_is_retried_but_never_credited_after_exhaustion():
    with patch.object(file_io, "_read_text", side_effect=[FileNotFoundError(), "{", '{"saving":false}']), \
            patch.object(file_io.time, "sleep"):
        assert file_io.read_json("snapshot") == {"saving": False}
    with patch.object(file_io, "_read_text", return_value="{"), patch.object(file_io.time, "sleep"):
        with pytest.raises(json.JSONDecodeError):
            file_io.read_json("snapshot")


def test_nontransient_io_failure_is_not_retried():
    with patch.object(file_io, "_read_text", side_effect=OSError("disk failure")) as read:
        with pytest.raises(OSError):
            file_io.read_json("snapshot")
        assert read.call_count == 1


@pytest.mark.skipif(os.name != "nt", reason="real Windows sharing semantics")
def test_windows_reader_allows_atomic_replacement_and_reads_its_original_snapshot(tmp_path):
    path, temp = tmp_path / "status.json", tmp_path / "status.json.tmp"
    path.write_text('{"snapshot":"old"}', encoding="utf-8")
    temp.write_text('{"snapshot":"new"}', encoding="utf-8")
    fdopen = os.fdopen
    def replace_while_open(fd, *args, **kwargs):
        import ctypes
        from ctypes import wintypes
        stream = fdopen(fd, *args, **kwargs)
        try:
            # .NET File.Replace uses ReplaceFileW. MoveFileExW (os.replace)
            # refuses an open destination even with FILE_SHARE_DELETE on this Windows version.
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            replace = kernel.ReplaceFileW
            replace.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
            replace.restype = wintypes.BOOL
            if not replace(str(path), str(temp), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            stream.close()
            raise
        return stream
    with patch.object(file_io.os, "fdopen", side_effect=replace_while_open):
        assert file_io.read_json(path) == {"snapshot": "old"}
    assert file_io.read_json(path) == {"snapshot": "new"}
