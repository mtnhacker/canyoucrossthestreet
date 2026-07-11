# canyoucrossthestreet.com

The permanent home of *Can you cross the street?* — a travel blog where each
post is one photo and the story behind it. Posts are written and emailed via
[Substack](https://canyoucrossthestreet.substack.com); this repo is the owned
archive on the author's domain, built with Hugo and deployed to GitHub Pages.
It updates itself: a daily GitHub Action pulls new Substack posts into the
repo, and every push to `main` rebuilds and redeploys the site.

## How it fits together

```
Substack (writing + email + discussion)
   │  RSS feed, checked daily
   ▼
.github/workflows/sync.yml ──▶ scripts/sync_substack.py
   │  new posts become content/posts/YYYY-MM-DD-slug/ page bundles
   │  (images downloaded into the bundle, Substack chrome stripped)
   ▼
git commit + push to main
   ▼
.github/workflows/deploy.yml ──▶ Hugo build ──▶ GitHub Pages
                                  └─ scripts/check_urls.py gate
```

- **Theme**: `themes/crossing/` — custom, photo-first, serif (Fraunces for
  question-titles, Literata for body; both self-hosted in
  `themes/crossing/static/fonts/`). No third-party theme to keep updated.
- **Content model**: every post is a page bundle
  `content/posts/YYYY-MM-DD-slug/index.md` with its images alongside.
  Front matter: `title`, `date`, `slug`, `images` (list), `alt` (map of
  filename → alt text), `hero_caption` (optional), `places` (taxonomy),
  `substack_url`, `substack_guid`, `draft`, `aliases` (for preserved
  WordPress URLs).
- **URLs**: posts publish at `/<slug>/` (see `[permalinks]` in `hugo.toml`),
  which is exactly the old WordPress structure — that's how every migrated
  URL keeps working. The migration details live in
  [`docs/migration-report.md`](docs/migration-report.md).
- **Identity**: `substack_guid` is the key. The sync skips any feed item
  whose GUID already exists in a bundle, so edits and retitles on Substack
  never create duplicates.

## Running things locally

You need Hugo (extended) ≥ 0.146 and Python 3.10+.

```bash
# preview the site (includes the draft fixture post)
hugo server -D

# install script dependencies
pip install -r requirements.txt

# pull new posts from the live Substack feed
python3 scripts/sync_substack.py

# verify every preserved WordPress URL against a local build
hugo build
python3 scripts/check_urls.py --build public
```

The sync is safe to run any number of times: a second run makes no changes,
and if any image download fails, that post is not written at all (nothing
half-broken ever lands in the repo) — the failure is reported so the next
run can retry.

### Testing the sync pipeline offline

`scripts/fixtures/` contains a Substack-shaped feed plus images. This is how
the fixture post (`2026-07-06-fixture-crossing-test`, kept as a draft)
was produced:

```bash
python3 scripts/sync_substack.py \
  --feed-file scripts/fixtures/feed-fixture.xml \
  --image-pool scripts/fixtures/image-pool
```

## Everyday tasks

### Tag a synced post with places

The sync leaves `places: []  # TODO: ...` in each new post. Edit the bundle's
`index.md`:

```yaml
places: ["England", "Hadrian's Wall"]
```

Commit and push; the post then appears under `/places/england/` etc.

### Write a site-only post (never touched Substack)

Create a bundle by hand — no `substack_url`/`substack_guid`:

```
content/posts/2026-08-01-my-site-only-post/
├── index.md
└── photo.jpg
```

```yaml
---
title: "Can you cross this street?"
date: 2026-08-01T09:00:00-06:00
slug: "my-site-only-post"
images: ["photo.jpg"]
alt:
  "photo.jpg": "Describe the photo for screen readers"
places: []
draft: false
---

The story goes here. The first image in `images` becomes the full-width
photo at the top of the page.
```

The end-of-post note automatically drops the "discussion on Substack" link
when there's no `substack_url` and just offers the subscribe link. (That
wording lives in one place:
`themes/crossing/layouts/_partials/substack-note.html`.)

### Change the canonical-URL policy

`hugo.toml` → `[params] canonicalMode`, one of:

- `"none"` (current): post pages emit no `rel=canonical`
- `"substack"`: posts with a `substack_url` point their canonical at the
  Substack original
- `"self"`: posts declare themselves canonical

### Backfill older Substack posts

**Known limitation:** the RSS feed only exposes roughly the 20 most recent
items, so the daily sync can only ever see recent posts. If posts older than
that need importing (e.g. before this repo existed), use Substack's export:
Substack → Settings → Exports → "Create new export", download the zip, then:

```bash
python3 scripts/import_substack_export.py --export substack-export.zip
```

It shares the sync's conversion + GUID dedup code, so nothing gets imported
twice no matter which path a post arrived by.

## How the daily sync actually runs

Substack's CDN blocks requests from GitHub's server IP ranges (the feed
returns 403 no matter what the request looks like), so the daily sync runs
**on the author's Mac** via launchd instead of in GitHub Actions:

- `scripts/sync-local.sh` — pulls, runs the sync in a local venv, commits
  as `sync: <post title(s)>`, and pushes (which triggers the deploy).
- `scripts/com.canyoucrossthestreet.sync.plist` — launchd agent that runs
  the script daily at 9:17 AM local time. If the Mac is asleep at that
  moment, launchd runs it on the next wake. Install once with:

  ```bash
  cp scripts/com.canyoucrossthestreet.sync.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.canyoucrossthestreet.sync.plist
  ```

  Logs land in `/tmp/canyoucrossthestreet-sync.log`. To run a sync right
  now: `bash scripts/sync-local.sh`.

## GitHub Actions

- **`sync.yml`** — manual dispatch only (kept for debugging; it will 403
  from GitHub's network — that's Substack's IP block, not a bug).
- **`deploy.yml`** — on every push to `main`: installs pinned Hugo
  (extended 0.164.0), builds with `--minify`, runs the URL-preservation
  check as a gate, and deploys to GitHub Pages. `static/CNAME` carries the
  custom domain.

One-time repo setup: Settings → Pages → Source: **GitHub Actions**, and add
`canyoucrossthestreet.com` as the custom domain (with *Enforce HTTPS* once
the certificate is issued).

## DNS: pointing the domain at GitHub Pages

The domain is registered/hosted at DreamHost and currently serves the old
WordPress site. **Nothing below needs to happen until cutover day** — the
repo builds and deploys happily to `mtnhacker.github.io/canyoucrossthestreet`
in the meantime.

At DreamHost (Panel → Websites → Manage Websites → DNS, or Domains → Manage
Domains → DNS):

1. Remove the existing `A` record(s) pointing the bare domain at DreamHost
   web hosting.
2. Add four `A` records for the apex (`canyoucrossthestreet.com`):

   ```
   185.199.108.153
   185.199.109.153
   185.199.110.153
   185.199.111.153
   ```

3. Optionally add the matching `AAAA` records for IPv6:
   `2606:50c0:8000::153`, `2606:50c0:8001::153`,
   `2606:50c0:8002::153`, `2606:50c0:8003::153`.
4. Point `www` at the Pages host with a `CNAME` record:
   `www → mtnhacker.github.io.`
5. In the GitHub repo: Settings → Pages → Custom domain →
   `canyoucrossthestreet.com` → Save, then tick *Enforce HTTPS* once the
   certificate check passes (can take up to an hour after DNS propagates).

DreamHost keeps hosting email/anything else on the domain untouched — only
the web `A`/`CNAME` records change.

### DNS-cutover checklist

Flip DNS only after all of these:

- [ ] `deploy.yml` green on `main` (build + URL check passing)
- [ ] Migration report reviewed (`docs/migration-report.md`)
- [ ] Redirect spot-check: pick a handful of old WordPress URLs (including
      one that had an inbound 301 from the even-older site) and confirm they
      resolve on the Pages URL: `https://mtnhacker.github.io/canyoucrossthestreet/<old-path>`
      (or run `scripts/check_urls.py` against a local build)
- [ ] Custom domain added in repo Settings → Pages (do this before DNS so
      the CNAME is claimed and squatting is impossible)
- [ ] Flip the DreamHost DNS records as above
- [ ] After propagation: `https://canyoucrossthestreet.com/` serves the Hugo
      site over HTTPS; spot-check an old URL end to end
- [ ] Leave the WordPress site untouched for a while as a fallback — nothing
      on it was modified during migration

## Repo map

```
hugo.toml                     site config (permalinks, taxonomy, canonicalMode)
content/posts/                one folder per post (bundle: index.md + images)
content/about.md              About page (stub — content to come)
content/archive.md            archive page grouped by year
themes/crossing/              the custom theme (layouts, css, fonts)
scripts/
  sync_substack.py            daily RSS → repo sync
  import_substack_export.py   one-off backfill from a Substack export zip
  migrate_wordpress.py        the one-off WordPress migration (already run)
  check_urls.py               asserts every old URL still resolves
  convert_common.py           shared HTML→Markdown/bundle machinery
  substack_common.py          Substack-specific cleanup shared by both importers
  url_map.csv                 old → new URL mapping table
  fixtures/                   offline test feed + images for the sync pipeline
docs/migration-report.md      what was migrated, flagged items, URL table
.github/workflows/            sync.yml (daily) + deploy.yml (Pages)
```
