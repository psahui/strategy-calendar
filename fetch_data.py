#!/usr/bin/env python3
"""Fetch strategy game release data from Steam's keyless storefront endpoints.

Discovers apps via publisher/developer search, pulls per-app metadata from
appdetails (cached in data/cache.json to respect rate limits), filters to
upcoming titles plus releases from the last N months, and writes
data/games.json for the static frontend.

Stdlib only — no API keys, no third-party packages.
"""

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "cache.json"
OUT_PATH = DATA_DIR / "games.json"

SEARCH_URL = "https://store.steampowered.com/search/results/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"
HEADERS = {"User-Agent": "Mozilla/5.0 (strategy-release-calendar; personal hobby project)"}

DETAILS_THROTTLE_SECONDS = 1.6
SEARCH_THROTTLE_SECONDS = 1.0
CACHE_MAX_AGE_DAYS = 30

MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}
MONTHS.update({m[:3]: v for m, v in list(MONTHS.items())})


def http_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 — retry on any transient failure
            wait = 8 * (attempt + 1)
            print(f"    retrying after error ({exc}); waiting {wait}s", flush=True)
            time.sleep(wait)
    print(f"    giving up on {url}", flush=True)
    return None


def parse_release_string(raw):
    """Parse Steam's release-date display string into (precision, y, m, d).

    precision is one of: day, month, quarter, year, tba.
    """
    s = html.unescape((raw or "").strip())
    low = s.lower()
    year_match = re.search(r"(20\d\d)", s)
    year = int(year_match.group(1)) if year_match else None

    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+),?\s+(20\d\d)$", s)
    if m and m.group(2).lower() in MONTHS:
        return "day", int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d\d)$", s)
    if m and m.group(1).lower() in MONTHS:
        return "day", int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))
    m = re.match(r"^([A-Za-z]+),?\s+(20\d\d)$", s)
    if m and m.group(1).lower() in MONTHS:
        return "month", int(m.group(2)), MONTHS[m.group(1).lower()], None
    m = re.search(r"\bq([1-4])\b", low)
    if m and year:
        return "quarter", year, int(m.group(1)) * 3 - 2, None
    if year:
        return "year", year, None, None
    return "tba", None, None, None


def release_sort_key(precision, year, month, day):
    """Numeric key for chronological sorting; unknown parts sort late."""
    y = year or 9998
    mth = month if month else 98
    d = day if day else 98
    if precision == "tba":
        return 99989898
    return y * 10000 + mth * 100 + d


def search_steam(field, value, extra_params):
    """Paginate a storefront search; yield (appid, title, release_string)."""
    start = 0
    while True:
        params = {
            "query": "", "start": start, "count": 50, field: value,
            "infinite": 1, "cc": "us", "l": "english", "category1": "998,21",
        }
        params.update(extra_params)
        url = SEARCH_URL + "?" + urllib.parse.urlencode(params)
        payload = http_json(url)
        if not payload or not payload.get("success"):
            return
        rows_html = payload.get("results_html", "")
        rows = re.findall(
            r'data-ds-appid="(\d+)".*?<span class="title">(.*?)</span>'
            r'.*?search_released[^>]*>\s*(.*?)\s*</div>',
            rows_html, re.S)
        if not rows:
            return
        for appid, title, released in rows:
            yield int(appid), html.unescape(title), re.sub(r"<[^>]+>", "", released).strip()
        start += 50
        total = payload.get("total_count", 0)
        if start >= total:
            return
        time.sleep(SEARCH_THROTTLE_SECONDS)


def discover_apps(config, cutoff_key):
    """Run all configured searches; return {appid: search_release_string}."""
    found = {}
    searches = ([("publisher", p) for p in config["publisher_searches"]]
                + [("developer", d) for d in config["developer_searches"]])
    for field, value in searches:
        print(f"Searching {field} = {value}", flush=True)
        count_before = len(found)

        # Pass 1: everything marked coming soon.
        for appid, _title, released in search_steam(field, value, {"filter": "comingsoon"}):
            found.setdefault(appid, released)

        # Pass 2: released items newest-first; stop once we are clearly past
        # the cutoff (tolerate a few stragglers with odd date strings).
        consecutive_old = 0
        for appid, _title, released in search_steam(field, value, {"sort_by": "Released_DESC"}):
            precision, y, mth, d = parse_release_string(released)
            key = release_sort_key(precision, y, mth, d)
            if precision in ("day", "month") and key < cutoff_key:
                consecutive_old += 1
                if consecutive_old >= 15:
                    break
                continue
            consecutive_old = 0
            found.setdefault(appid, released)
        print(f"  running total: {len(found)} apps (+{len(found) - count_before})", flush=True)
    return found


def load_cache():
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    return {}


def needs_refetch(entry, now):
    if not entry or "data" not in entry:
        return True
    fetched_at = datetime.fromisoformat(entry["fetched_at"])
    age_days = (now - fetched_at).days
    data = entry["data"]
    if data is None:  # previous fetch failed or app is hidden; retry weekly
        return age_days >= 7
    release = data.get("release_date") or {}
    if release.get("coming_soon"):  # unreleased metadata changes often
        return age_days >= 1
    # Recently released titles can flip EA status / fix dates for a while.
    precision, y, mth, d = parse_release_string(release.get("date"))
    if precision == "day":
        try:
            released_on = datetime(y, mth, d, tzinfo=timezone.utc)
            if (now - released_on).days <= 60:
                return age_days >= 3
        except ValueError:
            pass
    return age_days >= CACHE_MAX_AGE_DAYS


def fetch_details(appids, cache):
    now = datetime.now(timezone.utc)
    to_fetch = [a for a in appids if needs_refetch(cache.get(str(a)), now)]
    print(f"appdetails: {len(to_fetch)} to fetch, {len(appids) - len(to_fetch)} cached", flush=True)
    filters = "basic,developers,publishers,release_date,genres,fullgame"
    for i, appid in enumerate(to_fetch, 1):
        url = f"{DETAILS_URL}?appids={appid}&cc=us&l=english&filters={filters}"
        payload = http_json(url)
        entry = (payload or {}).get(str(appid)) or {}
        data = entry.get("data") if entry.get("success") else None
        if data is not None:
            # basic includes bulky description fields we never use
            for junk in ("detailed_description", "about_the_game", "pc_requirements",
                         "mac_requirements", "linux_requirements", "supported_languages",
                         "content_descriptors", "ratings", "demos"):
                data.pop(junk, None)
        cache[str(appid)] = {"fetched_at": now.isoformat(), "data": data}
        if i % 25 == 0 or i == len(to_fetch):
            print(f"  {i}/{len(to_fetch)}", flush=True)
            CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(DETAILS_THROTTLE_SECONDS)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


def map_groups(names, groups):
    """Map raw Steam publisher/developer names onto configured display groups."""
    matched = set()
    lookup = {alias.casefold(): group for group, aliases in groups.items() for alias in aliases}
    for name in names or []:
        group = lookup.get((name or "").casefold())
        if group:
            matched.add(group)
    return sorted(matched)


def watchlist_match(data, config):
    """True if the app genuinely involves a configured publisher/developer."""
    aliases = {a.casefold() for aliases in config["publisher_groups"].values() for a in aliases}
    aliases |= {a.casefold() for aliases in config["developer_groups"].values() for a in aliases}
    aliases |= {s.casefold() for s in config["publisher_searches"] + config["developer_searches"]}
    names = (data.get("publishers") or []) + (data.get("developers") or [])
    return any((n or "").casefold() in aliases for n in names)


def build_items(appids, cache, config, cutoff_key):
    minor_dlc = [re.compile(p, re.I) for p in config["minor_dlc_patterns"]]
    include = set(config["include_appids"])
    exclude = set(config["exclude_appids"])
    items, skipped = [], {"type": 0, "watchlist": 0, "minor_dlc": 0, "old": 0, "nodata": 0}

    for appid in appids:
        entry = cache.get(str(appid)) or {}
        data = entry.get("data")
        if not data:
            skipped["nodata"] += 1
            continue
        if appid in exclude:
            continue
        if data.get("type") not in ("game", "dlc"):
            skipped["type"] += 1
            continue
        if appid not in include and not watchlist_match(data, config):
            skipped["watchlist"] += 1
            continue
        name = data.get("name") or f"App {appid}"
        if (appid not in include and data["type"] == "dlc"
                and any(p.search(name) for p in minor_dlc)):
            skipped["minor_dlc"] += 1
            continue

        release = data.get("release_date") or {}
        coming_soon = bool(release.get("coming_soon"))
        raw_date = (release.get("date") or "").strip()
        precision, y, mth, d = parse_release_string(raw_date)
        sort_key = release_sort_key(precision, y, mth, d)
        if not coming_soon and sort_key < cutoff_key:
            skipped["old"] += 1
            continue

        genres = [g.get("description", "") for g in data.get("genres") or []]
        if coming_soon:
            status = "upcoming"
        elif "Early Access" in genres:
            status = "early_access"
        else:
            status = "released"

        fullgame = data.get("fullgame") or None
        items.append({
            "appid": appid,
            "name": name,
            "type": data["type"],
            "status": status,
            "date_string": raw_date or "Coming soon",
            "precision": precision,
            "sort_key": sort_key,
            "developers": data.get("developers") or [],
            "publishers": data.get("publishers") or [],
            "dev_groups": map_groups(data.get("developers"), config["developer_groups"]) or ["Other developers"],
            "pub_groups": map_groups(data.get("publishers"), config["publisher_groups"]) or ["Other publishers"],
            "url": f"https://store.steampowered.com/app/{appid}/",
            "capsule": data.get("capsule_image") or data.get("header_image") or "",
            "parent": {"appid": int(fullgame["appid"]), "name": fullgame["name"]} if fullgame else None,
        })

    print(f"kept {len(items)} items; skipped {skipped}", flush=True)
    return items


def main():
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    DATA_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config["max_age_months"] * 30.44)
    cutoff_key = cutoff.year * 10000 + cutoff.month * 100 + cutoff.day

    found = discover_apps(config, cutoff_key)
    for appid in config["include_appids"]:
        found.setdefault(int(appid), "")
    print(f"discovered {len(found)} candidate apps", flush=True)

    cache = load_cache()
    fetch_details(sorted(found), cache)
    items = build_items(sorted(found), cache, config, cutoff_key)
    items.sort(key=lambda it: (it["sort_key"], it["name"].casefold()))

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "cutoff": cutoff.date().isoformat(),
        "publisher_groups": list(config["publisher_groups"]) + ["Other publishers"],
        "developer_groups": list(config["developer_groups"]) + ["Other developers"],
        "items": items,
    }
    OUT_PATH.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {OUT_PATH} with {len(items)} items", flush=True)


if __name__ == "__main__":
    sys.exit(main())
