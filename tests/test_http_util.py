"""Tests for arti_http_util."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import arti_http_util as http_util


def test_post_in_thread_delegates_to_session():
    mock_session = MagicMock()
    mock_session.post.return_value = MagicMock(status_code=200)

    resp = asyncio.run(
        http_util.post_in_thread(mock_session, "https://example.com", json={"x": 1})
    )
    assert resp.status_code == 200
    mock_session.post.assert_called_once()
