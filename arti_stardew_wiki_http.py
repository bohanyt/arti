"""Fixed-header HTTPS transport for explicitly enabled Stardew wiki retrieval.

No environment proxies, cookies, auth session, automatic redirect/retry or model
dependency. The runtime owns request admission; the wiki library owns redirects.
"""
from __future__ import annotations

from http.client import HTTPSConnection, HTTPResponse
import ipaddress
import socket
import time
from urllib.parse import urlsplit

from arti_stardew_wiki import MAX_RESPONSE_BYTES, WikiFetchResponse, _canonical_url, _Unavailable

_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
            "User-Agent": "ARTI-Stardew-Wiki/1.0"}
_RESPONSE_HEADERS = frozenset({"location", "content-type", "content-length", "content-encoding"})


class _DeadlineReader:
    def __init__(self, reader, sock, clock, deadline):
        self._reader, self._socket = reader, sock
        self._clock, self._deadline = clock, deadline

    def _read(self, method, size):
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("wiki_response_deadline")
        self._socket.settimeout(min(3.0, remaining))
        data = getattr(self._reader, method)(size)
        if self._clock() > self._deadline:
            raise TimeoutError("wiki_response_deadline")
        return data

    def readline(self, size=-1):
        # Read lines incrementally too: buffered readline may otherwise wait
        # indefinitely for a slow trickle that never violates an idle timeout.
        data = bytearray()
        while size < 0 or len(data) < size:
            item = self._read("read1", 1)
            if not item:
                break
            data.extend(item)
            if item == b"\n":
                break
        return bytes(data)

    def read1(self, size):
        return self._read("read1", size)

    def read(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.read1(size - len(data))
            if not part:
                break
            data.extend(part)
        return bytes(data)

    def close(self):
        self._reader.close()


class _PublicWikiConnection(HTTPSConnection):
    def set_read_deadline(self, clock, deadline):
        self._clock, self._deadline = clock, deadline
        def response(sock, **kwargs):
            value = HTTPResponse(sock, **kwargs)
            value.fp = _DeadlineReader(value.fp, sock, clock, deadline)
            return value
        self.response_class = response

    def connect(self):
        # Resolve once, reject non-public destinations, then connect to that
        # literal address. TLS still verifies the allowlisted hostname via SNI.
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if self._clock() >= self._deadline:
            raise TimeoutError("wiki_resolver_deadline")
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global
                                or ipaddress.ip_address(a[4][0]).is_multicast for a in addresses):
            raise ValueError("wiki_address_not_public")
        family, kind, proto, _, address = addresses[0]
        raw = socket.socket(family, kind, proto)
        try:
            raw.settimeout(min(self.timeout, self._deadline - self._clock()))
            raw.connect(address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class WikiHTTPFetcher:
    """One GET/connection, <=512KiB, socket timeouts and a response deadline.

    DNS uses the operating system resolver; its latency cannot be preempted by
    Python socket timeouts. No retry or alternate address fallback is attempted.
    Construction performs no network I/O.
    """
    def __init__(self, *, connection_factory=_PublicWikiConnection, clock=time.monotonic):
        self._connection_factory = connection_factory
        self._clock = clock

    def __call__(self, url: str) -> WikiFetchResponse:
        try:
            canonical = _canonical_url(url)
        except _Unavailable as exc:
            raise ValueError("wiki_target_not_allowed") from exc
        parts = urlsplit(canonical)
        deadline = self._clock() + 12.0
        connection = self._connection_factory(host=parts.hostname, timeout=3.0)
        if isinstance(connection, _PublicWikiConnection):
            connection.set_read_deadline(self._clock, deadline)
        response = None
        try:
            connection.request("GET", parts.path, headers=dict(_HEADERS))
            transport_socket = connection.sock
            response = connection.getresponse()
            raw_headers = response.getheaders()
            if sum(len(k) + len(v) for k, v in raw_headers) > 16 * 1024:
                raise ValueError("wiki_headers_too_large")
            headers = {k.lower(): v for k, v in raw_headers if k.lower() in _RESPONSE_HEADERS}
            body = bytearray()
            if response.status == 200:
                if headers.get("content-encoding", "identity").lower() != "identity":
                    raise ValueError("wiki_encoding_not_allowed")
                if "content-length" in headers:
                    size = int(headers["content-length"])
                    if not 0 <= size <= MAX_RESPONSE_BYTES:
                        raise ValueError("wiki_response_too_large")
                while True:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise TimeoutError("wiki_response_deadline")
                    if transport_socket is not None:
                        transport_socket.settimeout(min(3.0, remaining))
                    chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ValueError("wiki_response_too_large")
            if self._clock() > deadline:
                raise TimeoutError("wiki_response_deadline")
            if response.status == 200 and "content-length" in headers and len(body) != int(headers["content-length"]):
                raise ValueError("wiki_incomplete_response")
            return WikiFetchResponse(response.status, canonical, headers, bytes(body))
        finally:
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()
