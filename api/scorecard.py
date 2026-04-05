"""
/api/scorecard?id={cricbuzz_match_id}
Returns batting, bowling, ball-by-ball commentary, playing XI, analysis.
Scrapes directly from Cricbuzz scorecard + commentary pages.
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

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cricbuzz.com/",
    "Cache-Control": "no-cache",
}

JSON_HEADERS = {
    **HEADERS,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_html(url: str) -> tuple[str, str]:
    """Fetch URL → (raw_html, decoded_nextjs_chunks)."""
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        html = r.text
        chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
        nxt = ""
        for c in chunks:
            try:    nxt += c.encode().decode("unicode_escape")
            except: nxt += c
        return html, nxt
    except Exception as e:
        logger.error("fetch_html %s: %s", url, e)
        return "", ""


def fetch_json(url: str) -> dict:
    try:
        r = httpx.get(url, headers=JSON_HEADERS, timeout=15, follow_redirects=True)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error("fetch_json %s: %s", url, e)
        return {}


# ── BATTING ──────────────────────────────────────────────────────────────────

def scrape_batting(nxt: str) -> list:
    out, seen = [], set()
    for m in re.finditer(r'"batName"\s*:\s*"([^"]+)"', nxt):
        name = m.group(1).strip()
        after = nxt[m.end(): m.end()+500]
        runs  = re.search(r'"runs"\s*:\s*(\d+)',     after)
        balls = re.search(r'"balls"\s*:\s*(\d+)',    after)
        fours = re.search(r'"fours"\s*:\s*(\d+)',    after)
        sixes = re.search(r'"sixes"\s*:\s*(\d+)',    after)
        out_d = re.search(r'"outDesc"\s*:\s*"([^"]*)"', after)
        no    = re.search(r'"notOut"\s*:\s*(true|false)', after)

        if not (runs and balls and fours and sixes): continue
        r_, b_, fo_, si_ = int(runs.group(1)), int(balls.group(1)), int(fours.group(1)), int(sixes.group(1))
        key = f"{name}_{r_}_{b_}"
        if key in seen: continue
        seen.add(key)

        sr  = round((r_ / b_) * 100, 1) if b_ else 0.0
        dis = out_d.group(1).strip() if out_d else ""
        not_out = (no and no.group(1) == "true") or dis == ""
        out.append({"name": name, "runs": r_, "balls": b_, "fours": fo_,
                    "sixes": si_, "sr": sr, "dismissal": dis, "not_out": not_out})
    return out


# ── BOWLING ──────────────────────────────────────────────────────────────────

def scrape_bowling(nxt: str) -> list:
    out, seen = [], set()
    for m in re.finditer(r'"bowlName"\s*:\s*"([^"]+)"', nxt):
        name  = m.group(1).strip()
        after = nxt[m.end(): m.end()+400]
        overs   = re.search(r'"overs"\s*:\s*"?([\d.]+)"?', after)
        maidens = re.search(r'"maidens"\s*:\s*(\d+)', after)
        runs    = re.search(r'"runs"\s*:\s*(\d+)',    after)
        wickets = re.search(r'"wickets"\s*:\s*(\d+)', after)
        if not (overs and runs and wickets): continue
        ov, r_, w_ = float(overs.group(1)), int(runs.group(1)), int(wickets.group(1))
        mai = int(maidens.group(1)) if maidens else 0
        eco = round(r_ / ov, 2) if ov else 0.0
        key = f"{name}_{ov}_{r_}_{w_}"
        if key in seen: continue
        seen.add(key)
        out.append({"name": name, "overs": ov, "maidens": mai, "runs": r_, "wickets": w_, "eco": eco})
    return out


# ── EXTRAS & TOTALS ──────────────────────────────────────────────────────────

def scrape_extras(nxt: str) -> list:
    extras = []
    for em in re.finditer(r'"extrasData"\s*:\s*\{([^}]+)\}', nxt):
        c = em.group(1)
        def g(k): m = re.search(rf'"{k}"\s*:\s*(\d+)', c); return int(m.group(1)) if m else 0
        extras.append({"total": g("total"), "byes": g("byes"), "legByes": g("legByes"),
                       "noBalls": g("noBalls"), "wides": g("wides")})
    return extras


def scrape_totals(nxt: str) -> list:
    totals = []
    seen = set()
    for m in re.finditer(
        r'"runs"\s*:\s*(\d+)\s*,\s*"wickets"\s*:\s*(\d+)\s*,\s*"overs"\s*:\s*"?([\d.]+)"?',
        nxt[:10000]
    ):
        key = f"{m.group(1)}_{m.group(2)}_{m.group(3)}"
        if key in seen: continue
        seen.add(key)
        totals.append({"runs": int(m.group(1)), "wickets": int(m.group(2)), "overs": m.group(3)})
    return totals


# ── BALL BY BALL + COMMENTARY ─────────────────────────────────────────────────

def scrape_commentary_api(match_id: str) -> list:
    """Try Cricbuzz's internal commentary API first (fastest, most complete)."""
    # Try the JSON commentary endpoint
    for url in [
        f"https://www.cricbuzz.com/api/cricket-match/{match_id}/commentary",
        f"https://www.cricbuzz.com/api/html/cricket-commentary/{match_id}",
    ]:
        try:
            r = httpx.get(url, headers=JSON_HEADERS, timeout=12, follow_redirects=True)
            if r.status_code == 200:
                data = r.json() if "json" in r.headers.get("content-type", "") else {}
                if data:
                    return _parse_commentary_json(data)
        except Exception:
            pass
    return []


def _parse_commentary_json(data: dict) -> list:
    """Parse Cricbuzz commentary JSON format."""
    balls = []
    seen  = set()

    # Handle different JSON structures
    items = (data.get("commentary", []) or
             data.get("commentaryList", []) or
             data.get("data", {}).get("commentaryList", []) or [])

    for item in items:
        ov  = item.get("overNumber", item.get("over", 0))
        bn  = item.get("ballNumber", item.get("ball", 0))
        key = f"{ov}.{bn}"
        if key in seen: continue
        seen.add(key)

        comm    = item.get("commText", item.get("commentary", "")).strip()
        bat_r   = item.get("batRuns",  item.get("runs", 0)) or 0
        ext_r   = item.get("extraRuns", 0) or 0
        wicket  = item.get("isWicket", False) or item.get("wicket", False)
        four    = item.get("isFour", False) or item.get("four", False)
        six     = item.get("isSix",  False) or item.get("six",  False)
        batter  = item.get("batName", item.get("batsmanName", ""))
        bowler  = item.get("bowlName", item.get("bowlerName", ""))
        btype   = "W" if wicket else "6" if six else "4" if four else str(bat_r)

        balls.append({"over": ov, "ball": bn, "label": key,
                      "runs": bat_r, "extras": ext_r, "type": btype,
                      "wicket": wicket, "four": four, "six": six,
                      "batter": batter, "bowler": bowler, "comment": comm})

    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls


def scrape_bbb_from_page(nxt: str) -> list:
    """Extract ball-by-ball from Next.js page chunks (fallback)."""
    balls = []
    seen  = set()

    for m in re.finditer(r'"overNumber"\s*:\s*(\d+)\s*,\s*"ballNumber"\s*:\s*(\d+)', nxt):
        ov, bn = int(m.group(1)), int(m.group(2))
        key = f"{ov}.{bn}"
        if key in seen: continue
        seen.add(key)

        after  = nxt[m.start(): m.start()+700]
        comm   = re.search(r'"commText"\s*:\s*"([^"]+)"',  after)
        bat_r  = re.search(r'"batRuns"\s*:\s*(\d+)',        after)
        ext_r  = re.search(r'"extraRuns"\s*:\s*(\d+)',      after)
        is_w   = re.search(r'"isWicket"\s*:\s*(true|false)', after)
        is_4   = re.search(r'"isFour"\s*:\s*(true|false)',   after)
        is_6   = re.search(r'"isSix"\s*:\s*(true|false)',    after)
        batter = re.search(r'"batName"\s*:\s*"([^"]+)"',    after)
        bowler = re.search(r'"bowlName"\s*:\s*"([^"]+)"',   after)

        br     = int(bat_r.group(1)) if bat_r else 0
        er     = int(ext_r.group(1)) if ext_r else 0
        wicket = is_w and is_w.group(1) == "true"
        four   = is_4 and is_4.group(1) == "true"
        six    = is_6 and is_6.group(1) == "true"
        btype  = "W" if wicket else "6" if six else "4" if four else str(br)

        balls.append({
            "over": ov, "ball": bn, "label": key,
            "runs": br, "extras": er, "type": btype,
            "wicket": wicket, "four": four, "six": six,
            "batter":  batter.group(1) if batter else "",
            "bowler":  bowler.group(1) if bowler else "",
            "comment": comm.group(1).strip() if comm else "",
        })

    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls


# ── PLAYING XI ───────────────────────────────────────────────────────────────

def scrape_xi(nxt: str) -> dict:
    teams: dict = {}
    # Pattern 1: <b>Team</b> (Playing XI): p1, p2, ...
    for m in re.finditer(r'<b>([^<]+)</b>\s*\(Playing XI\):\s*([^"<]{20,})', nxt):
        team    = m.group(1).strip()
        players = [p.strip().split("(")[0].strip() for p in m.group(2).split(",") if p.strip()]
        teams[team] = [p for p in players if len(p) > 1]

    # Pattern 2: JSON style
    if not teams:
        for m in re.finditer(
            r'"teamName"\s*:\s*"([^"]+)"[^}]{0,300}?"playersXI"\s*:\s*\[([^\]]+)\]', nxt
        ):
            team    = m.group(1)
            players = re.findall(r'"(?:name|playerName)"\s*:\s*"([^"]+)"', m.group(2))
            if players: teams[team] = players

    return teams


# ── ANALYSIS ─────────────────────────────────────────────────────────────────

def build_analysis(batting: list, bowling: list, totals: list, bbb: list) -> dict:
    a: dict = {}
    if totals:
        tot = totals[0]
        try:
            ov = float(tot.get("overs", 0) or 0)
            a["crr"] = round(tot["runs"] / ov, 2) if ov else 0
        except: pass

    if batting:
        a["top_bat"]  = sorted(batting, key=lambda x: -x.get("runs", 0))[:3]
        a["fours"]    = sum(b.get("fours", 0) for b in batting)
        a["sixes"]    = sum(b.get("sixes", 0) for b in batting)

    if bowling:
        a["top_bowl"] = sorted(bowling, key=lambda x: (-x.get("wickets", 0), x.get("eco", 99)))[:3]
        a["maidens"]  = sum(b.get("maidens", 0) for b in bowling)

    if bbb:
        dots     = sum(1 for b in bbb if str(b.get("type","")) in ("0","·","") and not b.get("wicket"))
        a["dot_pct"] = round((dots / len(bbb)) * 100, 1)
        a["wickets_in_bbb"] = sum(1 for b in bbb if b.get("wicket"))

        # Runs per over
        ov_runs: dict = {}
        for b in bbb:
            ov = b.get("over", 0)
            ov_runs[ov] = ov_runs.get(ov, 0) + b.get("runs", 0) + b.get("extras", 0)
        a["runs_per_over"] = [{"over": int(k)+1, "runs": v}
                               for k, v in sorted(ov_runs.items(), key=lambda x: int(x[0]))]

    return a


# ── MATCH RESULT ─────────────────────────────────────────────────────────────

def find_result(nxt: str) -> str | None:
    m = re.search(
        r'"status"\s*:\s*"([^"]*(?:won by|tied|no result|abandoned|draw)[^"]*)"',
        nxt, re.I)
    return m.group(1) if m else None


# ── VERCEL HANDLER ───────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        qs       = parse_qs(urlparse(self.path).query)
        match_id = qs.get("id", [None])[0]
        if not match_id:
            self._send(400, {"error": "Missing ?id="}); return

        # Fetch scorecard page
        _, nxt = fetch_html(f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}")

        batting  = scrape_batting(nxt)
        bowling  = scrape_bowling(nxt)
        extras   = scrape_extras(nxt)
        totals   = scrape_totals(nxt)
        xi       = scrape_xi(nxt)
        result   = find_result(nxt)

        # Commentary: try API first, then page
        bbb = scrape_commentary_api(match_id)
        if not bbb:
            bbb = scrape_bbb_from_page(nxt)

        analysis = build_analysis(batting, bowling, totals, bbb)

        self._send(200, {
            "status":       "success" if (batting or bbb) else "empty",
            "source":       "live",
            "match_id":     match_id,
            "updated_at":   datetime.now(timezone.utc).isoformat(),
            "batting":      batting,
            "bowling":      bowling,
            "extras":       extras,
            "totals":       totals,
            "bbb":          bbb,
            "playing_xi":   xi,
            "analysis":     analysis,
            "match_result": result,
        })

    def _send(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=18, stale-while-revalidate=22")
        self.end_headers()
        self.wfile.write(body)
