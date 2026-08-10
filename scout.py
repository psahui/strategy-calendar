#!/usr/bin/env python3
"""Scout: find upcoming strategy games from studios the calendar doesn't track.

Sweeps Steam's coming-soon lists under a set of strategy tags, discards
everything already tracked or previously dismissed, details what's left
(reusing the main script's cache and throttling), and writes a review report
to data/scout.json for the admin page's Scout tab.

Deliberately changes nothing on the site: the report is a reading list.
"""

import json
import sys
from datetime import datetime, timezone

import fetch_data as fd

# Steam tag IDs, verified against live store pages.
SWEEP_TAGS = {
    "Strategy": 9,
    "Wargame": 4684,
    "Grand Strategy": 4364,
    "4X": 1670,
    "Turn-Based Strategy": 1741,
    "Board Game": 1770,
    "Tabletop": 17389,
}

# At most this many new games get detailed per run; the rest queue for the
# next run (details are cached, so the backlog drains quickly).
DETAIL_CAP = 250

SCOUT_PATH = fd.DATA_DIR / "scout.json"


def tracked_studio_names(config):
    """Every studio name the calendar already watches, lowercased."""
    names = set()
    for group in list(config["publisher_groups"].values()) + list(config["developer_groups"].values()):
        names |= {a.strip().casefold() for a in group}
    radar = config.get("radar_searches", {})
    names |= {s.strip().casefold() for s in
              config["publisher_searches"] + config["developer_searches"]
              + radar.get("publishers", []) + radar.get("developers", [])}
    return names


def main():
    config = json.loads((fd.ROOT / "config.json").read_text(encoding="utf-8"))
    dismissed = set(config.get("scout_dismissed", []))

    tracked_appids = set()
    if fd.OUT_PATH.exists():
        games = json.loads(fd.OUT_PATH.read_text(encoding="utf-8"))
        tracked_appids = {it["appid"] for it in games.get("items", [])}

    # Sweep each tag's coming-soon list (games only, no DLC).
    discovered = {}   # appid -> {"name", "released", "tags"}
    failures = []
    for tag_name, tag_id in SWEEP_TAGS.items():
        print(f"Sweeping tag: {tag_name}", flush=True)
        try:
            for appid, title, released in fd.search_steam(
                    "tags", tag_id, {"filter": "comingsoon", "category1": "998"}):
                entry = discovered.setdefault(
                    appid, {"name": title, "released": released, "tags": []})
                entry["tags"].append(tag_name)
        except fd.SearchFailure as exc:
            print(f"  SWEEP FAILED: {exc}", flush=True)
            failures.append(f"{tag_name}: {exc}")
        print(f"  running total: {len(discovered)} coming-soon games", flush=True)

    if len(failures) == len(SWEEP_TAGS):
        print("ABORTING: every sweep failed; not writing a report.", flush=True)
        return 1

    candidates = [a for a in discovered
                  if a not in tracked_appids and a not in dismissed]
    print(f"{len(candidates)} candidates after removing tracked/dismissed", flush=True)

    # Detail the candidates (studio names + demo flag), capped per run.
    cache = fd.load_cache()
    now = datetime.now(timezone.utc)
    uncached = [a for a in candidates if fd.needs_refetch(cache.get(str(a)), now)]
    detail_now = [a for a in candidates if a not in set(uncached)] + uncached[:DETAIL_CAP]
    pending = max(0, len(uncached) - DETAIL_CAP)
    if pending:
        print(f"detail cap reached: {pending} games queued for the next run", flush=True)
    fd.fetch_details(sorted(detail_now), cache)

    tracked_names = tracked_studio_names(config)
    items = []
    for appid in detail_now:
        data = (cache.get(str(appid)) or {}).get("data")
        if not data or data.get("type") != "game":
            continue
        studios = (data.get("developers") or []) + (data.get("publishers") or [])
        # A tracked studio's new game reaches the calendar by itself — skip.
        if any((s or "").strip().casefold() in tracked_names for s in studios):
            continue
        release = data.get("release_date") or {}
        raw_date = (release.get("date") or "").strip() or "Coming soon"
        precision, y, mth, d = fd.parse_release_string(raw_date)
        items.append({
            "appid": appid,
            "name": data.get("name") or discovered[appid]["name"],
            "developers": data.get("developers") or [],
            "publishers": data.get("publishers") or [],
            "tags": discovered[appid]["tags"],
            "date_string": raw_date,
            "sort_key": fd.release_sort_key(precision, y, mth, d),
            "has_demo": bool(data.get("demos")),
            "url": f"https://store.steampowered.com/app/{appid}/",
        })
    items.sort(key=lambda it: (it["sort_key"], it["name"].casefold()))

    SCOUT_PATH.write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "pending_details": pending,
        "search_failures": failures,
        "items": items,
    }, indent=1), encoding="utf-8")
    print(f"wrote {SCOUT_PATH} with {len(items)} candidate games "
          f"({sum(1 for i in items if i['has_demo'])} with demos)", flush=True)


if __name__ == "__main__":
    sys.exit(main())
