#!/usr/bin/env python3
"""Verify every migrated URL still resolves against a local Hugo build.

Serves the build directory over HTTP and requests each old path from
scripts/url_map.csv, asserting either a 200 with the post page, or an alias
page (Hugo's meta-refresh redirect, which GitHub Pages serves as a 200)
pointing at the right new path.

Usage:
    hugo build -d public
    python3 scripts/check_urls.py --build public
"""

from __future__ import annotations

import argparse
import csv
import functools
import http.server
import re
import socketserver
import threading
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="public")
    ap.add_argument("--url-map", default="scripts/url_map.csv")
    ap.add_argument("--port", type=int, default=1414)
    args = ap.parse_args()

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(Path(args.build).resolve()))
    handler.log_message = lambda *a, **k: None

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: N802
            pass

    quiet = functools.partial(Quiet, directory=str(Path(args.build).resolve()))
    with socketserver.TCPServer(("127.0.0.1", args.port), quiet) as srv:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{args.port}"

        ok, bad = 0, []
        with open(args.url_map, newline="") as fh:
            for row in csv.DictReader(fh):
                old, new, how = row["old_path"], row["new_path"], row["method"]
                try:
                    with urllib.request.urlopen(base + old, timeout=10) as resp:
                        body = resp.read().decode("utf-8", "replace")
                        status = resp.status
                        final = resp.url[len(base):]
                except Exception as exc:  # noqa: BLE001
                    bad.append(f"{old}: request failed ({exc})")
                    continue
                if status != 200:
                    bad.append(f"{old}: HTTP {status}")
                    continue
                if final == new or (how == "same" and final == old):
                    ok += 1
                    continue
                # alias page: meta refresh to the new URL
                m = re.search(r'content="0;\s*url=([^"]+)"', body)
                if m and m.group(1).rstrip("/") .endswith(new.rstrip("/")):
                    ok += 1
                    continue
                bad.append(f"{old}: 200 but not the expected page/redirect "
                           f"(wanted {new})")
        srv.shutdown()

    print(f"check_urls: {ok} ok, {len(bad)} failed")
    for b in bad:
        print(f"  FAIL {b}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
