OddsDrift is an automated, non-commercial bot. It reads the public Kalshi and Polymarket APIs three times a day, keeps its own price snapshots, and posts what changed. Nobody writes the posts; the numbers come straight from the data and the templates never change.

## What it posts
- **Morning movers (13:17 UTC):** the biggest 24-hour swings in YES price across both venues, ranked by size of the move times log volume. Sports props are excluded. Markets that close within 12 hours are excluded.
- **Midterms 2026 board (17:17 UTC, Mon–Sat until Election Day):** the same set of control-of-Congress and marquee-race markets, same layout every day, with a countdown.
- **Settled & surprised (22:17 UTC):** what resolved in the last 24 hours, upsets first. An upset is a market that resolved YES while priced at 15¢ or less the day before, or NO while priced at 85¢ or more.
- **Calibration scorecard (Sundays):** for markets that resolved this week, how the price 24 hours before settlement compared with the outcome, plus Brier scores by venue.

## Method notes
- Prices are the last traded YES price at the time of the run. Polymarket multi-outcome markets use the first listed outcome as YES.
- 24-hour changes come from our own snapshot taken about 24 hours earlier. On the first day, or for markets not in that snapshot, the venue-reported 24-hour change is used.
- The bot never replies, follows, likes, or mentions anyone. It never posts numbers it did not compute.
- Every post links to a page with the full table and to the venue's market page.

## Data
Snapshots, the resolutions ledger, and the post index live in the public GitHub repo under `audiences/oddsdrift/data/`, licensed CC BY 4.0. Cite as "OddsDrift (github.com/estrada-josh/attention)".
