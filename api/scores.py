"""
/api/scores  – Live + upcoming match list scraped from source.
Fixes: IPL slug abbreviation → full team name mapping, proper dates, clean JSON.
"""
import re, json, logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Mobile/15E148 Safari/604.1")
HEADERS = {"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9",
           "Cache-Control": "no-cache", "Referer": "https://www.google.com/"}

# ── COMPLETE IPL SLUG → FULL NAME MAP ──────────────────────────────────────
# Cricbuzz uses abbreviations in URL slugs for IPL teams
SLUG_TO_TEAM = {
    # Abbreviations used in Cricbuzz slugs
    "csk":   "Chennai Super Kings",
    "mi":    "Mumbai Indians",
    "rcb":   "Royal Challengers Bengaluru",
    "kkr":   "Kolkata Knight Riders",
    "srh":   "Sunrisers Hyderabad",
    "dc":    "Delhi Capitals",
    "rr":    "Rajasthan Royals",
    "pbks":  "Punjab Kings",
    "lsg":   "Lucknow Super Giants",
    "gt":    "Gujarat Titans",
    # Full slug forms
    "chennai-super-kings":          "Chennai Super Kings",
    "mumbai-indians":               "Mumbai Indians",
    "royal-challengers-bengaluru":  "Royal Challengers Bengaluru",
    "royal-challengers-bangalore":  "Royal Challengers Bengaluru",
    "kolkata-knight-riders":        "Kolkata Knight Riders",
    "sunrisers-hyderabad":          "Sunrisers Hyderabad",
    "delhi-capitals":               "Delhi Capitals",
    "rajasthan-royals":             "Rajasthan Royals",
    "punjab-kings":                 "Punjab Kings",
    "lucknow-super-giants":         "Lucknow Super Giants",
    "gujarat-titans":               "Gujarat Titans",
    # International teams
    "india":         "India",
    "australia":     "Australia",
    "england":       "England",
    "pakistan":      "Pakistan",
    "south-africa":  "South Africa",
    "new-zealand":   "New Zealand",
    "west-indies":   "West Indies",
    "sri-lanka":     "Sri Lanka",
    "bangladesh":    "Bangladesh",
    "afghanistan":   "Afghanistan",
    "zimbabwe":      "Zimbabwe",
    "ireland":       "Ireland",
    "scotland":      "Scotland",
    "namibia":       "Namibia",
    "oman":          "Oman",
    # PSL teams
    "quetta-gladiators":   "Quetta Gladiators",
    "karachi-kings":       "Karachi Kings",
    "lahore-qalandars":    "Lahore Qalandars",
    "multan-sultans":      "Multan Sultans",
    "peshawar-zalmi":      "Peshawar Zalmi",
    "islamabad-united":    "Islamabad United",
}

def resolve_team(slug_part: str) -> str:
    """Resolve a slug team token (like 'dc', 'mi', 'csk') to full name."""
    key = slug_part.strip().lower()
    if key in SLUG_TO_TEAM:
        return SLUG_TO_TEAM[key]
    # Try multi-word slug like 'delhi capitals' (after replace - with space)
    key2 = key.replace(" ", "-")
    if key2 in SLUG_TO_TEAM:
        return SLUG_TO_TEAM[key2]
    # Fall back to title-cased version
    return slug_part.strip().title()


def extract_vs_from_slug(slug: str) -> tuple[str, str] | tuple[None, None]:
    """
    Extract team names from a Cricbuzz slug.
    Handles both:
      csk-vs-mi-7th-match-ipl-2026         (abbreviations)
      delhi-capitals-vs-mumbai-indians-8th  (full slugs)
    """
    # Remove leading /live-cricket-scores/ID/ if present
    clean = re.sub(r'^/live-cricket-scores/\d+/', '', slug)

    # Split on '-vs-' first
    vs_pos = clean.find('-vs-')
    if vs_pos == -1:
        return None, None

    t1_slug = clean[:vs_pos]
    remainder = clean[vs_pos + 4:]

    # t2 ends at ordinal digit or at 'match' or end of meaningful part
    # Remove match number suffix like '-7th-match-...'
    t2_end = re.search(r'-\d+(?:st|nd|rd|th)-match|-match-\d+', remainder)
    t2_slug = remainder[:t2_end.start()] if t2_end else remainder.split('-match-')[0]

    t1 = resolve_team(t1_slug)
    t2 = resolve_team(t2_slug)
    return t1, t2


def detect_format(slug_lower: str) -> str:
    if "test" in slug_lower:    return "Test"
    if "-odi-" in slug_lower or "one-day" in slug_lower: return "ODI"
    if "-t10-" in slug_lower:   return "T10"
    return "T20"


def is_ipl(slug_lower: str, t1: str = "", t2: str = "") -> bool:
    IPL_TEAMS = {"Chennai Super Kings","Mumbai Indians","Royal Challengers Bengaluru",
                 "Royal Challengers Bangalore","Kolkata Knight Riders","Sunrisers Hyderabad",
                 "Delhi Capitals","Rajasthan Royals","Punjab Kings","Lucknow Super Giants","Gujarat Titans"}
    if "ipl" in slug_lower or "indian-premier-league" in slug_lower:
        return True
    return t1 in IPL_TEAMS or t2 in IPL_TEAMS


def parse_score(raw: str) -> dict | None:
    m = re.match(r"(\d{1,3})(?:/(\d{1,2}))?\s*(?:\(?([\d.]+)\s*(?:Ov|ov|overs?)?\)?)?", raw.strip())
    if not m: return None
    return {"r": int(m.group(1)),
            "w": int(m.group(2)) if m.group(2) is not None else None,
            "o": m.group(3) or None}


def decode_chunks(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    out = ""
    for c in chunks:
        try:    out += c.encode().decode("unicode_escape")
        except: out += c
    return out


def scrape_matches() -> list[dict]:
    try:
        resp = httpx.get("https://www.cricbuzz.com/cricket-match/live-scores",
                         headers=HEADERS, timeout=18, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Fetch failed: %s", e)
        return []

    html = resp.text
    nxt  = decode_chunks(html)
    matches: list[dict] = []
    seen: set[str] = set()

    # Extract from href links
    links = re.findall(r'href="(/live-cricket-scores/(\d+)/([^"]+))"', html)
    for full_href, match_id, slug in links:
        if match_id in seen:
            continue
        seen.add(match_id)

        t1, t2 = extract_vs_from_slug(slug)
        if not t1 or not t2:
            continue

        slug_lower = slug.lower()
        fmt      = detect_format(slug_lower)
        ipl_flag = is_ipl(slug_lower, t1, t2)

        # Series name from slug tail
        series_m = re.search(r'\d+(?:st|nd|rd|th)-match-(.+)$', slug)
        series   = series_m.group(1).replace("-"," ").title() if series_m else slug.split("-match-")[-1].replace("-"," ").title()

        matches.append({
            "id":          match_id,
            "name":        f"{t1} vs {t2}, {series}",
            "slug":        slug,
            "t1":          t1,
            "t2":          t2,
            "series":      series,
            "matchType":   fmt,
            "is_ipl":      ipl_flag,
            "matchStarted":False,
            "matchEnded":  False,
            "isLive":      False,
            "status":      "",
            "score":       [],
            "venue":       "",
            "dateTimeGMT": "",
            "url":         f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/{slug}",
        })

    # Enrich from Next.js data chunks
    _enrich(nxt, matches)

    # Sort: live first, upcoming, done
    matches.sort(key=lambda m: (0 if m["isLive"] else 2 if m["matchEnded"] else 1))
    return matches


def _enrich(nxt: str, matches: list[dict]):
    """Fill in isLive/matchEnded/status/score/venue/date from Next.js chunk data."""
    for m in matches:
        mid = m["id"]
        pos = nxt.find(f'"{mid}"')
        if pos == -1:
            # Try without quotes
            pos = nxt.find(mid)
        if pos == -1:
            continue
        window = nxt[pos: pos + 2000]

        # Live/complete status
        if re.search(r'"(?:live|status)"\s*:\s*"[^"]*LIVE[^"]*"', window, re.I):
            m["isLive"] = True; m["matchStarted"] = True
        if re.search(r'"status"\s*:\s*"[^"]*(?:won by|tied|result|completed)[^"]*"', window, re.I):
            m["matchEnded"] = True; m["matchStarted"] = True

        # Status text
        st = re.search(r'"status"\s*:\s*"([^"]+)"', window)
        if st:
            m["status"] = st.group(1)

        # Scores
        raw_sc = re.findall(r'"(?:score[12]?|liveScore|runs|score)"\s*:\s*"([\d/]+(?:\s*\([\d.]+\s*(?:Ov)?\))?)"', window)
        scores = [parse_score(s) for s in raw_sc if parse_score(s)]
        if scores:
            m["score"] = scores

        # Venue
        ven = re.search(r'"(?:venue|ground|stadium)"\s*:\s*"([^"]+)"', window, re.I)
        if ven:
            m["venue"] = ven.group(1)

        # Date/time
        dt = re.search(r'"(?:startDate|matchStartTimestamp|dateTime)"\s*:\s*"?(\d{10,13})"?', window)
        if dt:
            ts_raw = int(dt.group(1))
            if ts_raw > 9999999999:  # milliseconds
                ts_raw //= 1000
            try:
                m["dateTimeGMT"] = datetime.utcfromtimestamp(ts_raw).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            except Exception:
                pass

        # Also try ISO date string
        if not m["dateTimeGMT"]:
            iso = re.search(r'"(?:startDate|matchDate)"\s*:\s*"(\d{4}-\d{2}-\d{2}T[^"]+)"', window)
            if iso:
                m["dateTimeGMT"] = iso.group(1)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        qs     = parse_qs(urlparse(self.path).query)
        filt   = qs.get("filter", [None])[0]
        matches = scrape_matches()

        if filt == "live":    matches = [m for m in matches if m["isLive"]]
        elif filt == "upcoming": matches = [m for m in matches if not m["matchStarted"]]
        elif filt == "ipl":   matches = [m for m in matches if m["is_ipl"]]

        body = json.dumps({
            "status":     "success" if matches else "empty",
            "source":     "live",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count":      len(matches),
            "matches":    matches,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=25, stale-while-revalidate=30")
        self.end_headers()
        self.wfile.write(body)
