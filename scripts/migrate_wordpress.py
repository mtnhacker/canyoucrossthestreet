#!/usr/bin/env python3
"""One-off WordPress -> Hugo migration.

Reads a WordPress WXR export, writes Hugo page bundles under content/posts/,
downloads every referenced image into the bundle, preserves original URLs
(via the /:slug/ permalink scheme plus aliases when needed), and writes a
migration report + URL mapping table.

Usage:
    python3 scripts/migrate_wordpress.py --wxr export.xml            # full run
    python3 scripts/migrate_wordpress.py --wxr export.xml --no-fetch # skip image downloads
    python3 scripts/migrate_wordpress.py --wxr export.xml --image-pool DIR
                                          # take images from DIR instead of the network

The importer never touches the live WordPress site beyond plain GET requests
for images.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
from convert_common import (ConvertedPost, front_matter, full_size_wp_url,
                            html_to_post, download_images, write_bundle,
                            slugify, MORE_TOKEN)

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "wp": "http://wordpress.org/export/1.2/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
}

CAPTION_RE = re.compile(
    r"\[caption[^\]]*\](.*?)\[/caption\]", re.S | re.I)
SHORTCODE_RE = re.compile(r"\[/?([a-zA-Z][a-zA-Z0-9_-]*)(\s[^\]]*)?\]")
KNOWN_HARMLESS_SHORTCODES = {"caption"}


def preprocess_wp_html(html: str) -> str:
    """Convert WP-specific constructs into plain HTML the shared converter
    understands."""
    html = html.replace("<!--more-->", MORE_TOKEN)

    def caption_to_figure(m: re.Match) -> str:
        inner = m.group(1)
        # caption text = whatever trails the last closing tag
        text = re.sub(r"^.*>", "", inner, flags=re.S).strip()
        markup = inner[: len(inner) - len(text)] if text else inner
        return f"<figure>{markup}<figcaption>{text}</figcaption></figure>"

    html = CAPTION_RE.sub(caption_to_figure, html)
    # WP double-newline paragraphs (classic editor stores bare text)
    if "<p>" not in html and "<!-- wp:" not in html:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", html) if b.strip()]
        html = "\n".join(
            b if b.startswith("<") else f"<p>{b}</p>" for b in blocks)
    return html


def leftover_shortcodes(text: str) -> list[str]:
    found = []
    for m in SHORTCODE_RE.finditer(text):
        name = m.group(1).lower()
        if name not in KNOWN_HARMLESS_SHORTCODES:
            found.append(m.group(0)[:60])
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wxr", required=True)
    ap.add_argument("--content", default="content/posts")
    ap.add_argument("--report", default="docs/migration-report.md")
    ap.add_argument("--url-map", default="scripts/url_map.csv")
    ap.add_argument("--no-fetch", action="store_true",
                    help="write bundles and an image manifest, skip downloads")
    ap.add_argument("--image-pool", default=None,
                    help="directory with pre-downloaded images (bundle/file paths)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite existing bundles")
    args = ap.parse_args()

    content_dir = Path(args.content)
    content_dir.mkdir(parents=True, exist_ok=True)

    tree = ET.parse(args.wxr)
    channel = tree.getroot().find("channel")
    base_url = channel.findtext("wp:base_site_url", namespaces=NS) or ""
    items = channel.findall("item")

    # attachment id -> (url, alt)
    attachments: dict[str, tuple[str, str]] = {}
    for it in items:
        if it.findtext("wp:post_type", namespaces=NS) != "attachment":
            continue
        pid = it.findtext("wp:post_id", namespaces=NS)
        url = it.findtext("wp:attachment_url", namespaces=NS) or ""
        alt = ""
        for meta in it.findall("wp:postmeta", NS):
            if meta.findtext("wp:meta_key", namespaces=NS) == "_wp_attachment_image_alt":
                alt = (meta.findtext("wp:meta_value", namespaces=NS) or "").strip()
        if pid and url:
            attachments[pid] = (url, alt)

    posts = [it for it in items
             if it.findtext("wp:post_type", namespaces=NS) == "post"]

    migrated, skipped, review = [], [], []
    url_map: list[tuple[str, str, str]] = []
    manifest: dict[str, dict[str, str]] = {}

    for it in posts:
        status = it.findtext("wp:status", namespaces=NS)
        if status not in ("publish", "draft"):
            continue
        title = (it.findtext("title") or "Untitled").strip()
        is_draft = status == "draft"
        slug = it.findtext("wp:post_name", namespaces=NS) or slugify(title)
        date_gmt = it.findtext("wp:post_date_gmt", namespaces=NS) or ""
        if not date_gmt or date_gmt.startswith("0000"):
            date_gmt = it.findtext("wp:post_date", namespaces=NS) or "1970-01-01 00:00:00"
        date_iso = date_gmt.replace(" ", "T") + "+00:00"
        day = date_gmt.split(" ")[0]
        link = it.findtext("link") or ""
        old_path = urlparse(link).path if link else ""

        bundle = content_dir / f"{day}-{slug}"
        if bundle.exists() and not args.force:
            skipped.append((title, "bundle exists"))
            continue

        raw_html = it.findtext("content:encoded", namespaces=NS) or ""
        html = preprocess_wp_html(raw_html)

        post: ConvertedPost = html_to_post(html, base_url=base_url,
                                           strip_leading_hero=False)

        # featured image becomes the hero (first in images)
        thumb_id = None
        for meta in it.findall("wp:postmeta", NS):
            if meta.findtext("wp:meta_key", namespaces=NS) == "_thumbnail_id":
                thumb_id = (meta.findtext("wp:meta_value", namespaces=NS) or "").strip()
        hero_local = None
        if thumb_id and thumb_id in attachments:
            url, alt = attachments[thumb_id]
            url = full_size_wp_url(url)
            existing = next((r for r in post.images
                             if full_size_wp_url(r.url) == url), None)
            if existing:
                # already in the body: promote it to the front of the list
                post.images.remove(existing)
                post.images.insert(0, existing)
                hero_local = existing.local
                # drop its body occurrence only when it leads the post
                lead = re.match(
                    r"^!\[[^\]]*\]\(" + re.escape(existing.local) + r"(?: \"[^\"]*\")?\)\s*\n+",
                    post.markdown)
                if lead:
                    post.markdown = post.markdown[lead.end():]
            else:
                from convert_common import ImageRef, local_image_name
                taken = {r.local for r in post.images}
                ref = ImageRef(url=url, local=local_image_name(url, taken), alt=alt)
                post.images.insert(0, ref)
                hero_local = ref.local
        else:
            first_still = next((r for r in post.images if r.kind == "image"), None)
            if first_still:
                hero_local = first_still.local
                lead = re.match(
                    r"^!\[[^\]]*\]\(" + re.escape(hero_local) + r"(?: \"[^\"]*\")?\)\s*\n+",
                    post.markdown)
                if lead:
                    post.markdown = post.markdown[lead.end():]

        markdown = post.markdown.replace(MORE_TOKEN, "<!--more-->")

        flags = list(post.warnings)
        # scan the *source HTML* for unhandled WP shortcodes ([gallery] etc.);
        # scanning markdown would false-positive on every [link](url)
        flags += [f"shortcode left in content: {s}"
                  for s in leftover_shortcodes(CAPTION_RE.sub("", raw_html))]
        if not post.images:
            flags.append("no images found in post")

        new_path = f"/{slug}/"
        aliases = []
        # drafts have no public URL to preserve (WXR gives them "/")
        if not is_draft and old_path not in ("", "/"):
            if old_path != new_path:
                aliases.append(old_path)
            url_map.append((old_path, new_path, "same" if old_path == new_path else "alias"))

        stills = [r for r in post.images if r.kind == "image"]
        alt_map = {r.local: (r.alt or r.caption or title) for r in stills}
        front = front_matter(
            title=title, date=date_iso, slug=slug,
            images=[r.local for r in stills], alt=alt_map,
            aliases=aliases or None, draft=is_draft, places_todo=True)

        if args.no_fetch:
            write_bundle(bundle, front, markdown)
            manifest[str(bundle)] = {r.local: r.url for r in post.images}
            migrated.append((title, str(bundle), len(post.images), flags, is_draft))
            if flags:
                review.append((title, flags))
            continue

        # fetch (or copy) images into a temp bundle, then move into place
        tmp_bundle = bundle.with_name(bundle.name + ".tmp")
        if tmp_bundle.exists():
            shutil.rmtree(tmp_bundle)
        tmp_bundle.mkdir(parents=True)
        try:
            if args.image_pool:
                pool = Path(args.image_pool)
                for r in post.images:
                    src = pool / bundle.name / r.local
                    if not src.exists():
                        raise FileNotFoundError(f"{src} missing from image pool")
                    shutil.copy2(src, tmp_bundle / r.local)
            else:
                download_images(post.images, tmp_bundle)
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(tmp_bundle, ignore_errors=True)
            skipped.append((title, f"image fetch failed: {exc}"))
            continue
        (tmp_bundle / "index.md").write_text(front + "\n" + markdown, encoding="utf-8")
        if bundle.exists():
            shutil.rmtree(bundle)
        tmp_bundle.rename(bundle)
        migrated.append((title, str(bundle), len(post.images), flags, is_draft))
        if flags:
            review.append((title, flags))

    # ---- outputs -------------------------------------------------------
    Path(args.url_map).parent.mkdir(parents=True, exist_ok=True)
    with open(args.url_map, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["old_path", "new_path", "method"])
        w.writerows(url_map)

    if args.no_fetch:
        Path("scripts/_migration_images.json").write_text(
            json.dumps(manifest, indent=2))

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# WordPress migration report", ""]
    pub = [m for m in migrated if not m[4]]
    dr = [m for m in migrated if m[4]]
    lines.append(f"Migrated **{len(pub)} published** posts and {len(dr)} drafts; "
                 f"{sum(m[2] for m in migrated)} images referenced; "
                 f"{len(skipped)} skipped.")
    lines += ["", "## Posts", "", "| Post | Bundle | Images | Draft | Needs review |", "|---|---|---|---|---|"]
    for title, bundle, n, flags, is_draft in migrated:
        lines.append(f"| {title} | `{bundle}` | {n} | {'yes' if is_draft else ''} | "
                     f"{'; '.join(flags) if flags else ''} |")
    if skipped:
        lines += ["", "## Skipped", ""]
        lines += [f"- {t}: {why}" for t, why in skipped]
    lines += ["", "## URL mapping (old → new)", "",
              "| Old path | New path | How |", "|---|---|---|"]
    for old, new, how in url_map:
        lines.append(f"| `{old}` | `{new}` | {how} |")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"migrate: {len(migrated)} migrated ({len(pub)} published, {len(dr)} drafts), "
          f"{len(skipped)} skipped, {len(review)} flagged for review. "
          f"Report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
