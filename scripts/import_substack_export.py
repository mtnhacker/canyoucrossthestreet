#!/usr/bin/env python3
"""One-off import of a Substack data export (backfill for posts older than
the ~20 the RSS feed exposes).

Get the export from Substack: Settings → Exports → "Create new export".
The zip contains posts.csv and a posts/ folder with one HTML file per post.

Usage:
    python3 scripts/import_substack_export.py --export substack-export.zip
    python3 scripts/import_substack_export.py --export exported-folder/

Uses the same conversion + GUID-dedup code as the daily sync, so posts that
already came in via RSS are skipped, and a later RSS run won't duplicate
anything imported here.
"""

from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from convert_common import download_images, existing_guids, front_matter, slugify
from substack_common import convert_substack_html

PUBLICATION = "https://canyoucrossthestreet.substack.com"


def parse_export_date(value: str) -> datetime:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value.replace("Z", "+00:00"), fmt)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True,
                    help="Substack export zip or unpacked folder")
    ap.add_argument("--content", default="content/posts")
    ap.add_argument("--include-drafts", action="store_true")
    args = ap.parse_args()

    src = Path(args.export)
    tmpdir = None
    if src.is_file():
        tmpdir = tempfile.mkdtemp(prefix="substack-export-")
        with zipfile.ZipFile(src) as zf:
            zf.extractall(tmpdir)
        src = Path(tmpdir)

    posts_csv = src / "posts.csv"
    if not posts_csv.exists():
        print(f"error: {posts_csv} not found — is this a Substack export?",
              file=sys.stderr)
        return 1

    content_dir = Path(args.content)
    content_dir.mkdir(parents=True, exist_ok=True)
    known = existing_guids(content_dir)

    new, skipped, errors = 0, 0, []
    with open(posts_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            post_id = row.get("post_id", "").strip()
            title = (row.get("title") or "Untitled").strip()
            published = (row.get("is_published") or "").lower() in ("true", "1", "yes")
            if not published and not args.include_drafts:
                continue
            if (row.get("type") or "newsletter") not in ("newsletter", "post", ""):
                continue

            # RSS GUIDs are the canonical post URL; export post_id is
            # "<number>.<slug>". Store the URL form so both paths dedup.
            slug_part = post_id.split(".", 1)[1] if "." in post_id else slugify(title)
            slug = slugify(slug_part)
            url = f"{PUBLICATION}/p/{slug}"
            if url in known or post_id in known:
                skipped += 1
                continue

            html_file = src / "posts" / f"{post_id}.html"
            if not html_file.exists():
                errors.append(f"{title}: {html_file.name} missing from export")
                continue

            date = parse_export_date(row.get("post_date", ""))
            post = convert_substack_html(html_file.read_text(encoding="utf-8"))
            stills = [r for r in post.images if r.kind == "image"]
            alt_map = {r.local: (r.alt or r.caption or title) for r in stills}
            front = front_matter(
                title=title, date=date.isoformat(), slug=slug,
                images=[r.local for r in stills], alt=alt_map,
                places_todo=True, substack_url=url, substack_guid=url,
                draft=not published)

            bundle = content_dir / f"{date.strftime('%Y-%m-%d')}-{slug}"
            tmp = bundle.with_name(bundle.name + ".tmp")
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir(parents=True)
            try:
                download_images(post.images, tmp)
                (tmp / "index.md").write_text(front + "\n" + post.markdown,
                                              encoding="utf-8")
                if bundle.exists():
                    shutil.rmtree(bundle)
                tmp.rename(bundle)
            except Exception as exc:  # noqa: BLE001
                shutil.rmtree(tmp, ignore_errors=True)
                errors.append(f"{title}: {exc} — post NOT written")
                continue
            new += 1
            known[url] = bundle

    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"import: {new} new post(s), {skipped} skipped, {len(errors)} error(s)")
    for e in errors:
        print(f"  error: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
