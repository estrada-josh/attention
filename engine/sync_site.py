"""Upload the rendered site to the audience's Worker (KV) with a manifest diff.

Usage: python -m engine.sync_site --audience oddsdrift [--sha <commit>] [--full]
Env:   SYNC_TOKEN (bearer for the Worker's /hooks/* endpoints)

Flow: GET /hooks/manifest (path -> sha256) -> hash local files under site/ ->
POST /hooks/upload in chunks with only changed files (+ deletes) -> last chunk
carries the commit sha. Exit 0 on success, 1 when the Worker rejects a chunk.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys

import requests

from .config import load_audience

CHUNK_FILES = 40
CHUNK_BYTES = 3 * 1024 * 1024


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience", required=True)
    ap.add_argument("--sha", default=None)
    ap.add_argument("--full", action="store_true", help="ignore the remote manifest; upload everything")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    aud = load_audience(a.audience)
    token = os.environ.get("SYNC_TOKEN")
    if not token:
        print("SYNC_TOKEN not set", file=sys.stderr)
        return 1
    h = {"Authorization": f"Bearer {token}", "User-Agent": "attention-sync/0.1"}
    base = aud.site_url.rstrip("/")

    remote: dict[str, str] = {}
    if not a.full:
        r = requests.get(f"{base}/hooks/manifest", headers=h, timeout=60)
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
    if a.dry_run:
        for p in changed[:50]:
            print("  +", p)
        return 0
    if not changed and not deletes:
        if a.sha:
            requests.post(f"{base}/hooks/upload", headers=h, json={"files": [], "sha": a.sha}, timeout=60)
        return 0

    chunks: list[list[dict]] = [[]]
    size = 0
    for p in changed:
        digest, data = local[p]
        item = {"path": p, "b64": base64.b64encode(data).decode(), "sha256": digest}
        if len(chunks[-1]) >= CHUNK_FILES or size + len(item["b64"]) > CHUNK_BYTES:
            chunks.append([])
            size = 0
        chunks[-1].append(item)
        size += len(item["b64"])
    for i, files in enumerate(chunks):
        last = i == len(chunks) - 1
        body = {"files": files}
        if last:
            body["delete"] = deletes
            if a.sha:
                body["sha"] = a.sha
        r = requests.post(f"{base}/hooks/upload", headers=h, json=body, timeout=180)
        print(f"chunk {i + 1}/{len(chunks)} ({len(files)} files) -> {r.status_code} {r.text[:160]}")
        if r.status_code != 200:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
