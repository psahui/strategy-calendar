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


CACHE_SCHEMA = 2  # bump when fetch_details stores new fields, to invalidate old entries


def needs_refetch(entry, now):
    if not entry or "data" not in entry:
        return True
    if entry.get("v") != CACHE_SCHEMA:
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
    filters = "basic,developers,publishers,release_date,genres,fullgame,demos"
    for i, appid in enumerate(to_fetch, 1):
        url = f"{DETAILS_URL}?appids={appid}&cc=us&l=english&filters={filters}"
        payload = http_json(url)
        entry = (payload or {}).get(str(appid)) or {}
        data = entry.get("data") if entry.get("success") else None
        if data is not None:
            # basic includes bulky description fields we never use ("demos" stays —
            # it powers the Demo badge)
            for junk in ("detailed_description", "about_the_game", "pc_requirements",
                         "mac_requirements", "linux_requirements", "supported_languages",
                         "content_descriptors", "ratings"):
                data.pop(junk, None)
            # Steam omits "demos" when there is none; keep the key so the
            # cache-upgrade check in needs_refetch doesn't refetch forever.
            data.setdefault("demos", [])
        cache[str(appid)] = {"fetched_at": now.isoformat(), "v": CACHE_SCHEMA, "data": data}
        if i % 25 == 0 or i == len(to_fetch):
            print(f"  {i}/{len(to_fetch)}", flush=True)
            CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(DETAILS_THROTTLE_SECONDS)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


REVIEWS_URL = "https://store.steampowered.com/appreviews/"
REVIEW_MAX_AGE_DAYS = 7


def fetch_reviews(appids, cache):
    """Fetch Steam review summaries (keyless) for released titles, weekly-cached.

    Returns {appid: {"desc", "pct", "total"}} for titles with at least one review.
    """
    store = cache.setdefault("_reviews", {})
    now = datetime.now(timezone.utc)
    to_fetch = []
    for appid in appids:
        entry = store.get(str(appid))
        if not entry or (now - datetime.fromisoformat(entry["fetched_at"])).days >= REVIEW_MAX_AGE_DAYS:
            to_fetch.append(appid)
    print(f"reviews: {len(to_fetch)} to fetch, {len(appids) - len(to_fetch)} cached", flush=True)
    for i, appid in enumerate(to_fetch, 1):
        url = f"{REVIEWS_URL}{appid}?json=1&language=all&purchase_type=all&num_per_page=0"
        payload = http_json(url)
        summary = (payload or {}).get("query_summary") or {}
        store[str(appid)] = {"fetched_at": now.isoformat(), "summary": summary}
        if i % 25 == 0 or i == len(to_fetch):
            print(f"  {i}/{len(to_fetch)}", flush=True)
            CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
        time.sleep(DETAILS_THROTTLE_SECONDS)
    if to_fetch:
        CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")

    out = {}
    for appid in appids:
        s = (store.get(str(appid)) or {}).get("summary") or {}
        total = s.get("total_reviews") or 0
        if total > 0:
            out[appid] = {
                "desc": s.get("review_score_desc", ""),
                "pct": round(100 * (s.get("total_positive") or 0) / total),
                "total": total,
            }
    return out


def map_groups(names, groups):
    """Map raw Steam publisher/developer names onto configured display groups."""
    matched = set()
    lookup = {alias.strip().casefold(): group for group, aliases in groups.items() for alias in aliases}
    for name in names or []:
        group = lookup.get((name or "").strip().casefold())
        if group:
            matched.add(group)
    return sorted(matched)


def watchlist_match(data, config):
    """True if the app genuinely involves a configured publisher/developer."""
    aliases = {a.strip().casefold() for aliases in config["publisher_groups"].values() for a in aliases}
    aliases |= {a.strip().casefold() for aliases in config["developer_groups"].values() for a in aliases}
    aliases |= {s.strip().casefold() for s in config["publisher_searches"] + config["developer_searches"]}
    names = (data.get("publishers") or []) + (data.get("developers") or [])
    return any((n or "").strip().casefold() in aliases for n in names)


def build_items(appids, cache, config, cutoff_key):
    minor_dlc = [re.compile(p, re.I) for p in config["minor_dlc_patterns"]]
    include = set(config["include_appids"])
    exclude = set(config["exclude_appids"])
    items, skipped = [], {"type": 0, "watchlist": 0, "old": 0, "nodata": 0}
    minor_count = 0

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
        # Minor DLC (soundtracks, cosmetics…) stays in the data, flagged, so the
        # frontend can expose it as an explicit checkbox rather than hiding it.
        is_minor = (appid not in include and data["type"] == "dlc"
                    and any(p.search(name) for p in minor_dlc))
        if is_minor:
            minor_count += 1

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
            "kind": "minor_dlc" if is_minor else data["type"],
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
            "has_demo": bool(data.get("demos")),
            "blog_url": config["blog_links"].get(str(appid)),
        })

    print(f"kept {len(items)} items ({minor_count} flagged minor DLC); skipped {skipped}", flush=True)
    return items


def ics_escape(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def ics_fold(line):
    """RFC 5545 line folding: continuation lines start with a space."""
    chunks = []
    while len(line) > 70:
        chunks.append(line[:70])
        line = " " + line[70:]
    chunks.append(line)
    return "\r\n".join(chunks)


def write_ics(path, cal_name, items, generated_at):
    """Write an iCal feed of concrete-dated upcoming releases (all-day events)."""
    stamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "CALSCALE:GREGORIAN",
        "PRODID:-//Matchsticks for my Eyes//Strategy Release Calendar//EN",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        "X-PUBLISHED-TTL:P1D", "REFRESH-INTERVAL;VALUE=DURATION:P1D",
    ]
    for it in items:
        y, m, d = (it["sort_key"] // 10000, it["sort_key"] // 100 % 100, it["sort_key"] % 100)
        start = f"{y:04d}{m:02d}{d:02d}"
        end_dt = datetime(y, m, d) + timedelta(days=1)
        kind = " (DLC)" if it["type"] == "dlc" else ""
        desc = (f"{', '.join(it['developers'])} / {', '.join(it['publishers'])}"
                f"\nSteam: {it['url']}")
        lines += [
            "BEGIN:VEVENT",
            f"UID:app-{it['appid']}@psahui.github.io",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end_dt.strftime('%Y%m%d')}",
            f"SUMMARY:{ics_escape(it['name'] + kind)}",
            f"DESCRIPTION:{ics_escape(desc)}",
            f"URL:{it['url']}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(ics_fold(l) for l in lines) + "\r\n", encoding="utf-8")
    print(f"wrote {path.name} with {len(items)} events", flush=True)


ICS_FEEDS = {
    "calendar.ics": ("Strategy Release Calendar", None),
    "paradox.ics": ("Strategy Releases — Paradox", "Paradox Interactive"),
    "hooded-horse.ics": ("Strategy Releases — Hooded Horse", "Hooded Horse"),
    "slitherine-matrix.ics": ("Strategy Releases — Slitherine/Matrix", "Slitherine / Matrix"),
}


def write_feeds(items, generated_at):
    dated = [it for it in items
             if it["status"] == "upcoming" and it["precision"] == "day"
             and it["kind"] != "minor_dlc"]
    for filename, (cal_name, pub_group) in ICS_FEEDS.items():
        subset = [it for it in dated if pub_group is None or pub_group in it["pub_groups"]]
        write_ics(ROOT / filename, cal_name, subset, generated_at)


def sanity_check(items):
    """Refuse to overwrite good data with a collapsed dataset (broken scrape)."""
    if not OUT_PATH.exists():
        return len(items) > 0
    prev = len(json.loads(OUT_PATH.read_text(encoding="utf-8")).get("items", []))
    if prev >= 20 and len(items) < 0.7 * prev:
        print(f"SANITY FAIL: item count collapsed {prev} -> {len(items)}; "
              "Steam markup may have changed. Keeping existing data.", flush=True)
        return False
    return True


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

    if not sanity_check(items):
        return 1

    reviews = fetch_reviews([it["appid"] for it in items if it["status"] != "upcoming"], cache)
    for it in items:
        it["review"] = reviews.get(it["appid"])

    write_feeds(items, now)

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
