"""Restricted public Naver article text retrieval, without HTML/image storage."""

from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

import httpx

from .extraction import ExtractionError

MAX_BYTES = 2 * 1024 * 1024
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_HIDDEN = {"script", "style", "noscript", "template"}
_BREAKS = {"p", "div", "br", "li", "section", "article", "h1", "h2", "h3", "h4", "tr"}


def canonical_url(url: str) -> str:
    """Discard RSS tracking parameters; do not accept arbitrary destinations."""
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        raise ExtractionError("body_url_unsupported") from None
    if (parts.scheme != "https" or parts.netloc not in {"blog.naver.com", "m.blog.naver.com"}
            or not re.fullmatch(r"/hycj1025/[0-9]+", parts.path)
            or any(ord(c) < 33 or c == "\\" for c in url)):
        raise ExtractionError("body_url_unsupported")
    return "https://blog.naver.com" + parts.path


class ArticleText(HTMLParser):
    """Extract only a recognized article container, excluding active markup."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.found = False
        self.complete = False

    def handle_starttag(self, tag, attrs):
        if self.complete:
            return
        attributes = dict(attrs)
        if not self.stack:
            if self.found or not (
                "se-main-container" in (attributes.get("class") or "").split()
                or attributes.get("id") == "postViewArea"
            ):
                return
            self.found = True
        if tag in _BREAKS and not any(t in _HIDDEN for t in self.stack):
            self.parts.append(" ")
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        index = len(self.stack) - 1 - self.stack[::-1].index(tag)
        del self.stack[index:]
        if not self.stack:
            self.complete = True
        if tag in _BREAKS and not any(t in _HIDDEN for t in self.stack):
            self.parts.append(" ")

    def handle_data(self, data):
        if self.stack and not any(t in _HIDDEN for t in self.stack):
            self.parts.append(data)


def extract_text(html: str) -> str:
    parser = ArticleText()
    parser.feed(html)
    parser.close()
    text = " ".join("".join(parser.parts).split())
    if not parser.found or not parser.complete or not text:
        raise ExtractionError("body_content_unavailable")
    return text


def fetch_body(url: str) -> tuple[str, str]:
    """Fetch one public mobile article; redirects/login gates are not bypassed."""
    canonical = canonical_url(url)
    mobile = canonical.replace("https://blog.naver.com/", "https://m.blog.naver.com/", 1)
    try:
        with httpx.stream("GET", mobile, timeout=20, follow_redirects=False) as response:
            if response.is_redirect:
                raise ExtractionError("body_redirect_blocked")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ExtractionError("body_content_type_invalid")
            chunks = []
            size = 0
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ExtractionError("body_too_large")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            try:
                html = b"".join(chunks).decode(encoding, errors="strict")
            except (UnicodeError, LookupError):
                raise ExtractionError("body_encoding_invalid") from None
    except httpx.HTTPStatusError as exc:
        raise ExtractionError(f"body_http_{exc.response.status_code}") from None
    except httpx.RequestError:
        raise ExtractionError("body_network_error") from None
    return canonical, extract_text(html)
