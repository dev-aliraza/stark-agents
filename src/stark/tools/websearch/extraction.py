"""HTML to readable text, using only the standard library.

A model cannot use `page.content()`: 200KB of markup says almost nothing and costs a
fortune in context. What it needs is the article — headings, paragraphs, list items — and
the links, so it can decide where to go next.

There are better extractors (trafilatura, readability-lxml). This one is deliberately
dependency-free so that `websearch_search` and `websearch_open` work off a plain HTTP fetch,
with no browser binary and nothing to install beyond httpx. That is the difference between
"pip install and go" and "pip install, then download a 150MB Chromium".

The heuristic is simple and stated rather than clever: drop the chrome (`nav`, `header`,
`footer`, `aside`, `form`, `script`, `style`), keep the rest, and mark block boundaries so
the text does not run together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# Never contributes to readable text.
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "audio",
        "video",
        "map",
        "select",
        "option",
        "textarea",
    }
)

# Page furniture. Dropping these is what turns a page into an article.
#
# `form` is deliberately NOT here. ASP.NET and several CMS templates wrap the entire page
# body in a single <form>, so skipping it discards everything — measured on a real travel
# site: 20,744 characters of article, gone. The interactive children that a form actually
# contributes (input, select, textarea, option) are in SKIP_TAGS already.
BOILERPLATE_TAGS = frozenset({"nav", "header", "footer", "aside"})

# Below this, treat the extraction as having failed and try again without stripping. Any
# heuristic that drops whole subtrees can drop the wrong one — an article inside an <aside>,
# an unclosed <footer> that swallows the rest of the document — and returning nothing when
# the page plainly has text is the one outcome worth spending a second parse to avoid.
MIN_USEFUL_CHARS = 200

# Force a line break so paragraphs and list items do not run into each other.
BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "caption", "dd", "div", "dl", "dt",
        "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "main",
        "ol", "p", "pre", "section", "table", "tbody", "td", "th", "thead", "tr", "ul",
    }
)

HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}

# Links to nowhere useful.
_DEAD_HREF = re.compile(r"^\s*(#|javascript:|mailto:|tel:|data:)", re.IGNORECASE)
_MULTI_SPACE = re.compile(r"[ \t ]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")

DEFAULT_MAX_CHARS = 40_000
DEFAULT_MAX_LINKS = 40


@dataclass
class Link:
    text: str
    url: str


@dataclass
class Document:
    """The readable form of one page."""

    url: str = ""
    title: str = ""
    description: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    truncated: bool = False
    # True when boilerplate stripping was skipped because it had removed everything.
    recovered: bool = False

    def as_payload(self, max_links: int = DEFAULT_MAX_LINKS) -> dict:
        """The shape a tool returns to the model."""
        payload = {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "links": [{"text": link.text, "url": link.url} for link in self.links[:max_links]],
        }
        if self.description:
            payload["description"] = self.description
        if self.truncated:
            payload["truncated"] = True
        if len(self.links) > max_links:
            payload["links_omitted"] = len(self.links) - max_links
        return payload


class _Reader(HTMLParser):
    """Walks the document once, collecting text, headings and links."""

    def __init__(self, base_url: str = "", strip_boilerplate: bool = True):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._dropped = SKIP_TAGS | BOILERPLATE_TAGS if strip_boilerplate else SKIP_TAGS
        self.title = ""
        self.description = ""
        self.links: list[Link] = []
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._seen_links: set[str] = set()

    # --- helpers ----------------------------------------------------------------------

    def _emit(self, text: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(text)

    def _absolute(self, href: str) -> str | None:
        if _DEAD_HREF.match(href):
            return None
        url = urljoin(self.base_url, href.strip()) if self.base_url else href.strip()
        return url if urlparse(url).scheme in ("http", "https") else None

    # --- HTMLParser ---------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._dropped:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            values = dict(attrs)
            name = (values.get("name") or values.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = _clean_inline(values.get("content") or "")
        elif tag in HEADING_TAGS:
            self._emit(f"\n\n{'#' * HEADING_TAGS[tag]} ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._link_href = self._absolute(href)
            self._link_text = []
        elif tag in BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._dropped:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = False
        elif tag == "a":
            self._close_link()
        elif tag in HEADING_TAGS or tag in BLOCK_TAGS:
            self._emit("\n")

    def _close_link(self) -> None:
        href, self._link_href = self._link_href, None
        text = _clean_inline("".join(self._link_text))
        self._link_text = []
        if href and text and href not in self._seen_links:
            self._seen_links.add(href)
            self.links.append(Link(text=text[:160], url=href))

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link_text.append(data)
        self._emit(data)

    def text(self) -> str:
        return _collapse("".join(self._chunks))


def _clean_inline(text: str) -> str:
    return _MULTI_SPACE.sub(" ", text.replace("\n", " ")).strip()


def _collapse(text: str) -> str:
    """Normalise whitespace without losing paragraph structure."""
    lines = [_MULTI_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    return _MULTI_NEWLINE.sub("\n\n", "\n".join(lines)).strip()


def _read(html: str, url: str, strip_boilerplate: bool) -> _Reader:
    """One parse pass. Never raises: `HTMLParser` is lenient, and a page that still breaks
    it should degrade to less text rather than failing the tool call."""
    reader = _Reader(base_url=url, strip_boilerplate=strip_boilerplate)
    try:
        reader.feed(html)
        reader.close()
    except Exception:  # a hostile or truncated page must not take the tool down
        pass
    return reader


def extract(html: str, url: str = "", max_chars: int = DEFAULT_MAX_CHARS) -> Document:
    """Turn a page's HTML into a `Document`.

    Strips page furniture first. If that leaves nothing useful, parses again without
    stripping and keeps whichever pass found more — see `MIN_USEFUL_CHARS`.
    """
    reader = _read(html, url, strip_boilerplate=True)
    text = reader.text()
    recovered = False

    if len(text) < MIN_USEFUL_CHARS:
        unstripped = _read(html, url, strip_boilerplate=False)
        candidate = unstripped.text()
        if len(candidate) > len(text):
            reader, text, recovered = unstripped, candidate, True

    truncated = len(text) > max_chars
    if truncated:
        text = f"{text[:max_chars].rstrip()}\n\n[truncated at {max_chars} characters]"

    title = _clean_inline(reader.title) or _first_heading(text)

    return Document(
        url=url,
        title=title,
        description=reader.description,
        text=text,
        links=reader.links,
        truncated=truncated,
        recovered=recovered,
    )


def _first_heading(text: str) -> str:
    """Fall back to the first markdown heading when there is no <title>."""
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return ""
