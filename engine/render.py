"""Render the static site for one audience into audiences/<name>/site/.

Output: index.html, p/<id>.html, about.html, calibration.html, feed.xml,
feed.json, sitemap.xml, style.css, avatar.png/header.png (from assets),
.well-known/nostr.json, data/posts.json, data/resolutions.csv.
The data exports are bounded: data/posts.json holds the last 50 posts and
data/resolutions.csv the last 90 days. The repo keeps the full history.
The site is plain HTML with microformats2 so Bridgy Fed can bridge it.
"""
from __future__ import annotations

import csv
import html
import json
import logging
import re
import shutil
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Audience
from .state import Store

log = logging.getLogger("engine.render")

TEMPLATES = Path(__file__).parent / "templates"

# The site exports must stay small. They ride the KV upload path, which caps a
# single file well under 2 MB (see engine/sync_site.py MAX_FILE_BYTES).
# The repo under audiences/<name>/data/ keeps the full history.
SITE_POSTS_LIMIT = 50
# Fields the public post export needs. "rows" and "extra" stay out: the table
# is already on the post page, and "extra" holds internal chart settings.
SITE_POST_FIELDS = ("id", "slot", "shape", "title", "text", "published_at", "tags", "chart", "chart_alt")
SITE_RESOLUTIONS_DAYS = 90
# Two hard bounds under the day window. One bad settled_at value, or a busy
# day, must never push the export past the upload limit.
SITE_RESOLUTIONS_MAX_ROWS = 5000
SITE_RESOLUTIONS_MAX_BYTES = 1_000_000


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]),
                       trim_blocks=True, lstrip_blocks=True)


# A hashtag must start with a letter. The tag never follows a word character,
# a slash, or an "&". The "&" guard keeps an HTML entity out of the match.
HASHTAG_RE = re.compile(r"(?<![\w/&])#([^\W\d_]\w*)")


def text_to_html(text: str, site_url: str) -> str:
    """Escape, link hashtags and bare domains, keep line breaks.

    The result goes into element content only, never into an attribute.
    Therefore quote=False keeps an apostrophe as an apostrophe. An escaped
    apostrophe (&#x27;) would otherwise become the bogus hashtag #x27.
    """
    out = html.escape(text, quote=False)
    out = HASHTAG_RE.sub(lambda m: f'<a href="/tag/{m.group(1).lower()}">#{m.group(1)}</a>', out)
    dom = re.escape(site_url.replace("https://", ""))
    out = re.sub(rf"(?<![\w/]){dom}(/[\w\-/]*)?", lambda m: f'<a href="{site_url}{m.group(1) or ""}">{m.group(0)}</a>', out)
    return out.replace("\n", "<br>\n")


def _fmt_c(p) -> str:
    return "—" if p is None else f"{round(p * 100)}¢"


def _fmt_pts(d) -> str:
    return "—" if d is None else f"{round(d * 100):+d}"


def _fmt_money(v) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def table_html(post: dict) -> str:
    rows = post.get("rows") or []
    if not rows:
        return "<p>No table for this post.</p>"
    shape = post.get("shape")
    if shape == "movers":
        head = ["Venue", "Market", "24h ago", "Now", "Δ", "24h volume", "Closes"]
        body = [[r["label"], f'<a href="{html.escape(r["url"])}" rel="noopener">{html.escape(r["title"])}</a>' + (f' <small>{html.escape(r["subtitle"])}</small>' if r.get("subtitle") else ""),
                 _fmt_c(r["prev"]), _fmt_c(r["now"]), f'<span class="{"up" if (r["delta"] or 0) >= 0 else "down"}">{_fmt_pts(r["delta"])}</span>',
                 _fmt_money(r.get("volume_24h")), (r.get("close_time") or "")[:10]] for r in rows]
        num = {2, 3, 4, 5}
    elif shape == "settled":
        head = ["Venue", "Market", "24h before", "Result", "24h volume", "Settled"]
        body = [[r["label"], ("<b>UPSET</b> · " if r.get("upset") else "") + f'<a href="{html.escape(r["url"])}" rel="noopener">{html.escape(r["title"])}</a>' + (f' <small>{html.escape(r["subtitle"])}</small>' if r.get("subtitle") else ""),
                 _fmt_c(r.get("p24")), f'<span class="{"up" if r["result"] == "yes" else "down"}">{r["result"].upper()}</span>',
                 _fmt_money(r.get("volume_24h")), (r.get("settled_at") or "")[:16].replace("T", " ")] for r in rows]
        num = {2, 4}
    elif shape == "watchlist":
        head = ["Item", "Venue", "Market", "Now", "24h Δ", "24h volume"]
        body = [[html.escape(r["name"]), r["label"], f'<a href="{html.escape(r["url"])}" rel="noopener">{html.escape(r["title"])}</a>' + (f' <small>{html.escape(r["subtitle"])}</small>' if r.get("subtitle") else ""),
                 _fmt_c(r["now"]), _fmt_pts(r.get("delta")), _fmt_money(r.get("volume_24h"))] for r in rows]
        num = {3, 4, 5}
    elif shape == "scorecard":
        head = ["Priced (24h before)", "n", "Resolved YES", "Rate"]
        body = [[r["bucket"], r["n"], r["yes"], f'{r["rate"]*100:.0f}%' if r.get("rate") is not None else "—"] for r in rows]
        num = {1, 2, 3}
    else:
        head = list(rows[0].keys())
        body = [[html.escape(str(v)) for v in r.values()] for r in rows]
        num = set()
    th = "".join(f'<th class="{"num" if i in num else ""}">{h}</th>' for i, h in enumerate(head))
    trs = "".join("<tr>" + "".join(f'<td class="{"num" if i in num else ""}">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in body)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def calibration_data(store: Store, now):
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_venue = defaultdict(list)
    buckets = defaultdict(lambda: [0, 0])
    n_week = 0
    for res in store.load_resolutions():
        try:
            p = float(res.get("price_24h_before") or "")
        except ValueError:
            continue
        if res.get("result") not in ("yes", "no"):
            continue
        per_venue[res["venue"]].append((p, res["result"]))
        b = min(int(p * 10), 9)
        buckets[b][0] += 1
        buckets[b][1] += 1 if res["result"] == "yes" else 0
        if (res.get("settled_at") or "") >= cutoff:
            n_week += 1
    venues = []
    for v, pairs in sorted(per_venue.items()):
        br = sum((p - (1.0 if r == "yes" else 0.0)) ** 2 for p, r in pairs) / len(pairs) if pairs else None
        venues.append({"name": v, "n": len(pairs), "brier": br})
    bl = [{"bucket": f"{b*10}-{b*10+10}¢", "n": v[0], "yes": v[1], "rate": (v[1] / v[0]) if v[0] else None}
          for b, v in sorted(buckets.items())]
    return {"venues": venues, "buckets": bl, "n_all": sum(len(x) for x in per_venue.values()), "n_week": n_week}


def site_posts_export(posts: list[dict], site_url: str, limit: int = SITE_POSTS_LIMIT) -> list[dict]:
    """Return the newest `limit` posts with only the fields the export needs."""
    out = []
    for p in posts[:limit]:
        item = {k: p.get(k) for k in SITE_POST_FIELDS}
        item["url"] = f"{site_url}/p/{p['id']}"
        out.append(item)
    return out


def _res_cutoff(now, days: int) -> str:
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_site_resolutions(src: Path, dst: Path, now, days: int = SITE_RESOLUTIONS_DAYS,
                           max_rows: int = SITE_RESOLUTIONS_MAX_ROWS,
                           max_bytes: int = SITE_RESOLUTIONS_MAX_BYTES) -> int:
    """Write a bounded slice of the resolutions ledger. Return the row count.

    The slice holds the newest rows that settled within `days` days, capped by
    `max_rows` and by `max_bytes`. Rows keep the order of the source file.
    A row with an unreadable settled_at counts as old and drops out.
    """
    with open(src, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    cutoff = _res_cutoff(now, days)
    fresh = [r for r in rows if (r.get("settled_at") or r.get("recorded_at") or "") >= cutoff]
    kept: list[dict] = []
    size = 0
    for r in reversed(fresh):  # newest first, so the cap drops the oldest rows
        cost = sum(len(str(r.get(k) or "")) for k in fields) + len(fields) + 1
        if len(kept) >= max_rows or size + cost > max_bytes:
            break
        kept.append(r)
        size += cost
    kept.reverse()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in kept:
            w.writerow(r)
    if len(kept) < len(rows):
        log.info("site resolutions.csv: kept %d of %d rows (last %d days)", len(kept), len(rows), days)
    return len(kept)


def render_site(audience: Audience, now) -> Path:
    env = _env()
    store = Store(audience.data_dir)
    posts = store.load_posts()
    for p in posts:
        p["text_html"] = text_to_html(p["text"], audience.site_url)
    site = audience.site_dir
    site.mkdir(parents=True, exist_ok=True)
    (site / "p").mkdir(exist_ok=True)
    (site / "data").mkdir(exist_ok=True)
    (site / ".well-known").mkdir(exist_ok=True)

    follow = list(audience.raw.get("follow_links") or [])
    nav = list(audience.raw.get("nav_links") or [])
    common = {"a": audience, "follow": follow, "nav": nav}

    def write(name: str, tpl: str, **kw):
        out = env.get_template(tpl).render(**common, **kw)
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(out, encoding="utf-8")

    write("index.html", "index.html.j2", posts=posts[:20], canonical=audience.site_url + "/")
    for p in posts:
        write(f"p/{p['id']}.html", "post.html.j2", p=p, table_html=table_html(p),
              canonical=f"{audience.site_url}/p/{p['id']}")
    tags: dict[str, list] = {}
    for p in posts:
        for t in p.get("tags") or []:
            tags.setdefault(t.lower(), []).append(p)
    for t, plist in tags.items():
        write(f"tag/{t}.html", "index.html.j2", posts=plist[:50], canonical=f"{audience.site_url}/tag/{t}")
    about_md = (audience.dir / "about.md").read_text() if (audience.dir / "about.md").exists() else audience.description
    write("about.html", "about.html.j2", about_html=_md_to_html(about_md), canonical=audience.site_url + "/about",
          thresholds_yaml=yaml.safe_dump(audience.thresholds, sort_keys=True),
          sources=[{"name": s["type"], "note": s.get("note", "")} for s in audience.sources])
    cal = calibration_data(store, now)
    write("calibration.html", "calibration.html.j2", canonical=audience.site_url + "/calibration", **cal)
    updated = posts[0]["published_at"] if posts else now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    write("feed.xml", "feed.xml.j2", posts=posts[:30], updated=updated)
    feed_json = {
        "version": "https://jsonfeed.org/version/1.1", "title": audience.display_name,
        "home_page_url": audience.site_url + "/", "feed_url": audience.site_url + "/feed.json",
        "description": audience.description, "icon": audience.site_url + "/avatar.png",
        "items": [{"id": f"{audience.site_url}/p/{p['id']}", "url": f"{audience.site_url}/p/{p['id']}",
                   "title": p["title"], "content_text": p["text"], "date_published": p["published_at"],
                   "tags": p.get("tags", []), **({"image": audience.site_url + p["chart"]} if p.get("chart") else {})}
                  for p in posts[:30]],
    }
    (site / "feed.json").write_text(json.dumps(feed_json, indent=1))
    (site / "data" / "posts.json").write_text(
        json.dumps(site_posts_export(posts, audience.site_url), indent=1))
    if store.resolutions_path.exists():
        write_site_resolutions(store.resolutions_path, site / "data" / "resolutions.csv", now)
    urls = [audience.site_url + "/", audience.site_url + "/about", audience.site_url + "/calibration"] + \
           [f"{audience.site_url}/p/{p['id']}" for p in posts]
    (site / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
                                      "".join(f"  <url><loc>{html.escape(u)}</loc></url>\n" for u in urls) + "</urlset>\n")
    (site / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {audience.site_url}/sitemap.xml\n")
    shutil.copy(TEMPLATES / "style.css", site / "style.css")
    for asset in ("avatar.png", "header.png"):
        src = audience.assets_dir / asset
        if src.exists():
            shutil.copy(src, site / asset)
    nostr_pub = audience.raw.get("nostr_pubkey_hex")
    if nostr_pub:
        (site / ".well-known" / "nostr.json").write_text(json.dumps({"names": {"_": nostr_pub}}))
    (site / "health.txt").write_text(f"rendered {now.isoformat()}\nposts {len(posts)}\n")
    return site


def _md_to_html(md: str) -> str:
    """Tiny markdown: headings, paragraphs, bullet lists, links, bold, code."""
    out = []
    in_list = False
    for line in md.splitlines():
        s = line.rstrip()
        if not s:
            if in_list:
                out.append("</ul>")
                in_list = False
            continue
        s = html.escape(s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        if s.startswith("### "):
            out.append(f"<h3>{s[4:]}</h3>")
        elif s.startswith("## "):
            out.append(f"<h2>{s[3:]}</h2>")
        elif s.startswith("# "):
            out.append(f"<h1>{s[2:]}</h1>")
        elif s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{s[2:]}</li>")
        else:
            out.append(f"<p>{s}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)
