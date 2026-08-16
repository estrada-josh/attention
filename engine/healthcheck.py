"""Daily healthcheck for one audience.

Appends one row to data/metrics.csv (site ok, follower and star counts).
After two consecutive failing days it opens or updates one GitHub issue.
Usage: python -m engine.healthcheck --audience oddsdrift
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests

from .config import load_audience
from .state import Store

UA = {"User-Agent": "attention-healthcheck/0.1"}


def _get(url, **kw):
    try:
        return requests.get(url, timeout=30, headers={**UA, **kw.pop("headers", {})}, **kw)
    except requests.RequestException:
        return None


def bsky_profile(handle: str) -> dict:
    r = _get("https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile", params={"actor": handle})
    if r is not None and r.status_code == 200:
        d = r.json()
        return {"followers": d.get("followersCount"), "posts": d.get("postsCount"), "ok": True}
    return {"followers": None, "posts": None, "ok": False}


def gh_repo(repo: str) -> dict:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    h = {"Authorization": f"Bearer {tok}"} if tok else {}
    r = _get(f"https://api.github.com/repos/{repo}", headers=h)
    if r is not None and r.status_code == 200:
        d = r.json()
        return {"stars": d.get("stargazers_count"), "watchers": d.get("subscribers_count"),
                "forks": d.get("forks_count"), "size_kb": d.get("size")}
    return {"stars": None, "watchers": None, "forks": None, "size_kb": None}


def _issue_upsert(audience_name: str, title: str, body: str) -> None:
    """One open issue per audience: comment if it exists, else create."""
    try:
        q = subprocess.run(["gh", "issue", "list", "--search", f"[healthcheck] {audience_name} in:title",
                            "--state", "open", "--json", "number", "-q", ".[0].number"],
                           capture_output=True, text=True, timeout=60)
        number = q.stdout.strip()
        if number:
            subprocess.run(["gh", "issue", "comment", number, "--body", body], timeout=60, check=False)
        else:
            subprocess.run(["gh", "issue", "create", "--title", title, "--body", body], timeout=60, check=False)
    except Exception as e:  # noqa: BLE001
        print("issue update failed:", e, file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audience", required=True)
    a = ap.parse_args(argv)
    aud = load_audience(a.audience)
    store = Store(aud.data_dir)
    now = datetime.now(timezone.utc)
    problems: list[str] = []

    r = _get(f"{aud.site_url}/health.json")
    site_ok = r is not None and r.status_code == 200
    if not site_ok:
        problems.append("site /health.json not 200")
    r = _get(f"{aud.site_url}/feed.xml")
    if not (r is not None and r.status_code == 200 and "<feed" in r.text):
        problems.append("feed.xml missing")

    state = store.load_state()
    last = (state.get("last_run") or {}).get("at")
    if last:
        age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        if age_h > 30:
            problems.append(f"last run {age_h:.0f}h ago")
    for name, b in (state.get("breakers") or {}).items():
        if b.get("disabled"):
            problems.append(f"source breaker open: {name}")

    handle = aud.raw.get("bluesky_handle") or f"{aud.domain}.web.brid.gy"
    bsky = bsky_profile(handle)
    if not bsky["ok"]:
        problems.append(f"bluesky profile {handle} not found")
    gh = gh_repo(aud.repo)

    row = {"date": now.strftime("%Y-%m-%d"), "site_ok": site_ok,
           "bsky_followers": bsky["followers"], "bsky_posts": bsky["posts"],
           "gh_stars": gh["stars"], "gh_watchers": gh["watchers"], "gh_forks": gh["forks"],
           "repo_size_kb": gh["size_kb"], "posts_total": len(store.load_posts()),
           "problems": "; ".join(problems)}
    store.append_metrics(row)
    print(json.dumps(row, indent=1))

    hc = state.setdefault("healthcheck", {"consecutive_failures": 0})
    hc["consecutive_failures"] = hc.get("consecutive_failures", 0) + 1 if problems else 0
    hc["last"] = row
    store.save_state(state)
    if hc["consecutive_failures"] >= 2 and os.environ.get("GH_TOKEN"):
        body = "Problems:\n- " + "\n- ".join(problems) + f"\n\nMetrics row: `{json.dumps(row)}`"
        _issue_upsert(aud.name, f"[healthcheck] {aud.name}: {problems[0]}", body)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
