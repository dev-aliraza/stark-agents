"""Fetching a URL over plain HTTP, with the guards a model-driven fetcher needs.

Two of these are not optional. A model can be told to fetch any URL — including one that
came off a web page it just read — so the fetcher refuses anything that is not public
http/https, and caps how much it will read. Without the first, a tool that "just fetches a
URL" is a way to reach the cloud metadata endpoint at 169.254.169.254 or a service on
localhost. Without the second, one large file exhausts memory.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 20.0
MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5

# Chosen so sites serve the same HTML they serve a browser; many return a stub otherwise.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 stark-agents/1.0"
)

ALLOWED_SCHEMES = ("http", "https")
TEXTUAL_TYPES = ("text/html", "application/xhtml", "text/plain", "application/xml", "text/xml")

# Sent on every page request, not just set as a client default: a caller may pass its own
# client, and losing the User-Agent means some sites answer with a stub instead of the page.
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(Exception):
    """The URL could not be fetched, with a reason worth showing the model."""


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    content_type: str = ""


def _is_public_host(host: str) -> bool:
    """Whether a hostname resolves only to public addresses.

    Resolution happens here rather than being left to the HTTP client so a private target
    is refused before any connection is made. A hostname that resolves to several
    addresses has to have all of them public, since which one gets used is not ours to
    decide.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve host '{host}': {exc}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False

    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return False
    return True


def check_url(url: str, allow_private: bool = False) -> str:
    """Validate a URL before fetching it. Returns the normalised URL."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetchError(
            f"only {' and '.join(ALLOWED_SCHEMES)} URLs can be fetched, got '{url}'"
        )
    if not parsed.hostname:
        raise FetchError(f"'{url}' has no host")
    if not allow_private and not _is_public_host(parsed.hostname):
        raise FetchError(
            f"'{parsed.hostname}' is not a public address. Local and private-network "
            "targets are refused; set STARK_WEBSEARCH_ALLOW_PRIVATE=1 to permit them."
        )
    return parsed.geturl()


def build_client(timeout: float = DEFAULT_TIMEOUT, **kwargs):
    """An httpx.AsyncClient configured the way every request here wants it."""
    import httpx

    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        headers=dict(PAGE_HEADERS),
        **kwargs,
    )


async def fetch_html(url: str, client=None, allow_private: bool = False) -> FetchResult:
    """GET a URL and return its markup.

    A redirect can land somewhere the original URL was not, so the final URL is checked
    too — otherwise the private-address guard is one redirect away from being bypassed.
    """
    import httpx

    target = check_url(url, allow_private=allow_private)

    owned = client is None
    client = client or build_client()
    try:
        response = await client.get(target, headers=PAGE_HEADERS)
    except httpx.HTTPError as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    final = str(response.url)
    if final != target:
        check_url(final, allow_private=allow_private)

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and not any(content_type.startswith(kind) for kind in TEXTUAL_TYPES):
        raise FetchError(
            f"{final} is {content_type}, not a web page. This tool reads text; use a "
            "different tool for binary content."
        )

    body = response.content[:MAX_BYTES]
    encoding = response.encoding or "utf-8"
    try:
        html = body.decode(encoding, errors="replace")
    except LookupError:
        html = body.decode("utf-8", errors="replace")

    return FetchResult(url=final, status=response.status_code, html=html, content_type=content_type)
