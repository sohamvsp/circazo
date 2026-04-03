"""
/api/scores  –  Cricbuzz live + upcoming matches scraper.
Deployed as Vercel Python serverless function.
Returns JSON: { status, source, updated_at, matches: [...] }
"""

import re
import json
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

CRICBUZZ_LIVE_URL = "https://www.cricbuzz.com/cricket-match/live-scores"


# ─────────────────────────────────────────────────────
#  HTML scraper helpers
# ─────────────────────────────────────────────────────

def _decode_nextjs(html: str) -> str:
    """Decode Next.js data chunks from Cricbuzz pages."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    out = ""
    for chunk in chunks:
        try:
            out += chunk.encode().decode("unicode_escape")
        except Exception:
            out += chunk
    return out


def _extract_score(raw: str) -> dict | None:
    """Parse a score string like '154/3 (16.2 Ov)' into structured data."""
    m = re.match(
        r"(\d{1,3})(?:/(\d{1,2}))?\s*(?:\(?([\d.]+)\s*(?:Ov|ov|overs?)?\)?)?",
        raw.strip(),
    )
    if not m:
        return None
    return {
        "r": int(m.group(1)),
        "w": int(m.group(2)) if m.group(2) is not None else None,
        "o": m.group(3) if m.group(3) else None,
    }


# ─────────────────────────────────────────────────────
#  LIVE MATCHES  (HTML parsing approach)
# ─────────────────────────────────────────────────────

def scrape_live_matches() -> list[dict]:
    """Scrape all current + upcoming matches from Cricbuzz live scores page."""
    try:
        resp = httpx.get(CRICBUZZ_LIVE_URL, headers=HEADERS, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Cricbuzz fetch failed: %s", exc)
        return []

    html = resp.text
    matches: list[dict] = []
    seen_ids: set[str] = set()

    # ── Strategy 1: extract from href links  ──────────────────────────────
    # Cricbuzz match links: /live-cricket-scores/{id}/{slug}
    links = re.findall(
        r'href="/live-cricket-scores/(\d+)/([^"]+)"',
        html,
    )

    for match_id, slug in links:
        if match_id in seen_ids:
            continue
        seen_ids.add(match_id)

        # Derive team names from slug: "csk-vs-mi-7th-match-ipl-2026"
        slug_clean = slug.replace("-", " ").lower()
        vs_m = re.search(r"^(.+?)\s+vs\s+(.+?)\s+(?:\d|match|game)", slug_clean)
        if not vs_m:
            vs_m = re.search(r"^(.+?)\s+vs\s+(.+)", slug_clean)
        if not vs_m:
            continue

        t1_raw = vs_m.group(1).title().strip()
        t2_raw = vs_m.group(2).title().strip()

        # Determine series/match name from slug tail
        series_m = re.search(r"\d+(?:st|nd|rd|th)-match-(.+)$", slug)
        series = series_m.group(1).replace("-", " ").title() if series_m else "Cricket"

        # Detect format
        fmt = "T20"
        if "test" in slug_clean:
            fmt = "Test"
        elif "odi" in slug_clean:
            fmt = "ODI"

        # Is it IPL?
        is_ipl = "ipl" in slug_clean or "indian-premier-league" in slug_clean

        matches.append(
            {
                "id": match_id,
                "name": f"{t1_raw} vs {t2_raw}",
                "slug": slug,
                "t1": t1_raw,
                "t2": t2_raw,
                "series": series,
                "matchType": fmt,
                "is_ipl": is_ipl,
                "matchStarted": False,  # refined below
                "matchEnded": False,
                "isLive": False,
                "status": "",
                "score": [],
                "venue": "",
                "dateTimeGMT": "",
                "cricbuzz_url": f"https://www.cricbuzz.com/live-cricket-scores/{match_id}/{slug}",
            }
        )

    # ── Strategy 2: enrich with score/status from Next.js chunks ─────────
    nxt = _decode_nextjs(html)

    # Live indicators
    live_ids: set[str] = set()
    live_markers = re.findall(r'"matchId":(\d+)[^}]*?"status":"([^"]*?LIVE[^"]*?)"', nxt, re.IGNORECASE)
    for mid, _ in live_markers:
        live_ids.add(mid)

    # Also mark complete matches
    done_ids: set[str] = set()
    done_markers = re.findall(r'"matchId":(\d+)[^}]*?"status":"([^"]*?(?:won by|result|tie|draw)[^"]*?)"', nxt, re.IGNORECASE)
    for mid, _ in done_markers:
        done_ids.add(mid)

    # Status text
    status_map: dict[str, str] = {}
    for m_id in seen_ids:
        # Look for status near match id mention
        pos = nxt.find(f'"{m_id}"')
        if pos == -1:
            continue
        after = nxt[pos: pos + 600]
        st = re.search(r'"status":"([^"]+)"', after)
        if st:
            status_map[m_id] = st.group(1)

    # Score extraction
    score_map: dict[str, list] = {}
    for m_id in seen_ids:
        scores = []
        pos = nxt.find(f'"{m_id}"')
        if pos == -1:
            continue
        window = nxt[pos: pos + 1200]
        # Find score strings like "154/3 (16.2 Ov)"
        raw_scores = re.findall(
            r'(?:"score"|"liveScore"|"score1"|"score2")\s*:\s*"([^"]+)"',
            window,
        )
        for rs in raw_scores:
            parsed = _extract_score(rs)
            if parsed:
                scores.append(parsed)
        if scores:
            score_map[m_id] = scores

    # Venue
    venue_map: dict[str, str] = {}
    for m_id in seen_ids:
        pos = nxt.find(f'"{m_id}"')
        if pos == -1:
            continue
        window = nxt[pos: pos + 800]
        ven = re.search(r'"(?:venue|ground|stadium)"\s*:\s*"([^"]+)"', window, re.IGNORECASE)
        if ven:
            venue_map[m_id] = ven.group(1)

    # Apply enrichments
    for m in matches:
        mid = m["id"]
        m["isLive"] = mid in live_ids
        m["matchStarted"] = mid in live_ids or mid in done_ids
        m["matchEnded"] = mid in done_ids
        m["status"] = status_map.get(mid, "")
        m["score"] = score_map.get(mid, [])
        m["venue"] = venue_map.get(mid, "")

    # ── Strategy 3: simple fallback with classic HTML selectors ──────────
    if not matches:
        matches = _scrape_fallback(html)

    # Sort: live first, then upcoming, then done
    order = {"live": 0, "upcoming": 1, "done": 2}

    def sort_key(m):
        if m["isLive"]:
            return 0
        if m["matchEnded"]:
            return 2
        return 1

    matches.sort(key=sort_key)
    return matches


def _scrape_fallback(html: str) -> list[dict]:
    """Classic regex fallback for older Cricbuzz HTML structure."""
    matches = []
    # Match blocks containing two team names
    blocks = re.findall(
        r'href="/live-cricket-scores/(\d+)/([^"]+)"[^<]*<[^>]+>[^<]*<[^>]+>([^<]+)</[^>]+>',
        html,
    )
    seen = set()
    for mid, slug, title in blocks:
        if mid in seen:
            continue
        seen.add(mid)
        vs = re.search(r"(.+?)\s+vs\s+(.+)", title)
        if not vs:
            continue
        matches.append(
            {
                "id": mid,
                "name": title.strip(),
                "t1": vs.group(1).strip(),
                "t2": vs.group(2).strip(),
                "series": slug.replace("-", " ").title(),
                "matchType": "T20",
                "is_ipl": "ipl" in slug.lower(),
                "matchStarted": False,
                "matchEnded": False,
                "isLive": False,
                "status": "",
                "score": [],
                "venue": "",
                "dateTimeGMT": "",
                "cricbuzz_url": f"https://www.cricbuzz.com/live-cricket-scores/{mid}/{slug}",
            }
        )
    return matches


# ─────────────────────────────────────────────────────
#  Vercel HTTP handler
# ─────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default access log

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        only = qs.get("filter", [None])[0]  # ?filter=live | upcoming | all

        matches = scrape_live_matches()

        if only == "live":
            matches = [m for m in matches if m["isLive"]]
        elif only == "upcoming":
            matches = [m for m in matches if not m["matchStarted"]]
        elif only == "ipl":
            matches = [m for m in matches if m.get("is_ipl")]

        body = json.dumps(
            {
                "status": "success" if matches else "empty",
                "source": "cricbuzz",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(matches),
                "matches": matches,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=25, stale-while-revalidate=30")
        self.end_headers()
        self.wfile.write(body)
