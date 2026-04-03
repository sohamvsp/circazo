"""
/api/scorecard?id={cricbuzz_match_id}
Scrapes detailed scorecard + ball-by-ball commentary from Cricbuzz.
Vercel Python serverless function.
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
    "Referer": "https://www.cricbuzz.com/",
}


# ─────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────

def _decode_nextjs(html: str) -> str:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    out = ""
    for chunk in chunks:
        try:
            out += chunk.encode().decode("unicode_escape")
        except Exception:
            out += chunk
    return out


def _get_page(url: str) -> tuple[str, str]:
    """Fetch page, return (raw_html, decoded_nextjs_data)."""
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=18, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text
        return html, _decode_nextjs(html)
    except Exception as exc:
        logger.error("Fetch %s failed: %s", url, exc)
        return "", ""


# ─────────────────────────────────────────────────────
#  SCORECARD  (batting + bowling)
# ─────────────────────────────────────────────────────

def scrape_scorecard(cricbuzz_id: str) -> dict:
    url = f"https://www.cricbuzz.com/live-cricket-scorecard/{cricbuzz_id}"
    html, nxt = _get_page(url)
    if not html:
        return {}

    # ── Batting ───────────────────────────────────────
    batting = []
    seen_bat: set[str] = set()

    for m in re.finditer(r'"batName":"([^"]+)"', nxt):
        name = m.group(1)
        after = nxt[m.end(): m.end() + 400]
        runs_m   = re.search(r'"runs"\s*:\s*(\d+)',   after)
        balls_m  = re.search(r'"balls"\s*:\s*(\d+)',  after)
        fours_m  = re.search(r'"fours"\s*:\s*(\d+)',  after)
        sixes_m  = re.search(r'"sixes"\s*:\s*(\d+)',  after)
        out_m    = re.search(r'"outDesc"\s*:\s*"([^"]*)"', after)
        not_out  = re.search(r'"notOut"\s*:\s*(true|false)', after)

        if runs_m and balls_m and fours_m and sixes_m:
            r = int(runs_m.group(1))
            b = int(balls_m.group(1))
            fo = int(fours_m.group(1))
            si = int(sixes_m.group(1))
            dedup = f"{name}_{r}_{b}"
            if dedup in seen_bat:
                continue
            seen_bat.add(dedup)
            sr = round((r / b) * 100, 2) if b else 0
            dismissal = out_m.group(1) if out_m else ""
            is_not_out = not_out and not_out.group(1) == "true"
            batting.append({
                "name": name,
                "runs": r,
                "balls": b,
                "fours": fo,
                "sixes": si,
                "sr": sr,
                "dismissal": dismissal,
                "not_out": is_not_out or dismissal == "",
            })

    # ── Bowling ───────────────────────────────────────
    bowling = []
    seen_bowl: set[str] = set()

    # Pattern 1 – full record
    for m in re.finditer(r'"bowlName"\s*:\s*"([^"]+)"', nxt):
        name = m.group(1)
        after = nxt[m.end(): m.end() + 300]
        overs_m   = re.search(r'"overs"\s*:\s*"?([\d.]+)"?',   after)
        maidens_m = re.search(r'"maidens"\s*:\s*(\d+)',         after)
        runs_m    = re.search(r'"runs"\s*:\s*(\d+)',            after)
        wickets_m = re.search(r'"wickets"\s*:\s*(\d+)',         after)

        if overs_m and runs_m and wickets_m:
            ov  = float(overs_m.group(1))
            mai = int(maidens_m.group(1)) if maidens_m else 0
            r   = int(runs_m.group(1))
            w   = int(wickets_m.group(1))
            eco = round(r / ov, 2) if ov else 0
            dedup = f"{name}_{ov}_{r}_{w}"
            if dedup in seen_bowl:
                continue
            seen_bowl.add(dedup)
            bowling.append({
                "name": name,
                "overs": ov,
                "maidens": mai,
                "runs": r,
                "wickets": w,
                "eco": eco,
            })

    # ── Extras ───────────────────────────────────────
    extras_list = []
    for em in re.finditer(
        r'"extrasData"\s*:\s*\{[^}]+\}', nxt
    ):
        chunk = em.group(0)
        tot = re.search(r'"total"\s*:\s*(\d+)', chunk)
        b_  = re.search(r'"byes"\s*:\s*(\d+)', chunk)
        lb_ = re.search(r'"legByes"\s*:\s*(\d+)', chunk)
        nb_ = re.search(r'"noBalls"\s*:\s*(\d+)', chunk)
        w_  = re.search(r'"wides"\s*:\s*(\d+)', chunk)
        extras_list.append({
            "total":   int(tot.group(1)) if tot else 0,
            "byes":    int(b_.group(1))  if b_  else 0,
            "legByes": int(lb_.group(1)) if lb_ else 0,
            "noBalls": int(nb_.group(1)) if nb_ else 0,
            "wides":   int(w_.group(1))  if w_  else 0,
        })

    # ── Innings totals ────────────────────────────────
    innings_totals = []
    for im in re.finditer(r'"runs"\s*:\s*(\d+)\s*,\s*"wickets"\s*:\s*(\d+)\s*,\s*"overs"\s*:\s*"?([\d.]+)"?', nxt[:6000]):
        innings_totals.append({
            "runs":    int(im.group(1)),
            "wickets": int(im.group(2)),
            "overs":   im.group(3),
        })

    # ── Match result ─────────────────────────────────
    result_matches = re.findall(
        r'"status"\s*:\s*"([^"]*(?:won by|tied|no result|abandoned|draw)[^"]*)"',
        nxt, re.IGNORECASE,
    )
    match_result = result_matches[0] if result_matches else None
    is_completed = bool(match_result) or len(batting) >= 20

    return {
        "batting":       batting,
        "bowling":       bowling,
        "extras":        extras_list,
        "innings_totals": innings_totals,
        "match_result":  match_result,
        "is_completed":  is_completed,
    }


# ─────────────────────────────────────────────────────
#  BALL-BY-BALL COMMENTARY
# ─────────────────────────────────────────────────────

def scrape_commentary(cricbuzz_id: str, innings: int = 1) -> list[dict]:
    """Scrape ball-by-ball commentary for a specific innings."""
    url = f"https://www.cricbuzz.com/live-cricket-scorecard/{cricbuzz_id}"
    html, nxt = _get_page(url)
    if not nxt:
        return []

    balls = []
    seen_balls: set[str] = set()

    # Pattern: "overNumber":N,"ballNumber":N,"commText":"...","batRuns":N,...
    for m in re.finditer(
        r'"overNumber"\s*:\s*(\d+)\s*,\s*"ballNumber"\s*:\s*(\d+)',
        nxt,
    ):
        ov_no  = int(m.group(1))
        ball_no = int(m.group(2))
        after = nxt[m.start(): m.start() + 600]

        comm    = re.search(r'"commText"\s*:\s*"([^"]+)"', after)
        bat_r   = re.search(r'"batRuns"\s*:\s*(\d+)',      after)
        ext_r   = re.search(r'"extraRuns"\s*:\s*(\d+)',    after)
        is_wick = re.search(r'"isWicket"\s*:\s*(true|false)', after)
        is_four = re.search(r'"isFour"\s*:\s*(true|false)', after)
        is_six  = re.search(r'"isSix"\s*:\s*(true|false)',  after)
        batter  = re.search(r'"batName"\s*:\s*"([^"]+)"',  after)
        bowler  = re.search(r'"bowlName"\s*:\s*"([^"]+)"', after)

        dedup = f"{ov_no}.{ball_no}"
        if dedup in seen_balls:
            continue
        seen_balls.add(dedup)

        br = int(bat_r.group(1)) if bat_r else 0
        er = int(ext_r.group(1)) if ext_r else 0
        wicket = is_wick and is_wick.group(1) == "true"
        four   = is_four and is_four.group(1) == "true"
        six    = is_six  and is_six.group(1)  == "true"

        ball_type = "W" if wicket else "6" if six else "4" if four else str(br)

        balls.append({
            "over":     ov_no,
            "ball":     ball_no,
            "label":    f"{ov_no}.{ball_no}",
            "runs":     br,
            "extras":   er,
            "type":     ball_type,
            "wicket":   wicket,
            "four":     four,
            "six":      six,
            "batter":   batter.group(1) if batter else "",
            "bowler":   bowler.group(1) if bowler else "",
            "comment":  comm.group(1) if comm else "",
        })

    # Sort by over.ball ascending
    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls


# ─────────────────────────────────────────────────────
#  PLAYING XI
# ─────────────────────────────────────────────────────

def scrape_playing_xi(cricbuzz_id: str) -> dict[str, list[str]]:
    url = f"https://www.cricbuzz.com/live-cricket-scores/{cricbuzz_id}"
    html, nxt = _get_page(url)
    if not nxt:
        return {}

    teams: dict[str, list[str]] = {}
    xi_matches = re.findall(
        r'<b>([^<]+)</b>\s*\(Playing XI\):\s*([^"<]+)',
        nxt,
    )
    for team_name, players_str in xi_matches:
        players = [
            p.strip().split("(")[0].strip()
            for p in players_str.split(",")
            if p.strip()
        ]
        teams[team_name.strip()] = players

    if not teams:
        # Try JSON-style playing XI
        for m in re.finditer(r'"team"\s*:\s*"([^"]+)"[^}]*?"players"\s*:\s*\[([^\]]+)\]', nxt):
            team = m.group(1)
            raw_players = re.findall(r'"name"\s*:\s*"([^"]+)"', m.group(2))
            if raw_players:
                teams[team] = raw_players

    return teams


# ─────────────────────────────────────────────────────
#  Vercel handler
# ─────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

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
        match_id = qs.get("id", [None])[0]
        include  = qs.get("include", ["scorecard,bbb,xi"])[0].split(",")

        if not match_id:
            self._send(400, {"error": "Missing ?id=<cricbuzz_match_id>"})
            return

        result: dict = {
            "status":     "success",
            "source":     "cricbuzz",
            "match_id":   match_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if "scorecard" in include:
            sc = scrape_scorecard(match_id)
            result["scorecard"] = sc

        if "bbb" in include:
            bbb = scrape_commentary(match_id)
            result["bbb"] = bbb

        if "xi" in include:
            xi = scrape_playing_xi(match_id)
            result["playing_xi"] = xi

        if not result.get("scorecard") and not result.get("bbb"):
            result["status"] = "empty"

        self._send(200, result)

    def _send(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=25")
        self.end_headers()
        self.wfile.write(body)
