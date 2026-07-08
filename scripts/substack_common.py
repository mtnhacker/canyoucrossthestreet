"""Substack-specific conversion shared by the RSS sync and the export importer."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, unquote

from bs4 import BeautifulSoup

from convert_common import clean_url, html_to_post, ConvertedPost, slugify

# Substack chrome: anything matching these is stripped before conversion.
_STRIP_SELECTORS = [
    ".subscribe-widget", ".subscription-widget-wrap", ".subscription-widget",
    ".button-wrapper", ".captioned-button-wrap", ".preamble",
    ".post-footer", ".publication-footer", ".footer",
    ".like-button-container", ".post-ufi", ".share-dialog",
    ".poll-embed", ".digest-post-embed", ".install-substack-app-embed",
    "[data-component-name='SubscribeWidgetToDOM']",
    "[data-component-name='ButtonCreateButton']",
    "[data-component-name='ShareNoteButton']",
    "[data-component-name='AudioEmbedPlayer']",
]
_STRIP_TEXT_BUTTONS = re.compile(
    r"^(subscribe now|share|share this post|leave a comment|refer a friend|"
    r"give a gift subscription|get more from .* in the substack app|"
    r"read .* in the substack app|thanks for reading.*subscribe for free.*)$",
    re.I)


def substack_full_size(url: str) -> str:
    """Resolve a substackcdn.com/image/fetch/... URL to the original asset."""
    if "substackcdn.com/image/fetch" not in url:
        return url
    # the original URL is the (encoded) last path segment
    m = re.search(r"/(https?[:%].+)$", url)
    if not m:
        return url
    orig = unquote(m.group(1))
    return orig if orig.startswith("http") else url


def clean_substack_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for sel in _STRIP_SELECTORS:
        for el in soup.select(sel):
            el.decompose()
    # text-only button paragraphs ("Subscribe now", "Share", ...)
    for p in soup.find_all(["p", "h4"]):
        if _STRIP_TEXT_BUTTONS.match(p.get_text(" ", strip=True) or ""):
            p.decompose()
    # unwrap image links; point <img> at the full-size original
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        img["src"] = substack_full_size(src)
        img.attrs.pop("srcset", None)
    for source in soup.find_all("source"):
        source.decompose()
    for a in soup.find_all("a", href=True):
        if "substackcdn.com/image" in a["href"]:
            a["href"] = substack_full_size(a["href"])
    return str(soup)


def convert_substack_html(html: str) -> ConvertedPost:
    return html_to_post(clean_substack_html(html), full_size=False,
                        strip_leading_hero=True)


def slug_from_substack_url(url: str) -> str:
    path = urlparse(clean_url(url)).path
    m = re.search(r"/p/([^/]+)/?$", path)
    return slugify(m.group(1)) if m else slugify(path.rsplit("/", 1)[-1] or "post")


def parse_rss_date(value: str) -> datetime:
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
