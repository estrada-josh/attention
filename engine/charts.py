"""PNG charts for posts (matplotlib, headless). One function per chart kind.

Every chart: 1200x675 px, light background, watermark with the site domain
bottom-right, identical layout per kind. Returns the written path.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

W, H, DPI = 1200, 675, 100
BG = "#fbfaf7"
INK = "#1d1d1b"
MUTED = "#6b6b66"
UP = "#1f7a4d"
DOWN = "#b23a2f"
GRID = "#e5e2da"
VENUE_COLORS = {"kalshi": "#2b5b9e", "polymarket": "#6b3fa0"}


def _fig():
    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=BG)
    return fig


def _finish(fig, ax, title: str, subtitle: str, watermark: str, path: Path) -> Path:
    fig.text(0.04, 0.93, title, fontsize=22, fontweight="bold", color=INK, ha="left", va="center")
    if subtitle:
        fig.text(0.04, 0.875, subtitle, fontsize=13, color=MUTED, ha="left", va="center")
    fig.text(0.985, 0.025, watermark, fontsize=11, color=MUTED, ha="right", va="bottom")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=11)
    ax.set_facecolor(BG)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor=BG)
    plt.close(fig)
    return path


def _label(row: dict, n: int = 46) -> str:
    t = row.get("title") or ""
    if row.get("subtitle"):
        t += f" — {row['subtitle']}"
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1] + "…"


def movers_chart(rows: list[dict], title: str, subtitle: str, watermark: str, path: Path) -> Path:
    rows = rows[:8]
    fig = _fig()
    ax = fig.add_axes([0.42, 0.12, 0.54, 0.7])
    ys = list(range(len(rows)))[::-1]
    for y, r in zip(ys, rows):
        prev, now = (r["prev"] or 0) * 100, (r["now"] or 0) * 100
        # print the delta of the rounded endpoints, so the label adds up
        pv, nv = round(prev), round(now)
        color = UP if now >= prev else DOWN
        ax.plot([prev, now], [y, y], color=color, linewidth=6, solid_capstyle="round", zorder=2)
        ax.scatter([prev], [y], color=MUTED, s=60, zorder=3)
        ax.scatter([now], [y], color=color, s=90, zorder=4)
        ax.text(now + (2.5 if now >= prev else -2.5), y, f"{pv:d}→{nv:d}¢ ({nv - pv:+d})",
                va="center", ha="left" if now >= prev else "right", fontsize=11, color=INK)
        ax.text(-0.03, y, f"{r.get('label','')} · {_label(r)}", va="center", ha="right", fontsize=11.5, color=INK,
                transform=ax.get_yaxis_transform())
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0¢", "25¢", "50¢", "75¢", "100¢"])
    ax.grid(axis="x", color=GRID, zorder=0)
    ax.spines["left"].set_visible(False)
    return _finish(fig, ax, title, subtitle, watermark, path)


def settled_chart(rows: list[dict], title: str, subtitle: str, watermark: str, path: Path) -> Path:
    rows = rows[:8]
    fig = _fig()
    ax = fig.add_axes([0.42, 0.12, 0.54, 0.7])
    ys = list(range(len(rows)))[::-1]
    for y, r in zip(ys, rows):
        p = (r.get("p24") or 0) * 100
        yes = r.get("result") == "yes"
        color = UP if yes else DOWN
        ax.barh(y, p, color=color, height=0.55, zorder=2, alpha=0.9)
        tag = "UPSET · " if r.get("upset") else ""
        ax.text(p + 1.5, y, f"{tag}{round(p):d}¢ → {'YES' if yes else 'NO'}", va="center", ha="left", fontsize=11, color=INK)
        ax.text(-0.03, y, f"{r.get('label','')} · {_label(r)}", va="center", ha="right", fontsize=11.5, color=INK,
                transform=ax.get_yaxis_transform())
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.7, len(rows) - 0.3)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0¢", "25¢", "50¢", "75¢", "100¢"])
    ax.set_xlabel("YES price 24h before settlement", color=MUTED, fontsize=11)
    ax.grid(axis="x", color=GRID, zorder=0)
    ax.spines["left"].set_visible(False)
    return _finish(fig, ax, title, subtitle, watermark, path)


def board_chart(rows: list[dict], title: str, subtitle: str, watermark: str, path: Path) -> Path:
    # group by name; one row per watch item, one dot per venue
    names = []
    for r in rows:
        if r["name"] not in names:
            names.append(r["name"])
    names = names[:10]
    fig = _fig()
    ax = fig.add_axes([0.36, 0.12, 0.6, 0.7])
    ys = {n: i for i, n in enumerate(names[::-1])}
    for r in rows:
        if r["name"] not in ys:
            continue
        y = ys[r["name"]]
        x = (r.get("now") or 0) * 100
        c = VENUE_COLORS.get(r["venue"], INK)
        ax.scatter([x], [y], color=c, s=140, zorder=3)
        # the delta of the rounded endpoints, so the label agrees with the price
        prev = r.get("prev")
        d = r.get("delta")
        if prev is not None:
            move = f" ({round(x) - round(prev * 100):+d})"
        elif d is not None:
            move = f" ({round(d * 100):+d})"
        else:
            move = ""
        ax.text(x, y + 0.28, f"{r.get('label','')} {round(x):d}¢" + move,
                ha="center", va="bottom", fontsize=10.5, color=c)
    for n, y in ys.items():
        ax.text(-0.03, y, n, va="center", ha="right", fontsize=12, color=INK, transform=ax.get_yaxis_transform())
        ax.axhline(y, color=GRID, zorder=0, linewidth=1)
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.7, len(names) - 0.3)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0¢", "25¢", "50¢", "75¢", "100¢"])
    ax.spines["left"].set_visible(False)
    return _finish(fig, ax, title, subtitle, watermark, path)


def scorecard_chart(rows: list[dict], title: str, subtitle: str, watermark: str, path: Path) -> Path:
    fig = _fig()
    ax = fig.add_axes([0.1, 0.14, 0.5, 0.68])
    xs = [i * 10 + 5 for i in range(10)]
    by_bucket = {r["bucket"]: r for r in rows}
    ax.plot([0, 100], [0, 100], color=GRID, linewidth=2, zorder=1)
    for i, x in enumerate(xs):
        r = by_bucket.get(f"{i*10}-{i*10+10}¢")
        if not r or not r.get("n"):
            continue
        rate = (r["rate"] or 0) * 100
        ax.scatter([x], [rate], s=40 + 8 * min(r["n"], 40), color=VENUE_COLORS["kalshi"], alpha=0.85, zorder=3)
        ax.text(x, rate + 4, f"n={r['n']}", ha="center", fontsize=9, color=MUTED)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 105)
    ax.set_xlabel("YES price the day before (¢)", color=MUTED)
    ax.set_ylabel("share that resolved YES (%)", color=MUTED)
    ax.grid(color=GRID, zorder=0)
    return _finish(fig, ax, title, subtitle, watermark, path)


RENDERERS = {
    "movers": movers_chart,
    "settled": settled_chart,
    "board": board_chart,
    "scorecard": scorecard_chart,
}


def render_chart(kind: str, rows: list[dict], title: str, subtitle: str, watermark: str, path: Path) -> Path | None:
    fn = RENDERERS.get(kind)
    if fn is None or not rows:
        return None
    return fn(rows, title, subtitle, watermark, path)
