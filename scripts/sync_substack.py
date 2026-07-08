#!/usr/bin/env python3
"""Pull new posts from the Substack RSS feed into content/posts/.

- Identity key is the feed item GUID (stored as substack_guid in front
  matter), so retitles/edits on Substack never create duplicates.
- Idempotent: running twice changes nothing.
- Atomic per post: if any image download fails, nothing is written for that
  post and the failure is reported.

Usage:
    python3 scripts/sync_substack.py                  # against the live feed
    python3 scripts/sync_substack.py --feed-file f.xml --image-pool DIR
                                                      # offline / testing

Known limitation (see README): the RSS feed only exposes the ~20 most recent
items. Older posts must come in via Substack's export tool and
scripts/import_substack_export.py.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from convert_common import (clean_url, download_images, existing_guids,
                            front_matter, hero_caption)
from substack_common import (convert_substack_html, parse_rss_date,
                             slug_from_substack_url)

DEFAULT_FEED = "https://canyoucrossthestreet.substack.com/feed"
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}


def sync(feed_xml: str, content_dir: Path, image_pool: str | None = None) -> tuple[int, int, list[str]]:
    channel = ET.fromstring(feed_xml).find("channel")
    items = channel.findall("item") if channel is not None else []
    known = existing_guids(content_dir)

    new, skipped, errors = 0, 0, []
    new_titles = []
    for item in items:
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "Untitled").strip()
        link = clean_url((item.findtext("link") or "").strip())
        if not guid:
            errors.append(f"{title}: item has no GUID, skipped")
            continue
        if guid in known:
            skipped += 1
            continue

        date = parse_rss_date(item.findtext("pubDate") or "")
        slug = slug_from_substack_url(link)
        html = item.findtext("content:encoded", namespaces=NS) or ""
        if not html.strip():
            errors.append(f"{title}: feed item has no content "
                          "(paywalled post?), skipped")
            continue

        post = convert_substack_html(html)
        stills = [r for r in post.images if r.kind == "image"]
        alt_map = {r.local: (r.alt or r.caption or title) for r in stills}
        front = front_matter(
            title=title,
            date=date.isoformat(),
            slug=slug,
            images=[r.local for r in stills],
            alt=alt_map,
            places_todo=True,
            substack_url=link,
            substack_guid=guid,
            hero_caption=hero_caption(post),
        )

        bundle = content_dir / f"{date.strftime('%Y-%m-%d')}-{slug}"
        tmp = bundle.with_name(bundle.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        try:
            if image_pool:
                for r in post.images:
                    src = Path(image_pool) / bundle.name / r.local
                    if not src.exists():
                        raise FileNotFoundError(f"{src} missing from image pool")
                    shutil.copy2(src, tmp / r.local)
            else:
                download_images(post.images, tmp)
            for w in post.warnings:
                errors.append(f"{title}: {w} (post still synced)")
            (tmp / "index.md").write_text(front + "\n" + post.markdown,
                                          encoding="utf-8")
            if bundle.exists():
                shutil.rmtree(bundle)
            tmp.rename(bundle)
        except Exception as exc:  # noqa: BLE001 - report, write nothing
            shutil.rmtree(tmp, ignore_errors=True)
            errors.append(f"{title}: {exc} — post NOT written")
            continue
        new += 1
        new_titles.append(title)
        known[guid] = bundle

    for t in new_titles:
        print(f"NEW: {t}")
    return new, skipped, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed-url", default=DEFAULT_FEED)
    ap.add_argument("--feed-file", help="read the feed from a file (testing)")
    ap.add_argument("--content", default="content/posts")
    ap.add_argument("--image-pool",
                    help="directory with pre-downloaded images (testing)")
    args = ap.parse_args()

    if args.feed_file:
        feed_xml = Path(args.feed_file).read_text(encoding="utf-8")
    else:
        resp = requests.get(args.feed_url, timeout=30,
                            headers={"User-Agent": "canyoucrossthestreet-sync/1.0"})
        resp.raise_for_status()
        feed_xml = resp.text

    content_dir = Path(args.content)
    content_dir.mkdir(parents=True, exist_ok=True)
    new, skipped, errors = sync(feed_xml, content_dir, args.image_pool)

    print(f"sync: {new} new post(s), {skipped} skipped, {len(errors)} error(s)")
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if errors and new == 0 and skipped == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
