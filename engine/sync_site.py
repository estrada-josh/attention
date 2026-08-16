"""Upload the rendered site to the audience's Worker (KV) with a manifest diff.

Usage: python -m engine.sync_site --audience oddsdrift [--sha <commit>] [--full]
Env:   SYNC_TOKEN (bearer for the Worker's /hooks/* endpoints)

Flow: GET /hooks/manifest (path -> sha256) -> hash local files under site/ ->
POST /hooks/upload in chunks with only changed files -> one final POST that
carries the full manifest, the deletes, and the commit sha.

Order rules:
1. Pages, feeds, the sitemap, and charts upload before /data/*. A data file
   that fails must never block a new post page.
2. A failed chunk does not stop the run. The script sends every chunk, sends
   the final commit, and returns 1 at the end.
3. A single file above MAX_FILE_BYTES is skipped with a warning. The Worker
   rejects a body above 2 MiB, so such a chunk can only fail.

Exit 0 when every chunk succeeded. Exit 1 when any chunk failed or any file
was skipped.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import sys
import time

import requests

from .config import load_audience

CHUNK_FILES = 30
CHUNK_BYTES = 1024 * 1024
# Largest single file we send, before base64. The Worker caps a request body at
# 2 MiB and base64 adds one third, so 1.4 MB raw is the safe ceiling.
MAX_FILE_BYTES = 1_400_000
# KV allows one write per second per key. The Worker writes the meta keys on
# the final request only, but the pause also keeps the file writes apart.
CHUNK_SLEEP_S = 1.1
# Paths that must reach KV first. Lower rank uploads earlier.
DATA_PREFIX = "/data/"


def upload_rank(path: str) -> int:
    """Rank one site path for upload order. Lower goes first."""
    if path.startswith(DATA_PREFIX):
        return 2
    if path.endswith(".html") or path.startswith("/feed.") or path == "/sitemap.xml" or path.startswith("/charts/"):
        return 0
    return 1


def order_for_upload(paths: list[str]) -> list[str]:
    """Sort changed paths so pages, feeds, and charts upload before /data/*."""
    return sorted(paths, key=lambda p: (upload_rank(p), p))


def build_chunks(changed: list[str], local: dict[str, tuple[str, bytes]]) -> tuple[list[list[dict]], list[str]]:
    """Group changed files into request-sized chunks. Return (chunks, skipped)."""
    chunks: list[list[dict]] = []
    skipped: list[str] = []
    size = 0
    for path in order_for_upload(changed):
        digest, data = local[path]
        if len(data) > MAX_FILE_BYTES:
            skipped.append(path)
            continue
        item = {"path": path, "b64": base64.b64encode(data).decode(), "sha256": digest}
        if not chunks or len(chunks[-1]) >= CHUNK_FILES or size + len(item["b64"]) > CHUNK_BYTES:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item["b64"])
    return chunks, skipped


def post_chunk(url: str, headers: dict, body: dict, label: str) -> bool:
    """POST one upload request. Retry once on a 5xx or a network error."""
    for attempt in (1, 2):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=180)
        except requests.RequestException as e:
            print(f"{label} -> network error: {e}", file=sys.stderr)
            if attempt == 2:
                return False
            time.sleep(CHUNK_SLEEP_S)
            continue
        print(f"{label} -> {r.status_code} {r.text[:160]}")
        if r.status_code == 200:
            return True
        if r.status_code < 500 or attempt == 2:
            return False
        time.sleep(CHUNK_SLEEP_S)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience", required=True)
    ap.add_argument("--sha", default=None)
    ap.add_argument("--full", action="store_true", help="ignore the remote manifest; upload everything")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--base", default=None, help="override the Worker base URL (e.g. http://127.0.0.1:8787 for wrangler dev)")
    a = ap.parse_args(argv)
    aud = load_audience(a.audience)
    token = os.environ.get("SYNC_TOKEN")
    if not token:
        print("SYNC_TOKEN not set", file=sys.stderr)
        return 1
    h = {"Authorization": f"Bearer {token}", "User-Agent": "attention-sync/0.1"}
    base = (a.base or aud.site_url).rstrip("/")
    upload_url = f"{base}/hooks/upload"

    remote: dict[str, str] = {}
    if not a.full:
        try:
            r = requests.get(f"{base}/hooks/manifest", headers=h, timeout=60)
        except requests.RequestException as e:
            print(f"manifest fetch failed ({e}); uploading everything", file=sys.stderr)
        else:
            if r.status_code == 200:
                remote = r.json()
            else:
                print(f"manifest fetch -> {r.status_code}; uploading everything", file=sys.stderr)

    local: dict[str, tuple[str, bytes]] = {}
    root = aud.site_dir
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = "/" + path.relative_to(root).as_posix()
        data = path.read_bytes()
        local[rel] = (hashlib.sha256(data).hexdigest(), data)

    changed = [p for p, (digest, _) in local.items() if remote.get(p) != digest]
    deletes = [p for p in remote if p not in local]
    print(f"local files: {len(local)}  changed: {len(changed)}  deletes: {len(deletes)}")

    chunks, skipped = build_chunks(changed, local)
    for path in skipped:
        print(f"::warning::skipping {path}: {len(local[path][1])} bytes is above the {MAX_FILE_BYTES} byte upload cap",
              file=sys.stderr)

    if a.dry_run:
        for path in order_for_upload(changed)[:50]:
            print("  +", path)
        return 1 if skipped else 0

    failed_paths: set[str] = set()
    failures = 0
    for i, files in enumerate(chunks):
        if i:
            time.sleep(CHUNK_SLEEP_S)
        label = f"chunk {i + 1}/{len(chunks)} ({len(files)} files)"
        if not post_chunk(upload_url, h, {"files": files}, label):
            failures += 1
            failed_paths.update(f["path"] for f in files)

    # The manifest the Worker stores must name only files it really holds.
    # A skipped file and a failed file stay out, so the next run resends them.
    manifest = {p: digest for p, (digest, _) in local.items()
                if p not in failed_paths and p not in skipped}
    final_body: dict = {"files": [], "delete": deletes, "manifest": manifest, "final": True}
    if a.sha:
        final_body["sha"] = a.sha
    if chunks:
        time.sleep(CHUNK_SLEEP_S)
    if not post_chunk(upload_url, h, final_body, "final commit"):
        failures += 1

    if failures or skipped:
        print(f"sync incomplete: {failures} failed request(s), {len(skipped)} skipped file(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
