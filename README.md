# Strategy Release Calendar

A simple, static calendar of strategy game releases — upcoming titles plus the
last twelve months — from a watchlist of publishers and studios: Paradox
Interactive, Hooded Horse, Slitherine/Matrix, Ubisoft Mainz, Creative Assembly,
Firaxis, Illwinter, Amplitude, Mohawk, VR Designs and friends.

**Live site:** https://psahui.github.io/strategy-calendar/

Built for [Matchsticks for my Eyes](https://www.matchstickeyes.com).

This is a personal project shared as-is: maintained for my own use, with no
support, roadmap, or response-time commitments implied. The Matchsticks for my
Eyes name and logo are not licensed for reuse.

## How it works

- `fetch_data.py` (Python, stdlib only, no API keys) discovers apps via Steam's
  keyless storefront search (by publisher and by developer), pulls metadata from
  the `appdetails` endpoint with a local cache (`data/cache.json`) to respect
  rate limits, and writes `data/games.json`.
- `index.html` is a single static page (vanilla JS) that renders the timeline
  with client-side filters for publisher, developer, game/DLC, and
  released / early access / upcoming.
- A GitHub Action (`.github/workflows/refresh.yml`) re-runs the fetch daily and
  commits the updated data; GitHub Pages serves the result.

All release data comes verbatim from each game's Steam store page — the page
maintained by the publisher itself. No AI-generated content.

## Configuration

Everything editable lives in `config.json`:

- `publisher_searches` / `developer_searches` — which Steam searches to run
- `publisher_groups` / `developer_groups` — how raw Steam names map onto the
  filter checkboxes (aliases supported)
- `minor_dlc_patterns` — case-insensitive regexes that hide noise DLC
  (soundtracks, art books, cosmetic packs…)
- `include_appids` / `exclude_appids` — per-title overrides when a pattern gets
  something wrong
- `blog_links` — manual map of Steam appid → Matchsticks for my Eyes URL; adds a
  "Read on Matchsticks" link to that title's card. The appid is the number in
  the Steam URL (`store.steampowered.com/app/1176470/...` → `"1176470"`).
- `max_age_months` — how far back the "recently released" window reaches

## Feeds

Each refresh also writes iCal feeds of concrete-dated upcoming releases
(`calendar.ics`, `paradox.ics`, `hooded-horse.ics`, `slitherine-matrix.ics`) —
subscribe from any calendar app via the URL. Review summaries for released
titles come from Steam's public `appreviews` endpoint, refreshed weekly.

Run locally with:

```bash
python fetch_data.py
python -m http.server 8000
```

then open http://localhost:8000.
