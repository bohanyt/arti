"""Non-blocking wrappers for blocking HTTP used by the bridge."""

from __future__ import annotations

import asyncio

import requests

_groq_session = requests.Session()
_gemini_session = requests.Session()
_sambanova_session = requests.Session()


def groq_session() -> requests.Session:
    return _groq_session


def gemini_session() -> requests.Session:
    return _gemini_session


def sambanova_session() -> requests.Session:
    return _sambanova_session


async def post_in_thread(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """Run requests.Session.post in a worker thread (keeps event loop responsive)."""
    return await asyncio.to_thread(session.post, url, **kwargs)
