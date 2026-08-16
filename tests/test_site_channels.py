"""Zero-network tests for the site exports, the sync client, and the channels.

Every test here runs offline. Network calls are replaced with fakes.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from engine import sync_site
from engine.channels import bridgy as bridgy_mod
from engine.channels.bridgy import BridgyChannel
from engine.channels.nostr import created_at_epoch
from engine.config import Audience
from engine.render import (SITE_POSTS_LIMIT, site_posts_export, text_to_html,
                           write_site_resolutions)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 8, 16, 13, 17, tzinfo=timezone.utc)


def _aud(tmp_path: Path) -> Audience:
    return Audience(name="t", display_name="T (bot)", domain="t.example.com", site_url="https://t.example.com",
                    description="d", contact_email="t@example.com", repo="x/y", site_root="audiences/t/site",
                    tags=["PredictionMarkets"], sources=[], shapes=[], channels=[], thresholds={}, raw={})


# --------------------------------------------------------------- text_to_html
def test_text_to_html_keeps_apostrophes_and_skips_numeric_tags():
    h = text_to_html("Trump's #1 pick #Kalshi", "https://t.example.com")
    assert "Trump's" in h
    assert "tag/x27" not in h and "&#x27;" not in h
    assert 'href="/tag/1"' not in h and "#1" in h
    assert '<a href="/tag/kalshi">#Kalshi</a>' in h


def test_text_to_html_still_escapes_markup():
    h = text_to_html('a < b & "q" #Tag', "https://t.example.com")
    assert "&lt; b &amp;" in h and '"q"' in h
    assert '<a href="/tag/tag">#Tag</a>' in h


# ------------------------------------------------------------ bounded exports
def test_site_posts_export_caps_count_and_fields():
    posts = [{"id": f"p{i}", "slot": "am", "shape": "movers", "title": "T", "text": "x",
              "published_at": "2026-08-16T13:17:00Z", "tags": ["A"], "chart": None, "chart_alt": "",
              "rows": [{"big": "x" * 1000}], "extra": {"chart_kind": "bars"}, "text_html": "x"}
             for i in range(120)]
    out = site_posts_export(posts, "https://t.example.com")
    assert len(out) == SITE_POSTS_LIMIT
    assert out[0]["id"] == "p0" and out[0]["url"] == "https://t.example.com/p/p0"
    assert "rows" not in out[0] and "extra" not in out[0] and "text_html" not in out[0]


def _write_ledger(path: Path, rows: list[dict]) -> None:
    fields = ["venue", "ticker", "result", "settled_at", "price_24h_before"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def test_write_site_resolutions_drops_old_rows(tmp_path):
    src, dst = tmp_path / "resolutions.csv", tmp_path / "site.csv"
    rows = []
    for days in (200, 120, 91, 89, 10, 0):
        ts = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({"venue": "k", "ticker": f"T{days}", "result": "yes", "settled_at": ts, "price_24h_before": "0.5"})
    _write_ledger(src, rows)
    kept = write_site_resolutions(src, dst, NOW)
    got = [r["ticker"] for r in csv.DictReader(open(dst, encoding="utf-8"))]
    assert kept == 3 and got == ["T89", "T10", "T0"]


def test_write_site_resolutions_caps_row_count(tmp_path):
    src, dst = tmp_path / "resolutions.csv", tmp_path / "site.csv"
    ts = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_ledger(src, [{"venue": "k", "ticker": f"T{i}", "result": "yes", "settled_at": ts,
                         "price_24h_before": "0.5"} for i in range(500)])
    kept = write_site_resolutions(src, dst, NOW, max_rows=100)
    got = [r["ticker"] for r in csv.DictReader(open(dst, encoding="utf-8"))]
    assert kept == 100 and got[0] == "T400" and got[-1] == "T499"  # the newest rows survive


def test_write_site_resolutions_caps_bytes(tmp_path):
    src, dst = tmp_path / "resolutions.csv", tmp_path / "site.csv"
    ts = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_ledger(src, [{"venue": "k", "ticker": "T" * 200 + str(i), "result": "yes", "settled_at": ts,
                         "price_24h_before": "0.5"} for i in range(1000)])
    write_site_resolutions(src, dst, NOW, max_bytes=10_000)
    assert dst.stat().st_size <= 10_000 + 200


# ------------------------------------------------------------------ sync_site
def test_upload_order_puts_data_last():
    paths = ["/data/resolutions.csv", "/index.html", "/feed.xml", "/style.css",
             "/charts/x.png", "/p/a.html", "/data/posts.json", "/sitemap.xml"]
    got = sync_site.order_for_upload(paths)
    assert got.index("/index.html") < got.index("/data/resolutions.csv")
    assert got.index("/p/a.html") < got.index("/data/posts.json")
    assert got.index("/feed.xml") < got.index("/style.css")
    assert got[-2:] == ["/data/posts.json", "/data/resolutions.csv"]


def test_build_chunks_skips_an_oversize_file():
    local = {
        "/index.html": ("aa", b"<html>"),
        "/data/resolutions.csv": ("bb", b"x" * (sync_site.MAX_FILE_BYTES + 1)),
    }
    chunks, skipped = sync_site.build_chunks(list(local), local)
    assert skipped == ["/data/resolutions.csv"]
    assert [f["path"] for c in chunks for f in c] == ["/index.html"]


class _Resp:
    def __init__(self, status, text="{}"):
        self.status_code = status
        self.text = text

    def json(self):
        return json.loads(self.text)


def _run_sync(monkeypatch, tmp_path, site_files: dict[str, bytes], statuses: list[int], sha=None):
    """Run sync_site.main against a fake Worker. Return (exit code, requests)."""
    aud = _aud(tmp_path)
    site = tmp_path / "site"
    for rel, data in site_files.items():
        path = site / rel.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(type(aud), "site_dir", property(lambda self: site))
    monkeypatch.setattr(sync_site, "load_audience", lambda name: aud)
    monkeypatch.setattr(sync_site.time, "sleep", lambda s: None)
    monkeypatch.setenv("SYNC_TOKEN", "t")
    sent: list[dict] = []
    codes = list(statuses)

    def fake_get(url, **kw):
        return _Resp(404, "nope")

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return _Resp(codes.pop(0) if codes else 200)

    monkeypatch.setattr(sync_site.requests, "get", fake_get)
    monkeypatch.setattr(sync_site.requests, "post", fake_post)
    argv = ["--audience", "t", "--base", "http://127.0.0.1:8787"]
    if sha:
        argv += ["--sha", sha]
    return sync_site.main(argv), sent


def test_sync_sends_final_commit_even_after_a_failed_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_site, "CHUNK_FILES", 1)
    files = {"/index.html": b"<html>", "/feed.xml": b"<feed/>", "/data/posts.json": b"[]"}
    code, sent = _run_sync(monkeypatch, tmp_path, files, statuses=[200, 400, 200, 200], sha="a" * 40)
    assert code == 1                                   # the run reports the failure
    final = sent[-1]
    assert final["final"] is True and final["sha"] == "a" * 40
    assert "/feed.xml" in final["manifest"]
    # The file in the failed chunk stays out of the manifest, so the next run resends it.
    failed_path = sent[1]["files"][0]["path"]
    assert failed_path == "/index.html" and failed_path not in final["manifest"]


def test_sync_retries_a_chunk_once_on_5xx(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_site, "CHUNK_FILES", 10)
    code, sent = _run_sync(monkeypatch, tmp_path, {"/index.html": b"<html>"}, statuses=[503, 200, 200])
    assert code == 0
    assert len(sent) == 3                              # chunk, retry of the chunk, final commit
    assert sent[0] == sent[1] and sent[2]["final"] is True


def test_sync_skips_an_oversize_file_and_exits_1(monkeypatch, tmp_path):
    files = {"/index.html": b"<html>", "/data/big.csv": b"x" * (sync_site.MAX_FILE_BYTES + 1)}
    code, sent = _run_sync(monkeypatch, tmp_path, files, statuses=[200, 200])
    assert code == 1
    uploaded = [f["path"] for body in sent for f in body["files"]]
    assert uploaded == ["/index.html"]
    assert "/data/big.csv" not in sent[-1]["manifest"]


# -------------------------------------------------------------------- bridgy
class _FakeHTTP:
    """Records requests and answers from a scripted list."""

    def __init__(self, posts: list[_Resp], gets: list[_Resp]):
        self.posts, self.gets = list(posts), list(gets)
        self.post_urls: list[str] = []
        self.get_urls: list[str] = []

    def post(self, url, data=None, timeout=None, headers=None):
        self.post_urls.append(url)
        return self.posts.pop(0)

    def get(self, url, timeout=None, headers=None, allow_redirects=None):
        self.get_urls.append(url)
        return self.gets.pop(0)


def _patch_bridgy(monkeypatch, http: _FakeHTTP, live=True):
    monkeypatch.setattr(bridgy_mod.requests, "post", http.post)
    monkeypatch.setattr(bridgy_mod.requests, "get", http.get)
    monkeypatch.setattr(bridgy_mod, "wait_until_live", lambda *a, **kw: live)
    monkeypatch.setattr(bridgy_mod, "probe_live", lambda *a, **kw: live)
    monkeypatch.setattr(bridgy_mod.time, "sleep", lambda s: None)


def _resp(status, text="", headers=None):
    r = _Resp(status, text or "{}")
    r.headers = headers or {}
    return r


POST = {"id": "2026-08-16-am", "text": "hi", "published_at": "2026-08-16T13:17:00Z"}


def test_bridgy_202_records_accepted_not_ok(monkeypatch, tmp_path):
    http = _FakeHTTP(posts=[_resp(202, "Added")], gets=[_resp(404), _resp(404), _resp(404)])
    _patch_bridgy(monkeypatch, http)
    rec = BridgyChannel({}).publish(_aud(tmp_path), POST, {})
    assert rec["accepted"] is True and rec["ok"] is False and rec["attempts"] == 1
    assert rec["source"].endswith("/p/2026-08-16-am")


def test_bridgy_verifies_on_a_later_run(monkeypatch, tmp_path):
    link = '<at://did:plc:abc/app.bsky.feed.post/1>; rel="alternate"'
    http = _FakeHTTP(posts=[], gets=[_resp(200, headers={"Link": link})])
    _patch_bridgy(monkeypatch, http)
    rec = BridgyChannel({}).publish(_aud(tmp_path), POST, {}, prev={"accepted": True, "attempts": 1})
    assert rec["ok"] is True and rec["at_uri"] == "at://did:plc:abc/app.bsky.feed.post/1"
    assert http.post_urls == []                         # no second webmention


def test_bridgy_gives_up_after_three_attempts(monkeypatch, tmp_path):
    http = _FakeHTTP(posts=[], gets=[_resp(404), _resp(404), _resp(404)])
    _patch_bridgy(monkeypatch, http)
    rec = BridgyChannel({}).publish(_aud(tmp_path), POST, {}, prev={"accepted": True, "attempts": 3})
    assert rec["gave_up"] is True and rec["ok"] is False and http.post_urls == []


def test_bridgy_enables_the_site_on_no_user_found(monkeypatch, tmp_path):
    http = _FakeHTTP(posts=[_resp(400, "No user found for domain t.example.com"),
                            _resp(200, "ok"),        # POST /web-site
                            _resp(202, "Added")],    # webmention resend
                     gets=[_resp(404), _resp(404), _resp(404)])
    _patch_bridgy(monkeypatch, http)
    rec = BridgyChannel({}).publish(_aud(tmp_path), POST, {})
    assert http.post_urls == ["https://fed.brid.gy/webmention", "https://fed.brid.gy/web-site",
                              "https://fed.brid.gy/webmention"]
    assert rec["accepted"] is True and rec["bridgy_enabled_at"]


def test_bridgy_waits_once_per_run_when_the_site_is_down(monkeypatch, tmp_path):
    http = _FakeHTTP(posts=[], gets=[])
    _patch_bridgy(monkeypatch, http, live=False)
    waits = []
    monkeypatch.setattr(bridgy_mod, "wait_until_live", lambda *a, **kw: waits.append(a[0]) or False)
    ch = BridgyChannel({})
    aud = _aud(tmp_path)
    first = ch.publish(aud, POST, {})
    second = ch.publish(aud, dict(POST, id="2026-08-16-pm"), {})
    assert len(waits) == 1                              # only one long wait per run
    assert first["ok"] is False and "not live" in first["error"]
    assert second["error"] == "site not live; skipped"


# --------------------------------------------------------------------- nostr
def test_created_at_epoch_is_stable_and_survives_both_formats():
    assert created_at_epoch("2026-08-16T13:17:00Z") == created_at_epoch("2026-08-16T13:17:00+00:00")
    assert created_at_epoch("2026-08-16T13:17:00Z") == 1786886220
    assert created_at_epoch("nonsense") is None and created_at_epoch(None) is None


def test_nostr_script_accepts_created_at():
    text = (ROOT / "engine" / "channels" / "nostr_publish.mjs").read_text()
    assert "created-at" in text and "readCreatedAt(flags)" in text


# ----------------------------------------------------------------- workflows
def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


@pytest.mark.parametrize("name", ["publish.yml", "healthcheck.yml"])
def test_workflow_is_valid_yaml_and_pushes_fail_loudly(name):
    wf = _workflow(name)
    assert wf["jobs"], name
    text = (ROOT / ".github" / "workflows" / name).read_text()
    assert "&& break || sleep 10" not in text          # the old loop swallowed a failed push
    for step in list(wf["jobs"].values())[0]["steps"]:
        run = step.get("run") or ""
        if "git push" in run:
            assert 'ok=1; break' in run.replace("\n", " ") or "[ \"$ok\" = 1 ]" in run


def test_publish_workflow_does_not_stop_at_the_first_broken_audience():
    wf = _workflow("publish.yml")
    steps = {s.get("name") or s.get("uses"): s for s in wf["jobs"]["publish"]["steps"]}
    run_step = steps["Run engine"]
    assert "failed=1" in run_step["run"] and "failed=$failed" in run_step["run"]
    assert "set -euo pipefail" not in run_step["run"]  # -e would abort the audience loop
    # A later step turns the job red, so the commit step still runs first.
    fail_step = steps["Fail when the engine or the sync reported a failure"]
    assert "exit 1" in fail_step["run"]
    assert list(steps).index("Commit data + site") < list(steps).index(
        "Fail when the engine or the sync reported a failure")
    assert steps["Publish to channels"]["timeout-minutes"] == 15


def _cron_dows(field: str) -> set[int]:
    if field == "*":
        return set(range(7))
    out: set[int] = set()
    for part in field.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return {d % 7 for d in out}


def test_every_audience_slot_time_has_a_cron_entry():
    wf = _workflow("publish.yml")
    schedule = (wf.get("on") or wf.get(True))["schedule"]
    crons = [c["cron"].split() for c in schedule]
    missing = []
    for path in sorted((ROOT / "audiences").glob("*/audience.yml")):
        aud = yaml.safe_load(path.read_text()) or {}
        for shape in aud.get("shapes") or []:
            at = shape.get("at")
            if not at:
                continue
            hh, mm = at.split(":")
            want = {(d + 1) % 7 for d in shape.get("days", range(7))}
            covered = any(c[1] == str(int(hh))
                          and str(int(mm)) in {m.strip() for m in c[0].split(",")}
                          and want <= _cron_dows(c[4]) for c in crons)
            if not covered:
                missing.append(f"{path.parent.name}:{shape.get('slot')}@{at}")
    assert not missing, f"slots with no cron entry: {missing}"


# ------------------------------------------------------------------ template
def test_feed_template_marks_entries_as_notes():
    tpl = (ROOT / "engine" / "templates" / "feed.xml.j2").read_text()
    assert 'xmlns:activity="http://activitystrea.ms/spec/1.0/"' in tpl
    assert "<activity:object-type>http://activitystrea.ms/schema/1.0/note</activity:object-type>" in tpl
