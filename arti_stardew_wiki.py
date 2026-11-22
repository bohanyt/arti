"""Bounded, fail-closed Stardew wiki reference lookup (Issue #173).

This is a library boundary only: no network client, provider, game/native
control, browser, shell, save access, or global cache lives here. Callers
inject a fetcher that exposes redirects one response at a time. Returned wiki
text is untrusted reference data, never executable authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html.parser import HTMLParser
import ipaddress
import re
import time
from typing import Callable, Mapping, MutableMapping, Sequence
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

ALLOWED_WIKI_HOSTS = frozenset({"stardewvalleywiki.com", "wiki.stardewvalley.net"})
MAX_PAGES = 2
MAX_EXCERPT_BYTES = 12 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_REDIRECTS = 4
MAX_URL_CHARS = 2048
MAX_QUESTION_CHARS = 2048
MAX_TITLE_CHARS = 256
MAX_VERSION_CHARS = 256
DEFAULT_CACHE_TTL_SECONDS = 3600.0

# Issue #133 section 15. Selection is from this operator-owned index only;
# model text is never converted into a URL, search query or arbitrary title.
WIKI_DOMAIN_INDEX = (
    ("Controls", ("controls", "control", "kontrol", "direction", "arah")),
    ("Inventory", ("inventory", "inventori")),
    ("Tools", ("tools", "tool", "alat")),
    ("Multiplayer", ("multiplayer", "farmhand", "co-op")),
    ("Farming", ("farming", "bertani", "pertanian")),
    ("Crops", ("crops", "crop", "tanaman")),
    ("Pierre%27s_General_Store", ("pierre",)),
    ("Foraging", ("foraging",)), ("Mining", ("mining", "tambang")),
    ("Combat", ("combat", "pertarungan")), ("Fishing", ("fishing", "memancing")),
    ("Crafting", ("crafting",)), ("Cooking", ("cooking", "memasak")),
    ("Animals", ("animals", "animal", "ternak")),
    ("Friendship", ("friendship", "persahabatan")), ("Quests", ("quests", "quest", "misi")),
    ("Bundles", ("bundles", "bundle")), ("Carpenter%27s_Shop", ("carpenter", "robin")),
    ("Day_Cycle", ("day cycle", "siklus hari")), ("Festivals", ("festivals", "festival")),
    ("Ginger_Island", ("ginger island",)),
)


def resolve_stardew_wiki_pages(question: str) -> tuple[tuple[str, ...], str | None]:
    """Resolve named domain titles, not route facts or model-provided URLs.

    Unknown/overbroad questions require refinement. This first composition
    deliberately stops at indexed articles; deeper link discovery is not a
    crawler fallback and requires a separate bounded resolver extension.
    """
    reason = _question_reason(question)
    if reason:
        return (), reason
    if re.search(r"(?:https?://|www\.)", question, re.I):
        return (), "question_url_forbidden"
    words = " " + re.sub(r"[^\w'-]+", " ", question.casefold()) + " "
    titles = [title for title, aliases in WIKI_DOMAIN_INDEX
              if any(" " + alias + " " in words for alias in aliases)]
    if not titles:
        return (), "reference_topic_unavailable"
    if len(titles) > MAX_PAGES:
        return (), "reference_topic_ambiguous"
    return tuple("https://stardewvalleywiki.com/" + title for title in titles), None

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_IGNORE = frozenset({"script", "style", "noscript", "nav", "aside", "footer", "header", "form", "svg", "canvas", "template"})
_NOISE = frozenset({"navbox", "navigation", "sidebar", "infobox", "toc", "mw-editsection", "printfooter", "catlinks", "mw-jump-link"})
_BOUNDARY = frozenset({"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "blockquote", "pre", "br", "hr"})
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"(?:^|\s)[a-z]:[\\/]", r"\\\\[^\s]+[\\/]",
        r"/(?:home|users|mnt|private|etc|var)/[^\s]+", r"\bappdata[\\/]",
        r"\bconfig_local\.json\b", r"\bsavegameinfo\b",
        r"(?:^|[\s;&])(?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?cookie)\s*[:=]",
        r"\bauthorization\s*:\s*(?:bearer|basic)\b",
        r"\b(?:sk|ghp|github_pat)_[a-z0-9_-]{8,}\b",
    )
)


@dataclass(frozen=True)
class WikiFetchResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    version: str | None = None


@dataclass(frozen=True)
class WikiCacheEntry:
    title: str
    canonical_url: str
    excerpt: str
    excerpt_hash: str
    content_hash: str
    content_version: str
    checked_at: float
    content_bytes: int
    excerpt_truncated: bool
    redirect_hops: tuple[str, ...]


@dataclass(frozen=True)
class WikiPageReference:
    title: str
    canonical_url: str
    content_hash: str
    content_version: str
    checked_at: float
    content_bytes: int
    source: str
    provenance: str
    redirect_hops: tuple[str, ...]


@dataclass(frozen=True)
class WikiLookupResult:
    available: bool
    excerpt: str
    excerpt_bytes: int
    original_excerpt_bytes: int
    truncated: bool
    pages: tuple[WikiPageReference, ...]
    pages_fetched: int
    provenance: tuple[str, ...]
    unavailable_reason: str | None


class _Unavailable(Exception):
    def __init__(self, reason: str):
        self.reason = reason


class _ArticleParser(HTMLParser):
    """Extract only MediaWiki article text and a bounded title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, bool, bool, bool]] = []
        self.main_depth = self.ignore_depth = self.h1_depth = self.title_depth = 0
        self.main: list[str] = []
        self.h1: list[str] = []
        self.title_data: list[str] = []
        self.saw_main = False

    @staticmethod
    def _attrs(raw: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k.lower(): (v or "") for k, v in raw}

    @staticmethod
    def _main(attrs: Mapping[str, str]) -> bool:
        return attrs.get("id", "").lower() == "mw-content-text" or "mw-parser-output" in {x.lower() for x in attrs.get("class", "").split()}

    @staticmethod
    def _noise(attrs: Mapping[str, str]) -> bool:
        tokens: set[str] = set()
        for value in (attrs.get("id", ""), attrs.get("class", "")):
            tokens.update(x.lower() for x in value.split())
        return bool(tokens & _NOISE)

    def handle_starttag(self, tag: str, attrs_raw: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs = self._attrs(attrs_raw)
        in_parent_main = self.main_depth > 0
        starts_main = not in_parent_main and self._main(attrs)
        in_main = in_parent_main or starts_main
        starts_ignore = tag in _IGNORE or (in_main and self._noise(attrs))
        ignored = self.ignore_depth > 0 or starts_ignore
        starts_h1 = tag == "h1" and attrs.get("id", "").lower() == "firstheading"
        starts_title = tag == "title"
        if starts_main:
            self.saw_main = True
        if in_main and not ignored and tag in _BOUNDARY:
            self.main.append("\n")
        if tag in _VOID:
            return
        self.stack.append((tag, in_main, ignored, starts_h1, starts_title))
        if in_main:
            self.main_depth += 1
        if ignored:
            self.ignore_depth += 1
        if starts_h1:
            self.h1_depth += 1
        if starts_title:
            self.title_depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if (self.main_depth > 0 or self._main(self._attrs(attrs))) and self.ignore_depth == 0 and tag.lower() in _BOUNDARY:
            self.main.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        match = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if match is None:
            return
        frames = self.stack[match:]
        del self.stack[match:]
        for frame_tag, in_main, ignored, starts_h1, starts_title in reversed(frames):
            if in_main and self.main_depth:
                self.main_depth -= 1
            if ignored and self.ignore_depth:
                self.ignore_depth -= 1
            if starts_h1 and self.h1_depth:
                self.h1_depth -= 1
            if starts_title and self.title_depth:
                self.title_depth -= 1
        if self.main_depth > 0 and self.ignore_depth == 0 and tag in _BOUNDARY:
            self.main.append("\n")

    def handle_data(self, data: str) -> None:
        if self.h1_depth:
            self.h1.append(data)
        if self.title_depth:
            self.title_data.append(data)
        if self.main_depth and not self.ignore_depth:
            self.main.append(data)

    def article(self) -> tuple[str, str]:
        text = _normalize_article("".join(self.main))
        title = _inline("".join(self.h1)) or re.sub(
            r"\s*[-—]\s*Stardew Valley Wiki\s*$", "", _inline("".join(self.title_data)), flags=re.I
        )
        return title[:MAX_TITLE_CHARS].rstrip(), text


def _inline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_article(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sha(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _clip(text: str, limit: int = MAX_EXCERPT_BYTES) -> tuple[str, int, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, len(raw), False
    return raw[:limit].decode("utf-8", "ignore"), len(raw), True


def _question_reason(question: object) -> str | None:
    if not isinstance(question, str) or not question.strip():
        return "invalid_question"
    if len(question) > MAX_QUESTION_CHARS:
        return "question_too_large"
    if any(ord(c) < 32 and c not in "\t\r\n" for c in question) or any(p.search(question) for p in _PRIVATE_PATTERNS):
        return "private_context_rejected"
    return None


def _local_or_private_host(host: str) -> bool:
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any((addr.is_private, addr.is_loopback, addr.is_link_local, addr.is_reserved, addr.is_multicast, addr.is_unspecified))


def _canonical_url(url: object, *, redirect: bool = False) -> str:
    reason = "redirect_target_not_allowed" if redirect else "target_not_allowed"
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise _Unavailable(reason)
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        raise _Unavailable(reason) from None
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() != "https" or parts.username is not None or parts.password is not None:
        raise _Unavailable(reason)
    if not host or _local_or_private_host(host) or host not in ALLOWED_WIKI_HOSTS or port not in (None, 443):
        raise _Unavailable(reason)
    if parts.query or parts.fragment:
        raise _Unavailable(reason)
    try:
        path = unquote(parts.path or "/", errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise _Unavailable(reason) from None
    if "\\" in path or "\x00" in path or any(x in {".", ".."} for x in path.split("/")) or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise _Unavailable(reason)
    return urlunsplit(("https", host, quote(path or "/", safe="/-._~()"), "", ""))


def _headers(response: WikiFetchResponse) -> dict[str, str]:
    if not isinstance(response.headers, Mapping):
        raise _Unavailable("malformed_response")
    out: dict[str, str] = {}
    for key, value in response.headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _Unavailable("malformed_response")
        out[key.lower()] = value.strip()
    return out


def _fetch(start: str, fetcher: Callable[[str], WikiFetchResponse]) -> tuple[WikiFetchResponse, dict[str, str], str, tuple[str, ...]]:
    current, hops, seen = start, [start], {start}
    for redirect_count in range(MAX_REDIRECTS + 1):
        try:
            response = fetcher(current)
        except Exception:
            raise _Unavailable("fetch_failed") from None
        if not isinstance(response, WikiFetchResponse) or isinstance(response.status, bool) or not isinstance(response.status, int):
            raise _Unavailable("malformed_response")
        try:
            response_url = _canonical_url(response.url)
        except _Unavailable:
            raise _Unavailable("fetcher_url_mismatch") from None
        if response_url != current:
            raise _Unavailable("fetcher_url_mismatch")
        headers = _headers(response)
        if response.status not in _REDIRECTS:
            return response, headers, current, tuple(hops)
        if redirect_count >= MAX_REDIRECTS:
            raise _Unavailable("redirect_limit_exceeded")
        location = headers.get("location")
        if not location:
            raise _Unavailable("malformed_redirect")
        target = _canonical_url(urljoin(current, location), redirect=True)
        if target in seen:
            raise _Unavailable("redirect_loop")
        current = target
        seen.add(target)
        hops.append(target)
    raise _Unavailable("redirect_limit_exceeded")


def _article(response: WikiFetchResponse, headers: Mapping[str, str]) -> tuple[str, str, int]:
    if response.status != 200:
        raise _Unavailable("http_unavailable")
    content_type = headers.get("content-type", "").lower()
    if content_type and not (content_type.startswith("text/html") or content_type.startswith("application/xhtml+xml")):
        raise _Unavailable("unsupported_content_type")
    if headers.get("content-length"):
        try:
            declared = int(headers["content-length"])
        except ValueError:
            raise _Unavailable("malformed_response") from None
        if declared < 0:
            raise _Unavailable("malformed_response")
        if declared > MAX_RESPONSE_BYTES:
            raise _Unavailable("response_too_large")
    if not isinstance(response.body, bytes):
        raise _Unavailable("malformed_response")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise _Unavailable("response_too_large")
    try:
        html = response.body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _Unavailable("invalid_utf8") from None
    parser = _ArticleParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        raise _Unavailable("malformed_html") from None
    if not parser.saw_main:
        raise _Unavailable("main_content_missing")
    title, text = parser.article()
    if not text:
        raise _Unavailable("main_content_missing")
    if not title:
        raise _Unavailable("title_missing")
    return title, text, len(text.encode("utf-8"))


def _version(response: WikiFetchResponse, headers: Mapping[str, str], content_hash: str) -> str:
    value = response.version if response.version is not None else headers.get("etag") or headers.get("last-modified") or content_hash
    value = _inline(value) if isinstance(value, str) else content_hash
    return (value or content_hash)[:MAX_VERSION_CHARS]


def _cache_valid(entry: object, requested: str, *, now: float, ttl: float) -> bool:
    if not isinstance(entry, WikiCacheEntry):
        return False
    try:
        canonical = _canonical_url(entry.canonical_url)
        hops = tuple(_canonical_url(x) for x in entry.redirect_hops)
    except _Unavailable:
        return False
    checks = (
        bool(entry.title) and len(entry.title) <= MAX_TITLE_CHARS,
        bool(_HASH_RE.fullmatch(entry.content_hash)),
        bool(_HASH_RE.fullmatch(entry.excerpt_hash)) and _sha(entry.excerpt) == entry.excerpt_hash,
        len(entry.excerpt.encode("utf-8")) <= MAX_EXCERPT_BYTES,
        isinstance(entry.content_version, str) and bool(entry.content_version),
        isinstance(entry.checked_at, (int, float)) and not isinstance(entry.checked_at, bool),
        isinstance(entry.content_bytes, int) and not isinstance(entry.content_bytes, bool) and entry.content_bytes >= 0,
        bool(hops) and hops[-1] == canonical and requested in hops,
    )
    if not all(checks):
        return False
    return entry.checked_at <= now and now - float(entry.checked_at) <= ttl


def _page(entry: WikiCacheEntry, source: str) -> WikiPageReference:
    provenance = f"stardew_wiki:{source}"
    return WikiPageReference(entry.title, entry.canonical_url, entry.content_hash, entry.content_version, float(entry.checked_at), entry.content_bytes, source, provenance, entry.redirect_hops)


def _failure(reason: str, pages_fetched: int = 0) -> WikiLookupResult:
    return WikiLookupResult(False, "", 0, 0, False, (), pages_fetched, (), reason)


def lookup_stardew_wiki(
    question: str,
    page_urls: Sequence[str],
    *,
    fetcher: Callable[[str], WikiFetchResponse],
    cache: MutableMapping[str, WikiCacheEntry] | None = None,
    now: float | None = None,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
) -> WikiLookupResult:
    """Return bounded reference text from at most two pre-resolved wiki URLs.

    The question is admission-checked but is never interpolated into a URL or
    sent to the fetcher. The fetcher must not auto-follow redirects; every hop
    is validated here. Failures return explicit unavailable results and never
    invoke a fallback search/provider/crawler.
    """

    reason = _question_reason(question)
    if reason:
        return _failure(reason)
    if not callable(fetcher):
        return _failure("invalid_fetcher")
    if cache is not None and not isinstance(cache, MutableMapping):
        return _failure("invalid_cache")
    if isinstance(cache_ttl_seconds, bool) or not isinstance(cache_ttl_seconds, (int, float)) or cache_ttl_seconds < 0:
        return _failure("invalid_cache_ttl")
    if now is None:
        checked_at = time.time()
    elif isinstance(now, bool) or not isinstance(now, (int, float)):
        return _failure("invalid_check_time")
    else:
        checked_at = float(now)
    ttl = float(cache_ttl_seconds)

    try:
        candidates = tuple(page_urls[:MAX_PAGES])
    except (TypeError, AttributeError):
        try:
            candidates = tuple(page_urls)[:MAX_PAGES]
        except (TypeError, OverflowError):
            return _failure("invalid_page_urls")
    if not candidates:
        return _failure("reference_unavailable")
    try:
        candidates = tuple(_canonical_url(url) for url in candidates)
    except _Unavailable as exc:
        return _failure(exc.reason)

    pages: list[WikiPageReference] = []
    excerpts: list[tuple[str, str, bool]] = []
    failures: list[str] = []
    fetched = 0
    for requested in candidates:
        cached = cache.get(requested) if cache is not None else None
        if _cache_valid(cached, requested, now=checked_at, ttl=ttl):
            assert isinstance(cached, WikiCacheEntry)
            pages.append(_page(cached, "cache"))
            excerpts.append((cached.title, cached.excerpt, cached.excerpt_truncated))
            continue
        try:
            response, headers, final_url, hops = _fetch(requested, fetcher)
            fetched += 1
            title, text, content_bytes = _article(response, headers)
        except _Unavailable as exc:
            failures.append(exc.reason)
            continue
        content_hash = _sha(text)
        page_excerpt, _page_bytes, page_truncated = _clip(text)
        entry = WikiCacheEntry(
            title, final_url, page_excerpt, _sha(page_excerpt), content_hash,
            _version(response, headers, content_hash), checked_at, content_bytes,
            page_truncated, hops,
        )
        pages.append(_page(entry, "network"))
        excerpts.append((title, page_excerpt, page_truncated))
        if cache is not None:
            cache[final_url] = entry
            cache[requested] = entry

    if not pages:
        return _failure(failures[0] if failures else "reference_unavailable", fetched)
    combined = "\n\n".join(f"{title}\n{text}" for title, text, _ in excerpts)
    excerpt, original_bytes, combined_truncated = _clip(combined)
    return WikiLookupResult(
        True,
        excerpt,
        len(excerpt.encode("utf-8")),
        original_bytes,
        combined_truncated or any(truncated for _, _, truncated in excerpts),
        tuple(pages),
        fetched,
        tuple(page.provenance for page in pages),
        "partial_reference_unavailable" if failures else None,
    )
