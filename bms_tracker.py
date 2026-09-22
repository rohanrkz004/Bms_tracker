"""
AMB Cinemas (Gachibowli, Hyderabad) show tracker for BookMyShow.

Every BookMyShow showtimes page ships the FULL dataset as structured JSON
inline in the HTML, assigned to `window.__INITIAL_STATE__`. This script reads
that JSON directly instead of guessing movie/time/screen/status from
rendered text with regex -- so it doesn't silently break when BMS tweaks its
DOM/CSS, and it gets fields (per-show subtext like IMAX/GOLD/DOLBY ATMOS,
exact availability code, screen name, price) that are effectively impossible
to recover reliably from the rendered page alone.

Requires: pip install playwright   &&   playwright install firefox
"""

import json
import re
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TARGET_DATE = "20260925"
TARGET_LABEL = "25 September 2026"

# NOTE: verify this against a real browser before relying on it. Open
#   https://in.bookmyshow.com/cinemas/hyderabad/amb-cinemas-gachibowli/buytickets/AMBH/20260925
# in an actual browser -- BMS 301-redirects the slug to the canonical one, and
# the 3-5 char code after /buytickets/ in the FINAL url is the real venue
# code. If it doesn't say AMBH, update VENUE_CODE below.
VENUE_CODE = "AMBH"
VENUE_NAME = "AMB Cinemas: Gachibowli"
CITY_SLUG = "hyderabad"
VENUE_SLUG = "amb-cinemas-gachibowli"  # cosmetic only -- BMS resolves on VENUE_CODE

BMS_URL = (
    f"https://in.bookmyshow.com/cinemas/{CITY_SLUG}/{VENUE_SLUG}"
    f"/buytickets/{VENUE_CODE}/{TARGET_DATE}"
)

STATE_FILE = Path(f"amb_{TARGET_DATE}_state.json")
STATE_MARKER = "window.__INITIAL_STATE__"

# ---------------------------------------------------------------------------
# Extracting BMS's embedded JSON
# ---------------------------------------------------------------------------


def extract_initial_state(html: str):
    """Brace-match `window.__INITIAL_STATE__ = {...};` out of raw HTML.

    A naive regex truncates on the first '}' it sees, which is wrong because
    the object is deeply nested. This walks the string tracking string
    literals/escapes so nested braces don't confuse it.
    """
    m = re.search(re.escape(STATE_MARKER) + r"\s*=\s*", html)
    if not m:
        return None
    i = m.end()
    depth = 0
    start = -1
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if start == -1:
            if c == "{":
                start, depth = i, 1
            i += 1
            continue
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    return None


def availability_from(code):
    """BMS AvailStatus: 3=available, 1/2=fast filling, 0=sold out."""
    return {3: "AVAILABLE", 2: "FAST FILLING", 1: "FAST FILLING", 0: "SOLD OUT"}.get(
        code, "UNKNOWN"
    )


def shows_from_state(state: dict):
    """Pull the getShowtimesByVenue-<CODE>-<DATE> cache entry and flatten it
    into one row per showtime."""
    queries = state.get("venueShowtimesFunctionalApi", {}).get("queries", {}) or {}
    wanted_prefix = f"getShowtimesByVenue-{VENUE_CODE}-{TARGET_DATE}"

    data = None
    for k, v in queries.items():
        if k.startswith(wanted_prefix):
            data = v.get("data")
            break

    if data is None:
        raise RuntimeError(
            f"No '{wanted_prefix}' entry in __INITIAL_STATE__. "
            "VENUE_CODE or TARGET_DATE is probably wrong -- check them "
            "against a real browser session."
        )

    if data.get("AllShowDatesDisabled") or data.get("DownCinemasMessage"):
        msg = data.get("DownCinemasMessage") or data.get("DownCinemasTitle")
        print(f"[INFO] Venue reports no sellable shows for this date: {msg}")
        return []

    events = (data.get("showDetailsTransformed") or {}).get("Event") or []

    shows = []
    for event in events:
        movie = (event.get("EventTitle") or "").strip()
        # Rating/genre/language/format live on ChildEvents, not on Event itself.
        for child in event.get("ChildEvents") or []:
            fmt = (child.get("EventDimension") or "").strip()
            lang = (child.get("EventLanguage") or "").strip()
            language_format = ", ".join(x for x in (lang, fmt) if x)
            for st in child.get("ShowTimes") or []:
                shows.append(
                    {
                        "movie": movie,
                        "language_format": language_format,
                        "time": (st.get("ShowTime") or "").strip(),
                        "datetime": st.get("ShowDateTime") or "",
                        "screen": (st.get("ScreenName") or "Not shown").strip(),
                        "subtext": (st.get("Attributes") or "").strip() or None,
                        "status": availability_from(st.get("AvailStatus")),
                        "session_id": st.get("SessionId") or st.get("ShowTimeCode") or "",
                        "min_price": st.get("MinPrice"),
                        "max_price": st.get("MaxPrice"),
                    }
                )

    shows.sort(key=lambda s: (s["movie"].lower(), s["time"], s["screen"]))
    return shows


# ---------------------------------------------------------------------------
# Fetching the page
# ---------------------------------------------------------------------------


def scrape_via_browser():
    """Primary path: a real (headless) browser so any Cloudflare JS
    challenge actually gets solved, then read the JSON straight out of the
    page's own JS context -- no DOM/text guessing at all."""
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) "
                "Gecko/20100101 Firefox/155.0"
            ),
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://in.bookmyshow.com/",
            },
        )
        context.add_cookies(
            [
                {
                    "name": "Rgn",
                    "value": "Code=HYD|text=Hyderabad",
                    "domain": "in.bookmyshow.com",
                    "path": "/",
                }
            ]
        )
        page = context.new_page()
        response = page.goto(BMS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        status = response.status if response else 0
        raw_state = page.evaluate(
            f"() => {STATE_MARKER} ? JSON.stringify({STATE_MARKER}) : null"
        )
        browser.close()

        if status != 200 or not raw_state:
            print(
                f"[WARN] Browser fetch unreliable (HTTP {status}, "
                f"state present={bool(raw_state)})."
            )
            return None
        return json.loads(raw_state)


def scrape_via_reader():
    """Fallback for when BMS blocks the runner's IP outright (common on
    GitHub Actions/cloud datacenter IP ranges). Ask Jina Reader for RAW HTML
    (not its default cleaned-up markdown) so __INITIAL_STATE__ survives and
    we can parse it with the exact same structured-JSON logic."""
    req = urllib.request.Request(
        f"https://r.jina.ai/{BMS_URL}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,*/*",
            "X-Return-Format": "html",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        html = resp.read().decode("utf-8", errors="replace")
        print(f"[INFO] Reader HTTP {resp.status}; {len(html):,} chars")

    raw_state = extract_initial_state(html)
    if not raw_state:
        raise RuntimeError(
            "Reader fallback did not return an extractable __INITIAL_STATE__ "
            "block (BMS or Jina may have changed behaviour)."
        )
    return json.loads(raw_state)


def scrape():
    state = None
    try:
        state = scrape_via_browser()
    except Exception as exc:
        print(f"[WARN] Browser scrape failed: {exc}")

    if state is None:
        print("[INFO] Falling back to Reader fetch.")
        state = scrape_via_reader()

    return shows_from_state(state)


# ---------------------------------------------------------------------------
# State diffing
# ---------------------------------------------------------------------------


def load_state():
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("shows", []) if isinstance(data, dict) else []
    except Exception:
        return []


def key(s):
    return "|".join(
        [s.get("movie", ""), s.get("language_format", ""), s.get("time", ""), s.get("screen", "")]
    )


def main():
    try:
        shows = scrape()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1

    if not shows:
        # Could be a legitimately dark day, or a parsing miss. Either way,
        # don't overwrite good prior state with an empty result.
        print("[INFO] No shows parsed this run; state file left unchanged.")
        return 0

    old = {key(s): s for s in load_state()}
    new = {key(s): s for s in shows}

    added = [new[k] for k in sorted(set(new) - set(old))]
    removed = [old[k] for k in sorted(set(old) - set(new))]
    changed = [
        (old[k], new[k])
        for k in sorted(set(old) & set(new))
        if old[k].get("status") != new[k].get("status")
    ]

    print(f"[INFO] Parsed {len(shows)} AMB shows for {TARGET_LABEL}")
    movies = sorted({s["movie"] for s in shows})
    print(f"[INFO] Movies found: {len(movies)}")
    for movie in movies:
        rows = [s for s in shows if s["movie"] == movie]
        print(f"\n[{movie}]")
        for s in rows:
            sub = f" ({s['subtext']})" if s.get("subtext") else ""
            print(f"  {s['time']} | {s['language_format']} | {s['screen']}{sub} | {s['status']}")

    if not old:
        print("\n[INFO] First successful scrape: creating baseline only.")
    else:
        print(f"\n[CHANGES] added={len(added)} removed={len(removed)} status_changed={len(changed)}")
        for s in added:
            print(f"+ {s['movie']} | {s['time']} | {s['screen']} | {s['status']}")
        for s in removed:
            print(f"- {s['movie']} | {s['time']} | {s['screen']}")
        for a, b in changed:
            print(f"~ {b['movie']} | {b['time']} | {a['status']} -> {b['status']}")

    STATE_FILE.write_text(
        json.dumps(
            {
                "venue": VENUE_NAME,
                "venue_code": VENUE_CODE,
                "target_date": TARGET_DATE,
                "shows": shows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[INFO] State saved to {STATE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
