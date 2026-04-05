"""
/api/scores  — Cricbuzz live + upcoming match scraper.
Uses HTML section-based detection for reliable live/upcoming classification.
"""
import re, json, logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Mobile/15E148 Safari/604.1"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

# ── FULL TEAM NAME MAPS ──────────────────────────────────────────────────────
SLUG_MAP = {
    # IPL abbreviations used in Cricbuzz URL slugs
    "csk": "Chennai Super Kings",
    "mi": "Mumbai Indians",
    "rcb": "Royal Challengers Bengaluru",
    "kkr": "Kolkata Knight Riders",
    "srh": "Sunrisers Hyderabad",
    "dc": "Delhi Capitals",
    "rr": "Rajasthan Royals",
    "pbks": "Punjab Kings",
    "lsg": "Lucknow Super Giants",
    "gt": "Gujarat Titans",
    # Full slug forms
    "chennai-super-kings": "Chennai Super Kings",
    "mumbai-indians": "Mumbai Indians",
    "royal-challengers-bengaluru": "Royal Challengers Bengaluru",
    "royal-challengers-bangalore": "Royal Challengers Bengaluru",
    "kolkata-knight-riders": "Kolkata Knight Riders",
    "sunrisers-hyderabad": "Sunrisers Hyderabad",
    "delhi-capitals": "Delhi Capitals",
    "rajasthan-royals": "Rajasthan Royals",
    "punjab-kings": "Punjab Kings",
    "lucknow-super-giants": "Lucknow Super Giants",
    "gujarat-titans": "Gujarat Titans",
    # PSL
    "karachi-kings": "Karachi Kings",
    "lahore-qalandars": "Lahore Qalandars",
    "islamabad-united": "Islamabad United",
    "multan-sultans": "Multan Sultans",
    "quetta-gladiators": "Quetta Gladiators",
    "peshawar-zalmi": "Peshawar Zalmi",
    "rawalpindiz": "Rawalpindi Zalmi",
    "hyderabad-kingsmen": "Hyderabad Kingsmen",
    # International
    "india": "India",
    "australia": "Australia",
    "england": "England",
    "pakistan": "Pakistan",
    "south-africa": "South Africa",
    "new-zealand": "New Zealand",
    "west-indies": "West Indies",
    "sri-lanka": "Sri Lanka",
    "bangladesh": "Bangladesh",
    "afghanistan": "Afghanistan",
    "zimbabwe": "Zimbabwe",
    "ireland": "Ireland",
    "scotland": "Scotland",
    "namibia": "Namibia",
    "oman": "Oman",
    "usa": "USA",
    "uae": "UAE",
    "netherlands": "Netherlands",
    "kenya": "Kenya",
    "canada": "Canada",
    "nepal": "Nepal",
}

IPL_TEAMS = {
    "Chennai Super Kings", "Mumbai Indians", "Royal Challengers Bengaluru",
    "Royal Challengers Bangalore", "Kolkata Knight Riders", "Sunrisers Hyderabad",
    "Delhi Capitals", "Rajasthan Royals", "Punjab Kings",
    "Lucknow Super Giants", "Gujarat Titans",
}

def resolve(slug: str) -> str:
    k = slug.strip().lower()
    if k in SLUG_MAP:
        return SLUG_MAP[k]
    # Try with dashes replaced by spaces
    for key, val in SLUG_MAP.items():
        if key.replace("-", "") == k.replace("-", ""):
            return val
    return slug.strip().title()

def vs_from_slug(slug: str):
    """Extract t1, t2 from a slug like 'csk-vs-mi-7th-match-ipl-2026'."""
    # Remove match number suffix
    clean = re.sub(r'-\d+(?:st|nd|rd|th)-match.*', '', slug)
    clean = re.sub(r'-match-\d+.*', '', clean)
    pos = clean.find('-vs-')
    if pos < 0:
        return None, None
    t1_slug = clean[:pos]
    t2_slug = clean[pos+4:]
    return resolve(t1_slug), resolve(t2_slug)

def fmt_from_slug(slug: str) -> str:
    s = slug.lower()
    if "test" in s:    return "Test"
    if "-odi-" in s or "one-day" in s: return "ODI"
    if "-t10-" in s:   return "T10"
    return "T20"

def is_ipl(slug: str, t1: str, t2: str) -> bool:
    if "ipl" in slug.lower() or "indian-premier-league" in slug.lower():
        return True
    return t1 in IPL_TEAMS or t2 in IPL_TEAMS

def parse_score(raw: str):
    m = re.match(r"(\d{1,3})(?:/(\d{1,2}))?\s*(?:\(?([\d.]+)\s*(?:Ov|ov)?\)?)?", raw.strip())
    if not m: return None
    return {"r": int(m.group(1)),
            "w": int(m.group(2)) if m.group(2) else None,
            "o": m.group(3) or None}

def decode_chunks(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    out = ""
    for c in chunks:
        try:    out += c.encode().decode("unicode_escape")
        except: out += c
    return out


def scrape_matches() -> list:
    try:
        resp = httpx.get("https://www.cricbuzz.com/cricket-match/live-scores",
                         headers=HEADERS, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.error("Fetch failed: %s", e)
        return []

    html = resp.text
    nxt  = decode_chunks(html)

    # ── SECTION-BASED LIVE DETECTION ────────────────────────────────────────
    # Split HTML into: live section, upcoming section, recent section
    # Cricbuzz marks sections with these text markers
    markers = {
        "live":     ["Live Matches", "Live Cricket Scores", "cb-text-live"],
        "upcoming": ["Upcoming Matches", "Schedule", "Fixtures"],
        "recent":   ["Recent Matches", "Recent Results", "Completed"],
    }

    # Find section boundaries in raw HTML
    def find_first(texts, start=0):
        pos = len(html) + 1
        for t in texts:
            p = html.find(t, start)
            if 0 < p < pos:
                pos = p
        return pos if pos <= len(html) else -1

    live_start     = find_first(markers["live"])
    upcoming_start = find_first(markers["upcoming"])
    recent_start   = find_first(markers["recent"])

    # Build sections safely
    def section(start, end):
        if start < 0: return ""
        if end < 0: return html[start:]
        return html[start:end]

    # Order: live → upcoming → recent
    # If live not found but upcoming found, assume everything before upcoming is live
    if live_start < 0 and upcoming_start > 0:
        live_section     = html[:upcoming_start]
        upcoming_section = section(upcoming_start, recent_start)
    else:
        live_section     = section(live_start, upcoming_start if upcoming_start > 0 else recent_start)
        upcoming_section = section(upcoming_start, recent_start)

    recent_section = section(recent_start, -1)

    # Extract match IDs from each section
    def ids_from(sec): 
        return set(re.findall(r'/live-cricket-scores/(\d+)/', sec))

    live_ids     = ids_from(live_section)
    upcoming_ids = ids_from(upcoming_section)
    recent_ids   = ids_from(recent_section)

    # Also detect live from page-level "LIVE" badge near match link
    # (<span class="cb-text-live") within 800 chars of the link
    nxt_live_ids = set()
    for m in re.finditer(r'/live-cricket-scores/(\d+)/', html):
        mid = m.group(1)
        window = html[max(0, m.start()-100): m.end()+800]
        if re.search(r'(?:cb-text-live|"LIVE"|>LIVE<)', window, re.I):
            nxt_live_ids.add(mid)

    live_ids |= nxt_live_ids

    # Also check Next.js chunks for live status
    for m in re.finditer(r'"matchId"\s*:\s*"?(\d+)"?', nxt):
        mid = m.group(1)
        window = nxt[m.start(): m.start()+500]
        if re.search(r'"status"\s*:\s*"[^"]*(?:LIVE|live|Live)[^"]*"', window):
            live_ids.add(mid)
            upcoming_ids.discard(mid)

    # ── EXTRACT ALL MATCH LINKS ──────────────────────────────────────────────
    links = re.findall(r'/live-cricket-scores/(\d+)/([^"?\s]+)', html)
    seen: set = set()
    matches: list = []

    for match_id, slug in links:
        if match_id in seen:
            continue
        seen.add(match_id)

        t1, t2 = vs_from_slug(slug)
        if not t1 or not t2:
            continue

        slug_lower  = slug.lower()
        fmt         = fmt_from_slug(slug_lower)
        ipl_flag    = is_ipl(slug_lower, t1, t2)
        is_live     = match_id in live_ids
        is_ended    = match_id in recent_ids and match_id not in live_ids
        is_upcoming = not is_live and not is_ended

        # Series name from slug tail
        series_m = re.search(r'\d+(?:st|nd|rd|th)-match-(.+)$', slug)
        series   = (series_m.group(1).replace("-", " ").title() if series_m
                    else slug.split("-match-")[-1].replace("-", " ").title())

        entry = {
            "id":          match_id,
            "name":        f"{t1} vs {t2}, {series}",
            "slug":        slug,
            "t1":          t1,
            "t2":          t2,
            "series":      series,
            "matchType":   fmt,
            "is_ipl":      ipl_flag,
            "matchStarted": is_live or is_ended,
            "matchEnded":  is_ended,
            "isLive":      is_live,
            "status":      "",
            "score":       [],
            "venue":       "",
            "dateTimeGMT": "",
            "url":         f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/{slug}",
        }
        matches.append(entry)

    # ── ENRICH FROM NEXT.JS CHUNKS ───────────────────────────────────────────
    _enrich(nxt, matches)

    # Sort: live → upcoming → ended
    matches.sort(key=lambda m: (0 if m["isLive"] else 2 if m["matchEnded"] else 1))
    return matches


def _enrich(nxt: str, matches: list):
    for m in matches:
        mid = m["id"]
        # Find position in nxt
        pos = nxt.find(f'"{mid}"')
        if pos < 0: pos = nxt.find(mid)
        if pos < 0: continue
        window = nxt[pos: pos+2000]

        # Status text
        st = re.search(r'"status"\s*:\s*"([^"]{3,200})"', window)
        if st:
            sv = st.group(1).strip()
            if sv and sv not in ("", "null"):
                m["status"] = sv
                # Override live detection from status text
                if re.search(r'\bLIVE\b|\blive\b|\bLive\b|in progress', sv):
                    m["isLive"] = True; m["matchStarted"] = True; m["matchEnded"] = False
                elif re.search(r'won by|tied|no result|abandoned', sv, re.I):
                    m["matchEnded"] = True; m["matchStarted"] = True
                    if m["isLive"]: m["isLive"] = False

        # Scores
        score_raws = re.findall(
            r'"(?:score[12]?|liveScore|inningScore)"\s*:\s*"([\d]+/[\d]+(?:\s*\([\d.]+\s*Ov?\))?)"',
            window)
        scores = [parse_score(s) for s in score_raws if parse_score(s)]
        if scores:
            m["score"] = scores
            # If has score but marked upcoming, upgrade to started
            if not m["matchStarted"]:
                m["matchStarted"] = True
                if not m["matchEnded"]:
                    m["isLive"] = True

        # Venue
        ven = re.search(r'"(?:venue|ground|stadium)"\s*:\s*"([^"]{5,100})"', window, re.I)
        if ven: m["venue"] = ven.group(1)

        # Timestamp → ISO date
        dt = re.search(r'"(?:startDate|matchStartTimestamp|dateTimeGMT|startTime)"\s*:\s*"?(\d{10,13})"?', window)
        if dt:
            ts = int(dt.group(1))
            if ts > 9_999_999_999: ts //= 1000
            try:
                m["dateTimeGMT"] = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
            except Exception:
                pass

        if not m["dateTimeGMT"]:
            iso = re.search(r'"(?:startDate|matchDate|dateTimeGMT)"\s*:\s*"(\d{4}-\d{2}-\d{2}T[^"]{5,30})"', window)
            if iso: m["dateTimeGMT"] = iso.group(1)


class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        qs    = parse_qs(urlparse(self.path).query)
        filt  = qs.get("filter", [None])[0]
        matches = scrape_matches()

        if filt == "live":     matches = [m for m in matches if m["isLive"]]
        elif filt == "upcoming": matches = [m for m in matches if not m["matchStarted"]]
        elif filt == "ipl":    matches = [m for m in matches if m["is_ipl"]]

        body = json.dumps({
            "status":     "success" if matches else "empty",
            "source":     "live",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count":      len(matches),
            "matches":    matches,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=25")
        self.end_headers()
        self.wfile.write(body)
