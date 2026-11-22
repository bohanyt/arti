"""Operator monitoring must not block atomic publication of native session facts."""
import os

import pytest

from arti_runtime import ipc
from scripts.m1_operator import m1_paths


@pytest.mark.skipif(os.name != 'nt', reason='Windows delete sharing')
def test_operator_session_read_allows_publication_while_handle_is_open(tmp_path, monkeypatch):
    monkeypatch.setattr(m1_paths, 'COMMAND_DIR', tmp_path)
    path = tmp_path / 'session.json'
    ipc.write_json(path, dict(revision=1))
    original = ipc._open
    def publish_during_read(*args, **kwargs):
        stream = original(*args, **kwargs)
        try:
            ipc.write_json(path, dict(revision=2))
        except BaseException:
            stream.close()
            raise
        return stream
    with monkeypatch.context() as patch:
        patch.setattr(ipc, '_open', publish_during_read)
        assert m1_paths.read_session() == dict(revision=1)
    assert m1_paths.read_session() == dict(revision=2)
