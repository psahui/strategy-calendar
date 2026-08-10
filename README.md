# Strategy Release Calendar

A simple, static calendar of strategy game releases — upcoming titles plus the
last twelve months — from a watchlist of publishers and studios: Paradox
Interactive, Hooded Horse, Slitherine/Matrix, Ubisoft Mainz, Creative Assembly,
Firaxis, Illwinter, Amplitude, Mohawk, VR Designs, and Ashdar Games.

**Live site:** https://releases.matchstickeyes.com/

Built for [Matchsticks for my Eyes](https://www.matchstickeyes.com).

This is a personal project shared as-is: maintained for my own use, with no
support, roadmap, or response-time commitments implied. See the Licence
section below for reuse terms.

## How it works

- `fetch_data.py` (Python, standard library only, no API keys) discovers games
  via Steam's public storefront search (by publisher and by developer), pulls
  each game's details from the `appdetails` endpoint with a local cache
  (`data/cache.json`) to respect rate limits, and writes `data/games.json`.
- For Early Access titles, the fetch also reads the "Leaving Early Access"
  date from each store page (Steam shows it there but omits it from the API)
  and adds the full release to the upcoming timeline and iCal feeds.
- `index.html` is a single static page (vanilla JS) that renders the timeline
  with client-side filters for publisher, developer, game/DLC,
  released / early access / upcoming, and demo availability.
- A GitHub Action (`.github/workflows/refresh.yml`) re-runs the fetch daily
  and commits the updated data; GitHub Pages serves the result. A sanity check
  keeps the previous data if a run finds far fewer titles than expected —
  the usual sign that Steam has changed its page markup.

All release data comes verbatim from each game's Steam store page — the page
maintained by the publisher itself.

## Configuration

Everything editable lives in `config.json`:

- `publisher_searches` / `developer_searches` — which Steam searches to run.
  Note that Steam matches these strings exactly, including case and stray
  whitespace ("CREATIVE ASSEMBLY" and " Slitherine Ltd." are real examples).
- `publisher_groups` / `developer_groups` — how raw Steam names map onto the
  filter checkboxes (aliases supported)
- `minor_dlc_patterns` — case-insensitive regexes that flag minor DLC
  (soundtracks, art books, cosmetic packs), hidden by default behind a
  filter checkbox
- `include_appids` / `exclude_appids` — per-title overrides when a pattern
  gets something wrong
- `collections` — named curated groupings (e.g. Wargames, Matchsticks Picks)
  shown as the top filter group. Each collection lists `publishers` and
  `developers` by their **group display names** (the keys of the group maps
  above — the fetch fails loudly if a name doesn't match), plus optional
  per-title `appids`. A title can belong to several collections; anything
  matching no *taxonomy* collection falls into an implicit "Strategy
  (general)". A collection marked `"overlay": true` (e.g. Matchsticks Picks)
  is an endorsement that cuts across the taxonomy without replacing it — a
  picked title keeps its Wargames or Strategy (general) shelf. Every collection
  also gets its own iCal feed (`wargames.ics`, `matchsticks-picks.ics`, …).
- `radar_searches` — a second, lower-tier shopping list (currently empty).
  Studios added here are fetched like everything else but get no sidebar
  checkbox; their titles land in an "Indie radar" collection that is hidden
  by default and only appears once the list has entries.
- `blog_links` — manual map of Steam appid → Matchsticks for my Eyes URL;
  adds a "Read on Matchsticks" link to that title's card. The appid is the
  number in the Steam URL (`store.steampowered.com/app/1176470/...` → `"1176470"`).
- `max_age_months` — how far back the "recently released" window reaches

Config changes only reach the live site after the fetch runs — pushing
`config.json` alone redeploys nothing new. The fetch runs on a daily schedule
(about 04:20 AEST); to apply a change immediately, trigger it by hand from the
repository's **Actions tab → Refresh Steam data → Run workflow**, or with:

```bash
gh workflow run refresh.yml
```

Run locally with:

```bash
python fetch_data.py
python -m http.server 8000
```

then open http://localhost:8000.

## The Scout

`scout.py` is a research assistant, not part of the website: it sweeps
Steam's coming-soon lists under a set of strategy tags, discards everything
the calendar already tracks (and anything previously dismissed), and writes
`data/scout.json` — a reading list of upcoming games from unknown studios,
with a demo flag for Next Fest browsing. It runs weekly via
`.github/workflows/scout.yml` (or on demand from the Actions page), and the
report is reviewed on `admin.html`, where each game can be sent to the radar,
the watchlist, or dismissed for good (`scout_dismissed` in config).

## Feeds

Each refresh also writes iCal feeds (`calendar.ics`, `paradox.ics`,
`hooded-horse.ics`, `slitherine-matrix.ics`) — subscribe from any calendar app
via the URL. Only upcoming releases with confirmed dates become calendar
events; titles dated "Q4 2026" or "coming soon" are left out until Steam
shows a real date. Review summaries for released titles come from Steam's
public `appreviews` endpoint, refreshed weekly.

## Licence

The code in this repository is released under the MIT Licence — see
[LICENCE](LICENCE). In short: use, modify, and redistribute it freely
(commercial use included), keep the copyright notice with any copies, and
accept that it comes with no warranty.

Not covered by the licence:

- The **Matchsticks for my Eyes name and logo** (`assets/logo.png`), which
  remain the property of Peter Sahui and are not licensed for reuse — if you
  fork this, swap in your own branding.
- The contents of **`data/`** and the **`.ics` feeds**, which are factual
  release data sourced from Steam; game names and images belong to their
  respective publishers.
