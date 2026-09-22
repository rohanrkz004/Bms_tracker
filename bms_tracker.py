import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

TARGET_DATE = "20260925"
TARGET_LABEL = "25 September 2026"
VENUE = "AMB Cinemas: Gachibowli"
BMS_URL = f"https://in.bookmyshow.com/cinemas/hyderabad/amb-cinemas-gachibowli/buytickets/AMBH/{TARGET_DATE}"
STATE_FILE = Path("amb_20260925_state.json")

TIME_RE = re.compile(r"\b(?:0?[1-9]|1[0-2])[:.]\d{2}\s*(?:AM|PM)\b", re.I)
META_RE = re.compile(r"^[A-Za-z][A-Za-z +&-]*,\s*(?:2D|3D|4DX|IMAX|MX4D|DOLBY CINEMA)(?:\s+\w+)*$", re.I)
RATING_RE = re.compile(r"\s*\((?:U|A|UA\d*\+?)\)\s*$", re.I)
SCREEN_WORDS = ("SCREEN", "LASER", "DOLBY", "ATMOS", "BARCO", "HDR", "LUXE", "INFINITY", "PXL", "VIP", "IMAX", "4DX")


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm_time(s):
    m = TIME_RE.search(s or "")
    if not m:
        return ""
    raw = clean(m.group()).upper().replace(".", ":")
    h, tail = raw.split(":", 1)
    mm, ap = tail.split()
    return f"{int(h):02d}:{mm} {ap}"


def status_from(el):
    """Best-effort status from the show element and nearby DOM."""
    parts = []
    node = el
    for _ in range(5):
        if node is None:
            break
        try:
            parts.extend([
                node.inner_text(timeout=1000) or "",
                node.get_attribute("class") or "",
                node.get_attribute("aria-label") or "",
                node.get_attribute("title") or "",
                node.get_attribute("data-status") or "",
            ])
        except Exception:
            pass
        try:
            node = node.locator("..")
        except Exception:
            break
    blob = clean(" ".join(parts)).lower()
    if any(x in blob for x in ("sold out", "house full", "unavailable")):
        return "SOLD OUT"
    if "fast filling" in blob:
        return "FAST FILLING"
    return "AVAILABLE"


def scrape():
    with sync_playwright() as p:
        browser = p.firefox.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:155.0) Gecko/20100101 Firefox/155.0",
            locale="en-US",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1440, "height": 1200},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "Referer": "https://in.bookmyshow.com/"},
        )
        context.add_cookies([{"name": "Rgn", "value": "Code=HYD|text=Hyderabad", "domain": "in.bookmyshow.com", "path": "/"}])
        page = context.new_page()
        response = page.goto(BMS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        for _ in range(8):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(500)

        status = response.status if response else 0
        body = page.locator("body").inner_text()
        print(f"[INFO] BMS HTTP {status}; rendered text {len(body):,} chars")

        if status != 200 or "AMB Cinemas" not in body or not TIME_RE.search(body):
            browser.close()
            raise RuntimeError("BMS did not return a reliable AMB showtime page; state will not be changed.")

        # Extract every compact DOM block that contains a time. Python then
        # reconstructs movies from the rendered text order. This avoids relying
        # on BMS's private API/Next.js JSON.
        raw = page.evaluate("""() => {
          const timeRe = /\\b(?:0?[1-9]|1[0-2])[:.]\\d{2}\\s*(?:AM|PM)\\b/i;
          const out = [];
          for (const el of document.querySelectorAll('body *')) {
            const own = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!own || own.length > 180 || !timeRe.test(own)) continue;
            if ([...el.children].some(c => timeRe.test((c.innerText || '').trim()))) continue;
            let n = el;
            let context = '';
            for (let i=0; i<5 && n; i++, n=n.parentElement) {
              const t = (n.innerText || '').trim();
              if (t.length > 0 && t.length < 1200) context = t;
            }
            out.push({
              text: el.innerText || '',
              context,
              cls: typeof el.className === 'string' ? el.className : '',
              aria: el.getAttribute('aria-label') || '',
              title: el.getAttribute('title') || '',
              disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
            });
          }
          return out;
        }""")

        # The full body text is the most stable source for movie/language/screen
        # grouping. Parse it sequentially.
        lines = [clean(x) for x in body.splitlines() if clean(x)]
        start = 0
        for i, line in enumerate(lines):
            if line == "SUBTITLES LANGUAGE":
                start = i + 1
                break
        lines = lines[start:]

        shows = []
        current_movie = ""
        current_meta = ""
        pending_time = None

        noise = {"AVAILABLE", "FAST FILLING", "lan", "SUBTITLES LANGUAGE", "Select Price Range", "Select Show Timings"}

        for i, line in enumerate(lines):
            if line in noise:
                continue
            tm = norm_time(line)
            if tm:
                pending_time = tm
                # Screen is usually the next rendered line.
                screen = ""
                for nxt in lines[i + 1:i + 3]:
                    if TIME_RE.search(nxt):
                        break
                    if any(w in nxt.upper() for w in SCREEN_WORDS):
                        screen = nxt
                        break

                if current_movie:
                    shows.append({
                        "movie": current_movie,
                        "language_format": current_meta or "Not shown",
                        "time": tm,
                        "screen": screen or "Not shown",
                        "status": "AVAILABLE",
                    })
                continue

            if META_RE.match(line):
                current_meta = line
                continue

            # A movie title normally precedes a language/format line. Use that
            # relationship instead of guessing from arbitrary page labels.
            if i + 1 < len(lines) and META_RE.match(lines[i + 1]):
                current_movie = RATING_RE.sub("", line).strip()
                current_meta = lines[i + 1]

        # Map DOM status evidence back to matching times. We only promote a
        # status when BMS explicitly exposes it; no guessed SOLD OUT states.
        statuses = {}
        for item in raw:
            tm = norm_time(item.get("text", ""))
            if not tm:
                continue
            blob = clean(" ".join(str(item.get(k, "")) for k in ("context", "text", "cls", "aria", "title"))).lower()
            st = "SOLD OUT" if any(x in blob for x in ("sold out", "house full", "unavailable")) else ("FAST FILLING" if "fast filling" in blob else None)
            if st:
                statuses[tm] = st

        for show in shows:
            if show["time"] in statuses:
                show["status"] = statuses[show["time"]]

        # Deduplicate exact records.
        unique = {}
        for s in shows:
            key = "|".join([s["movie"], s["language_format"], s["time"], s["screen"]])
            unique[key] = s

        browser.close()
        result = sorted(unique.values(), key=lambda x: (x["movie"].lower(), x["time"], x["screen"]))
        if not result:
            raise RuntimeError("AMB page loaded but no movie showtimes were parsed; state will not be changed.")
        return result


def load_state():
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data.get("shows", []) if isinstance(data, dict) else []
    except Exception:
        return []


def key(s):
    return "|".join([s.get("movie", ""), s.get("language_format", ""), s.get("time", ""), s.get("screen", "")])


def main():
    try:
        shows = scrape()
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 1

    old = {key(s): s for s in load_state()}
    new = {key(s): s for s in shows}

    added = [new[k] for k in sorted(set(new) - set(old))]
    removed = [old[k] for k in sorted(set(old) - set(new))]
    changed = [(old[k], new[k]) for k in sorted(set(old) & set(new)) if old[k].get("status") != new[k].get("status")]

    print(f"[INFO] Parsed {len(shows)} AMB shows for {TARGET_LABEL}")
    movies = sorted({s["movie"] for s in shows})
    print(f"[INFO] Movies found: {len(movies)}")
    for movie in movies:
        rows = [s for s in shows if s["movie"] == movie]
        print(f"\n[{movie}]")
        for s in rows:
            print(f"  {s['time']} | {s['language_format']} | {s['screen']} | {s['status']}")

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

    STATE_FILE.write_text(json.dumps({
        "venue": VENUE,
        "target_date": TARGET_DATE,
        "shows": shows
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[INFO] State saved to {STATE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
