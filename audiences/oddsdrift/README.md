# OddsDrift — data

Automated prediction-market odds tracker. Site: https://oddsdrift.joshestrada.com
Follow on Bluesky: `@oddsdrift.joshestrada.com.web.brid.gy` (custom handle pending) · RSS: `/feed.xml`

## Files
- `data/snapshots/<venue>/<UTC>.csv.gz` — per-run price snapshots (non-sports, active markets; 45-day window)
- `data/resolutions.csv` — every non-sports market that settled, with the YES price 24h before settlement
- `data/posts.json` — every post the bot published (text, table, chart path)
- `data/metrics.csv` — daily healthcheck: followers, stars, site status
- `site/` — the generated static site (served by the Worker)

License: CC BY 4.0 (see `../../DATA_LICENSE`). Method: `about.md`.
