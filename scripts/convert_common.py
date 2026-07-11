"""Shared conversion helpers for WordPress migration and Substack sync.

Everything that turns "HTML post with remote images" into "Hugo page bundle
with local images and clean Markdown" lives here, so the WordPress importer,
the Substack RSS sync, and the Substack export importer all behave the same.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup, Comment, NavigableString
from markdownify import MarkdownConverter

# Substack's CDN rejects non-browser user agents with 403, so look like one.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg"}
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "ref", "source", "publication_id", "post_id", "isFreemail",
    "r", "triedRedirect",
}
MORE_TOKEN = "@@HUGO-MORE-MARKER@@"

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def http_get(url: str, timeout: int = 30):
    """GET that survives bot-blocking CDNs (Substack/Cloudflare).

    Plain `requests` is blocked by TLS fingerprint on some CDNs regardless of
    headers, so on 403 we retry with curl_cffi impersonating Chrome.
    """
    resp = requests.get(url, timeout=timeout, headers=BROWSER_HEADERS)
    if resp.status_code == 403:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            return resp
        resp = curl_requests.get(url, timeout=timeout, impersonate="chrome")
    return resp


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text) or "post"


def clean_url(url: str) -> str:
    """Drop tracking query parameters from a link."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS]
    return urlunparse(parts._replace(query=urlencode(kept)))


def yaml_str(value: str) -> str:
    """A safely quoted YAML scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def unique_name(name: str, taken: set[str]) -> str:
    base, ext = os.path.splitext(name)
    candidate, i = name, 1
    while candidate in taken:
        i += 1
        candidate = f"{base}-{i}{ext}"
    taken.add(candidate)
    return candidate


def local_image_name(url: str, taken: set[str]) -> str:
    """Stable, readable local filename for a remote image URL."""
    path = urlparse(url).path
    base = os.path.basename(path) or "image"
    base = re.sub(r"%[0-9A-Fa-f]{2}", "-", base)
    stem, ext = os.path.splitext(base)
    if ext.lower() not in IMAGE_EXTENSIONS:
        ext = ".jpg"
    stem = slugify(stem)[:60] or "image"
    return unique_name(f"{stem}{ext}", taken)


# --------------------------------------------------------------------------
# image collection
# --------------------------------------------------------------------------

@dataclass
class ImageRef:
    url: str            # remote URL to fetch (full size)
    local: str          # filename inside the bundle
    alt: str = ""
    caption: str = ""
    kind: str = "image"  # "image" or "video"


@dataclass
class ConvertedPost:
    markdown: str
    images: list[ImageRef] = field(default_factory=list)
    hero: str | None = None          # local filename of the lead image
    warnings: list[str] = field(default_factory=list)


class _Converter(MarkdownConverter):
    """markdownify with sane defaults for prose."""

    def convert_img(self, el, text, parent_tags=None, **kwargs):
        # Images are replaced by placeholders before conversion; anything that
        # still reaches here had no usable src and is dropped.
        return ""


def _md(html_fragment: str) -> str:
    return _Converter(heading_style="ATX", bullets="-", strip=["script", "style"]).convert(html_fragment)


def full_size_wp_url(url: str) -> str:
    """Strip WordPress thumbnail suffixes: photo-1024x576.jpg -> photo.jpg"""
    return re.sub(r"-\d{2,4}x\d{2,4}(?=\.\w{3,4}$)", "", url)


def html_to_post(html: str, *, base_url: str = "",
                 full_size: bool = True,
                 strip_leading_hero: bool = True) -> ConvertedPost:
    """Convert post HTML to Markdown, extracting images as local references.

    Returns markdown where every image is a `![alt](localname "caption")`
    figure reference, plus the list of images to download.
    """
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []

    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # normalize links: absolute + tracking-free
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if base_url and href.startswith("/"):
            href = base_url.rstrip("/") + href
        a["href"] = clean_url(href)

    # drop empty divs / spans that carry no content
    for tag in soup.find_all(["div", "span"]):
        if not tag.get_text(strip=True) and not tag.find(["img", "iframe", "video"]):
            tag.decompose()

    images: list[ImageRef] = []
    taken: set[str] = set()
    placeholders: dict[str, ImageRef] = {}

    def register(img_tag, caption: str = "") -> str | None:
        src = img_tag.get("src") or img_tag.get("data-src") or ""
        if not src:
            return None
        if base_url and src.startswith("/"):
            src = base_url.rstrip("/") + src
        src = clean_url(src)
        if full_size:
            src = full_size_wp_url(src)
        ref = ImageRef(url=src,
                       local=local_image_name(src, taken),
                       alt=(img_tag.get("alt") or "").strip(),
                       caption=caption.strip())
        images.append(ref)
        token = f"@@IMG-{len(images) - 1}@@"
        placeholders[token] = ref
        return token

    # figures with captions
    for fig in soup.find_all("figure"):
        img = fig.find("img")
        if not img:
            continue
        capt_el = fig.find("figcaption")
        caption = capt_el.get_text(" ", strip=True) if capt_el else ""
        token = register(img, caption)
        if token:
            fig.replace_with(NavigableString(f"\n\n{token}\n\n"))

    # bare images (possibly wrapped in a link to the full-size file)
    for img in soup.find_all("img"):
        token = register(img)
        if not token:
            img.decompose()
            continue
        target = img
        parent = img.parent
        if parent and parent.name == "a":
            href = parent.get("href", "")
            same = os.path.basename(urlparse(full_size_wp_url(href)).path) == \
                   os.path.basename(urlparse(placeholders[token].url).path)
            if same or not href:
                target = parent
        target.replace_with(NavigableString(f"\n\n{token}\n\n"))

    # self-hosted videos: download into the bundle like images
    for video in soup.find_all("video"):
        src = video.get("src") or ""
        if not src:
            source = video.find("source")
            src = source.get("src", "") if source else ""
        if base_url and src.startswith("/"):
            src = base_url.rstrip("/") + src
        src = clean_url(src)
        if src and os.path.splitext(urlparse(src).path)[1].lower() in (".mp4", ".webm", ".mov", ".m4v"):
            base = os.path.basename(urlparse(src).path)
            stem, ext = os.path.splitext(base)
            local = unique_name(f"{slugify(stem)[:60] or 'video'}{ext.lower()}", taken)
            ref = ImageRef(url=src, local=local, kind="video")
            images.append(ref)
            token = f"@@IMG-{len(images) - 1}@@"
            placeholders[token] = ref
            video.replace_with(NavigableString(f"\n\n{token}\n\n"))
        else:
            warnings.append(f"video embed needs manual review: {src[:120] or 'no src'}")
            video.replace_with(NavigableString(
                f"\n\n[Embedded video]({src})\n\n" if src else "\n\n"))

    for iframe in soup.find_all(["iframe", "embed", "object", "audio"]):
        src = iframe.get("src", "") or ""
        warnings.append(f"embed needs manual review: {src[:120] or iframe.name}")
        iframe.replace_with(NavigableString(f"\n\n[Embedded content: {clean_url(src)}]({clean_url(src)})\n\n" if src else "\n\n"))

    markdown = _md(str(soup))

    # replace placeholders with markdown figures (or video tags)
    for token, ref in placeholders.items():
        if ref.kind == "video":
            figure = f'<video controls preload="metadata" src="{ref.local}"></video>'
        elif ref.caption:
            figure = f'![{ref.alt}]({ref.local} "{ref.caption}")'
        else:
            figure = f"![{ref.alt}]({ref.local})"
        markdown = markdown.replace(token, figure)

    # tidy whitespace
    markdown = re.sub(r"[ \t]+$", "", markdown, flags=re.M)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"

    hero = None
    stills = [r for r in images if r.kind == "image"]
    if stills:
        if strip_leading_hero:
            # if the first thing in the body is the first image, lift it out
            first_fig = re.match(
                r"^!\[[^\]]*\]\(" + re.escape(stills[0].local) + r"(?: \"[^\"]*\")?\)\s*\n+",
                markdown)
            if first_fig:
                markdown = markdown[first_fig.end():]
        hero = stills[0].local

    return ConvertedPost(markdown=markdown, images=images, hero=hero, warnings=warnings)


# --------------------------------------------------------------------------
# bundle writing
# --------------------------------------------------------------------------

def hero_caption(post: ConvertedPost) -> str:
    """Caption of the lead image, if it was lifted out of the body (so the
    caption would otherwise be lost)."""
    stills = [r for r in post.images if r.kind == "image"]
    if stills and stills[0].caption and stills[0].local not in post.markdown:
        return stills[0].caption
    return ""


def front_matter(*, title: str, date: str, slug: str, images: list[str],
                 alt: dict[str, str], places: list[str] | None = None,
                 aliases: list[str] | None = None,
                 substack_url: str | None = None,
                 substack_guid: str | None = None,
                 draft: bool = False,
                 places_todo: bool = False,
                 hero_caption: str = "") -> str:
    lines = ["---"]
    lines.append(f"title: {yaml_str(title)}")
    lines.append(f"date: {date}")
    lines.append(f"slug: {yaml_str(slug)}")
    if images:
        lines.append("images:")
        lines += [f"  - {yaml_str(i)}" for i in images]
    else:
        lines.append("images: []")
    if alt:
        lines.append("alt:")
        lines += [f"  {yaml_str(k)}: {yaml_str(v)}" for k, v in alt.items()]
    if hero_caption:
        lines.append(f"hero_caption: {yaml_str(hero_caption)}")
    if places:
        lines.append("places: [" + ", ".join(yaml_str(p) for p in places) + "]")
    elif places_todo:
        lines.append('places: []  # TODO: tag this post, e.g. ["England", "Hadrian\'s Wall"]')
    else:
        lines.append("places: []")
    if aliases:
        lines.append("aliases:")
        lines += [f"  - {yaml_str(a)}" for a in aliases]
    if substack_url:
        lines.append(f"substack_url: {yaml_str(substack_url)}")
    if substack_guid:
        lines.append(f"substack_guid: {yaml_str(substack_guid)}")
    lines.append(f"draft: {'true' if draft else 'false'}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def download_images(refs: list[ImageRef], dest: Path,
                    session=None) -> list[str]:
    """Download every image into dest. Raises on the first failure so the
    caller can abandon the whole bundle (never commit a half-broken post)."""
    saved = []
    for ref in refs:
        target = dest / ref.local
        if target.exists():
            saved.append(ref.local)
            continue
        resp = http_get(ref.url)
        if resp.status_code == 404 and full_size_wp_url(ref.url) != ref.url:
            resp = http_get(full_size_wp_url(ref.url))
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        expected = "video/" if ref.kind == "video" else "image/"
        if not (ctype.startswith(expected) or ref.local.endswith(".svg")
                or ctype in ("application/octet-stream",)):
            raise ValueError(f"unexpected content type ({ctype}): {ref.url}")
        with tempfile.NamedTemporaryFile(dir=dest, delete=False) as tmp:
            tmp.write(resp.content)
        shutil.move(tmp.name, target)
        saved.append(ref.local)
    return saved


def write_bundle(bundle_dir: Path, front: str, markdown: str) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "index.md").write_text(front + "\n" + markdown, encoding="utf-8")


def existing_guids(content_dir: Path) -> dict[str, Path]:
    """Map substack_guid -> bundle dir for every existing post."""
    guids: dict[str, Path] = {}
    for index in content_dir.glob("*/index.md"):
        text = index.read_text(encoding="utf-8")
        m = re.search(r"^substack_guid:\s*\"?([^\"\n]+)\"?\s*$", text, re.M)
        if m:
            guids[m.group(1).strip()] = index.parent
    return guids
