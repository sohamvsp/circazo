"""
/api/scores — Cricbuzz live-scores page scraper.
Extracts scores directly from HTML blocks around each match link.
Returns IST timestamps.
"""
import re, json, logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

# ── TEAM SLUG → FULL NAME ────────────────────────────────────────────────────
SLUG_MAP = {
    "csk":"Chennai Super Kings","mi":"Mumbai Indians",
    "rcb":"Royal Challengers Bengaluru","kkr":"Kolkata Knight Riders",
    "srh":"Sunrisers Hyderabad","dc":"Delhi Capitals",
    "rr":"Rajasthan Royals","pbks":"Punjab Kings",
    "lsg":"Lucknow Super Giants","gt":"Gujarat Titans",
    "chennai-super-kings":"Chennai Super Kings",
    "mumbai-indians":"Mumbai Indians",
    "royal-challengers-bengaluru":"Royal Challengers Bengaluru",
    "royal-challengers-bangalore":"Royal Challengers Bengaluru",
    "kolkata-knight-riders":"Kolkata Knight Riders",
    "sunrisers-hyderabad":"Sunrisers Hyderabad",
    "delhi-capitals":"Delhi Capitals",
    "rajasthan-royals":"Rajasthan Royals",
    "punjab-kings":"Punjab Kings",
    "lucknow-super-giants":"Lucknow Super Giants",
    "gujarat-titans":"Gujarat Titans",
    # PSL
    "karachi-kings":"Karachi Kings","lahore-qalandars":"Lahore Qalandars",
    "islamabad-united":"Islamabad United","multan-sultans":"Multan Sultans",
    "quetta-gladiators":"Quetta Gladiators","peshawar-zalmi":"Peshawar Zalmi",
    "rawalpindiz":"Rawalpindi Zalmi","hyderabad-kingsmen":"Hyderabad Kingsmen",
    # International
    "india":"India","australia":"Australia","england":"England","pakistan":"Pakistan",
    "south-africa":"South Africa","new-zealand":"New Zealand","west-indies":"West Indies",
    "sri-lanka":"Sri Lanka","bangladesh":"Bangladesh","afghanistan":"Afghanistan",
    "zimbabwe":"Zimbabwe","ireland":"Ireland","scotland":"Scotland","namibia":"Namibia",
    "oman":"Oman","usa":"USA","uae":"UAE","netherlands":"Netherlands",
    "kenya":"Kenya","canada":"Canada","nepal":"Nepal",
}
IPL_TEAMS = {
    "Chennai Super Kings","Mumbai Indians","Royal Challengers Bengaluru",
    "Royal Challengers Bangalore","Kolkata Knight Riders","Sunrisers Hyderabad",
    "Delhi Capitals","Rajasthan Royals","Punjab Kings","Lucknow Super Giants","Gujarat Titans",
}

def resolve(slug: str) -> str:
    k = slug.strip().lower()
    if k in SLUG_MAP: return SLUG_MAP[k]
    # strip common suffixes like "-2026", "-ipl"
    k2 = re.sub(r'-\d{4}$','', k)
    if k2 in SLUG_MAP: return SLUG_MAP[k2]
    return slug.strip().title()

def vs_from_slug(slug: str):
    clean = re.sub(r'-\d+(?:st|nd|rd|th)-(?:t20i?|odi|test)-?match.*','', slug)
    clean = re.sub(r'-\d+(?:st|nd|rd|th)-match.*','', clean)
    clean = re.sub(r'-match-\d+.*','', clean)
    pos = clean.find('-vs-')
    if pos < 0: return None, None
    return resolve(clean[:pos]), resolve(clean[pos+4:])

def parse_score(raw: str):
    m = re.match(r'(\d{1,3})(?:/(\d{1,2}))?\s*(?:\(?([\d.]+)\s*(?:Ov|ov)?\)?)?', raw.strip())
    if not m: return None
    return {"r": int(m.group(1)), "w": int(m.group(2)) if m.group(2) else None,
            "o": m.group(3) or None}

def decode_chunks(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    out = ""
    for c in chunks:
        try:    out += c.encode().decode("unicode_escape")
        except: out += c
    return out

def ts_to_ist(ts_val: str) -> str:
    """Convert a Unix timestamp (ms or s) to IST ISO string."""
    try:
        ts = int(ts_val)
        if ts > 9_999_999_999: ts //= 1000
        dt = datetime.utcfromtimestamp(ts).replace(tzinfo=timezone.utc).astimezone(IST)
        return dt.isoformat()
    except Exception:
        return ""

def scrape_matches() -> list:
    try:
        resp = httpx.get(
            "https://www.cricbuzz.com/cricket-match/live-scores",
            headers=HEADERS, timeout=20, follow_redirects=True
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Fetch failed: %s", e)
        return []

    html = resp.text
    nxt  = decode_chunks(html)

    # ── STEP 1: Find match IDs in each section (live / upcoming / recent) ───
    # Cricbuzz SSR wraps sections in divs with these class patterns
    section_boundaries = {
        "live":     [r'cb-text-live', r'Live Matches', r'"LIVE"', r'>LIVE<'],
        "upcoming": [r'cb-text-preview', r'Upcoming Matches', r'"UPCOMING"'],
        "recent":   [r'cb-text-complete', r'Recent Matches', r'"COMPLETE"', r'cb-text-stumps'],
    }

    def ids_in_section(html_part: str) -> set:
        return set(re.findall(r'/live-cricket-scores/(\d+)/', html_part))

    # Detect section positions in raw HTML
    def first_pos(patterns: list) -> int:
        for p in patterns:
            m = re.search(p, html)
            if m: return m.start()
        return -1

    pos_live = first_pos(section_boundaries["live"])
    pos_up   = first_pos(section_boundaries["upcoming"])
    pos_rec  = first_pos(section_boundaries["recent"])

    # Build section HTML slices
    def slice_html(start, end=-1):
        if start < 0: return ""
        return html[start:end] if end > 0 else html[start:]

    live_html = slice_html(pos_live, pos_up if pos_up > 0 else pos_rec)
    up_html   = slice_html(pos_up,   pos_rec)
    rec_html  = slice_html(pos_rec)

    # Also detect from Next.js chunks
    nxt_live_ids = set()
    for m in re.finditer(r'"(?:matchStatus|status)"\s*:\s*"([^"]*LIVE[^"]*)"', nxt, re.I):
        # look backwards for match ID
        before = nxt[max(0, m.start()-200):m.start()]
        ids = re.findall(r'\b(\d{6,8})\b', before)
        nxt_live_ids.update(ids)

    live_ids     = ids_in_section(live_html) | nxt_live_ids
    upcoming_ids = ids_in_section(up_html)   - live_ids
    recent_ids   = ids_in_section(rec_html)  - live_ids

    # ── STEP 2: Extract all match links ──────────────────────────────────────
    links = re.findall(r'/live-cricket-scores/(\d+)/([^"?\s]+)', html)
    seen: set = set()
    matches: list = []

    for match_id, slug in links:
        if match_id in seen: continue
        seen.add(match_id)

        t1, t2 = vs_from_slug(slug)
        if not t1 or not t2: continue

        slug_lower = slug.lower()
        fmt = ("Test" if "test" in slug_lower else
               "ODI"  if ("-odi-" in slug_lower or "one-day" in slug_lower) else "T20")
        ipl_flag = ("ipl" in slug_lower or "indian-premier-league" in slug_lower
                    or t1 in IPL_TEAMS or t2 in IPL_TEAMS)

        is_live    = match_id in live_ids
        is_ended   = match_id in recent_ids and not is_live
        is_upcoming = not is_live and not is_ended

        series_m = re.search(r'\d+(?:st|nd|rd|th)-match-(.+)$', slug)
        series   = (series_m.group(1).replace("-"," ").title() if series_m
                    else slug.replace("-"," ").title()[:40])

        matches.append({
            "id":          match_id,
            "name":        f"{t1} vs {t2}, {series}",
            "t1": t1, "t2": t2,
            "series":      series,
            "matchType":   fmt,
            "is_ipl":      ipl_flag,
            "matchStarted": is_live or is_ended,
            "matchEnded":  is_ended,
            "isLive":      is_live,
            "status":      "",
            "score":       [],
            "venue":       "",
            "dateTimeIST": "",
            "dateTimeGMT": "",
            "url":         f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/{slug}",
        })

    # ── STEP 3: Enrich from HTML blocks around each match link ───────────────
    _enrich_from_html(html, nxt, matches)

    # Sort: live → upcoming → ended
    matches.sort(key=lambda m: (0 if m["isLive"] else 2 if m["matchEnded"] else 1))
    return matches


def _enrich_from_html(html: str, nxt: str, matches: list):
    """Extract scores, status, venue, date from HTML blocks and NXT chunks."""
    for m in matches:
        mid = m["id"]

        # ── Find a ~3000-char window around this match_id in HTML ──────────
        pos = html.find(f'/{mid}/')
        if pos < 0: pos = html.find(mid)
        if pos < 0: continue
        html_win = html[max(0, pos-100): pos+3000]

        # ── Score extraction: multiple patterns ────────────────────────────
        # Pattern 1: "NNN/N (NN.N Ov)" or "NNN/N"
        score_raws = re.findall(
            r'(\d{1,3}/\d{1,2}\s*(?:\(\s*[\d.]+\s*(?:Ov|ov)?\s*\))?)',
            html_win
        )
        scores = []
        seen_scores = set()
        for sr in score_raws:
            parsed = parse_score(sr)
            if parsed and parsed["r"] is not None:
                key = f"{parsed['r']}/{parsed['w']}"
                if key not in seen_scores:
                    seen_scores.add(key)
                    scores.append(parsed)
        if scores:
            m["score"] = scores[:2]  # max 2 innings
            # If has score but not marked live/ended, upgrade to live
            if not m["matchStarted"] and any(s["r"] and s["r"] > 0 for s in scores):
                m["isLive"] = True
                m["matchStarted"] = True

        # ── Status text ───────────────────────────────────────────────────
        # Look for status classes
        st_m = re.search(
            r'class="cb-text-(?:live|complete|stumps|preview)[^"]*"[^>]*>([^<]{5,200})<',
            html_win, re.I
        )
        if st_m:
            sv = st_m.group(1).strip()
            m["status"] = sv
            if re.search(r'\bLIVE\b', sv, re.I):
                m["isLive"] = True; m["matchStarted"] = True; m["matchEnded"] = False
            elif re.search(r'won by|tied|no result|abandoned|result', sv, re.I):
                m["matchEnded"] = True; m["matchStarted"] = True
                m["isLive"] = False

        # ── Enrich from Next.js chunk near this ID ────────────────────────
        nxt_pos = nxt.find(f'"{mid}"')
        if nxt_pos < 0: nxt_pos = nxt.find(mid)
        if nxt_pos >= 0:
            nxt_win = nxt[nxt_pos: nxt_pos+2000]

            if not m["status"]:
                st = re.search(r'"status"\s*:\s*"([^"]{5,200})"', nxt_win)
                if st:
                    sv = st.group(1).strip()
                    m["status"] = sv
                    if re.search(r'\bLIVE\b', sv, re.I):
                        m["isLive"] = True; m["matchStarted"] = True; m["matchEnded"] = False
                    elif re.search(r'won by|tied|no result|abandoned', sv, re.I):
                        m["matchEnded"] = True; m["matchStarted"] = True; m["isLive"] = False

            # Venue
            if not m["venue"]:
                v = re.search(r'"(?:venue|ground|stadium)"\s*:\s*"([^"]{5,80})"', nxt_win, re.I)
                if v: m["venue"] = v.group(1)

            # Timestamp
            if not m["dateTimeGMT"]:
                dt = re.search(
                    r'"(?:startDate|matchStartTimestamp|dateTimeGMT|startTime|matchTime)"\s*:\s*"?(\d{10,13})"?',
                    nxt_win
                )
                if dt:
                    m["dateTimeGMT"]  = ts_to_ist(dt.group(1))  # store IST
                    m["dateTimeIST"]  = m["dateTimeGMT"]

            if not m["dateTimeGMT"]:
                iso = re.search(
                    r'"(?:startDate|dateTimeGMT)"\s*:\s*"(\d{4}-\d{2}-\d{2}T[^"]{5,30})"',
                    nxt_win
                )
                if iso:
                    try:
                        dt_utc = datetime.fromisoformat(iso.group(1).replace("Z",""))
                        dt_ist = dt_utc.replace(tzinfo=timezone.utc).astimezone(IST)
                        m["dateTimeGMT"] = dt_ist.isoformat()
                        m["dateTimeIST"] = m["dateTimeGMT"]
                    except Exception:
                        m["dateTimeGMT"] = iso.group(1)


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
            "updated_at": datetime.now(IST).isoformat(),
            "count":      len(matches),
            "matches":    matches,
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=25")
        self.end_headers()
        self.wfile.write(body)
