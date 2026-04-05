"""
/api/scorecard?id={match_id}
Multi-strategy scorecard + ball-by-ball from Cricbuzz.
Strategy 1: Cricbuzz commentary JSON API
Strategy 2: Next.js chunk parsing from scorecard page
Strategy 3: HTML table parsing
"""
import re, json, logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

BROWSER_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Mobile/15E148 Safari/604.1"
)
HEADERS_HTML = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cricbuzz.com/",
    "Cache-Control": "no-cache",
}
HEADERS_JSON = {
    **HEADERS_HTML,
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_html(url: str, timeout=18) -> tuple[str, str]:
    try:
        r = httpx.get(url, headers=HEADERS_HTML, timeout=timeout, follow_redirects=True)
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


def fetch_json(url: str, timeout=10) -> dict:
    try:
        r = httpx.get(url, headers=HEADERS_JSON, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        ct = r.headers.get("content-type","")
        if "json" in ct:
            return r.json()
        # Try to parse even if wrong content-type
        try: return r.json()
        except Exception: return {}
    except Exception as e:
        logger.debug("fetch_json %s: %s", url, e)
        return {}


# ── STRATEGY 1: Cricbuzz Commentary JSON API ─────────────────────────────────

def try_commentary_api(match_id: str) -> list:
    """Try Cricbuzz internal commentary API endpoints."""
    endpoints = [
        f"https://www.cricbuzz.com/api/cricket-match/{match_id}/commentary",
        f"https://www.cricbuzz.com/api/cricket-match/{match_id}/commentary/1",
        f"https://www.cricbuzz.com/api/cricket-match/{match_id}/commentary/2",
    ]
    for url in endpoints:
        data = fetch_json(url, timeout=8)
        if not data: continue
        balls = _parse_commentary_json(data)
        if balls:
            logger.info("Got %d balls from %s", len(balls), url)
            return balls
    return []


def _parse_commentary_json(data: dict) -> list:
    balls, seen = [], set()
    # Multiple possible keys
    items = (
        data.get("commentary", []) or
        data.get("commentaryList", []) or
        data.get("data", {}).get("commentaryList", []) or
        []
    )
    for item in items:
        ov  = item.get("overNumber", item.get("over", 0)) or 0
        bn  = item.get("ballNumber", item.get("ball", 0)) or 0
        key = f"{ov}.{bn}"
        if key in seen: continue
        seen.add(key)
        comm   = (item.get("commText") or item.get("commentary") or "").strip()
        bat_r  = int(item.get("batRuns") or item.get("runs") or 0)
        ext_r  = int(item.get("extraRuns") or 0)
        wicket = bool(item.get("isWicket") or item.get("wicket"))
        four   = bool(item.get("isFour") or item.get("four"))
        six    = bool(item.get("isSix") or item.get("six"))
        batter = (item.get("batName") or item.get("batsmanName") or "")
        bowler = (item.get("bowlName") or item.get("bowlerName") or "")
        btype  = "W" if wicket else "6" if six else "4" if four else str(bat_r)
        balls.append({"over": ov, "ball": bn, "label": key,
                      "runs": bat_r, "extras": ext_r, "type": btype,
                      "wicket": wicket, "four": four, "six": six,
                      "batter": batter, "bowler": bowler, "comment": comm})
    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls


# ── STRATEGY 2: Next.js Chunk Parsing ────────────────────────────────────────

def parse_bbb_nxt(nxt: str) -> list:
    balls, seen = [], set()
    for m in re.finditer(
        r'"overNumber"\s*:\s*(\d+)\s*,\s*"ballNumber"\s*:\s*(\d+)', nxt
    ):
        ov, bn = int(m.group(1)), int(m.group(2))
        key = f"{ov}.{bn}"
        if key in seen: continue
        seen.add(key)
        after  = nxt[m.start(): m.start()+800]
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
        wicket = bool(is_w and is_w.group(1) == "true")
        four   = bool(is_4 and is_4.group(1) == "true")
        six    = bool(is_6 and is_6.group(1) == "true")
        btype  = "W" if wicket else "6" if six else "4" if four else str(br)
        balls.append({"over": ov, "ball": bn, "label": key,
                      "runs": br, "extras": er, "type": btype,
                      "wicket": wicket, "four": four, "six": six,
                      "batter":  batter.group(1) if batter else "",
                      "bowler":  bowler.group(1) if bowler else "",
                      "comment": comm.group(1).strip() if comm else ""})
    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls


# ── STRATEGY 3: HTML Table Parsing ───────────────────────────────────────────

def parse_html_batting(html: str) -> list:
    """Parse batting scorecard from HTML table."""
    batting, seen = [], set()
    # Look for table rows with batting data
    rows = re.findall(
        r'<td[^>]*>([A-Z][a-z]+ [A-Z][^<]{2,40})</td>.*?'
        r'<td[^>]*>(\d+)</td>.*?'   # runs
        r'<td[^>]*>(\d+)</td>.*?'   # balls
        r'<td[^>]*>(\d+)</td>.*?'   # fours
        r'<td[^>]*>(\d+)</td>',     # sixes
        html, re.DOTALL
    )
    for row in rows[:20]:
        name = row[0].strip()
        r_, b_, fo_, si_ = int(row[1]), int(row[2]), int(row[3]), int(row[4])
        key = f"{name}_{r_}_{b_}"
        if key in seen: continue
        seen.add(key)
        sr = round((r_/b_)*100, 1) if b_ else 0.0
        batting.append({"name": name, "runs": r_, "balls": b_, "fours": fo_,
                        "sixes": si_, "sr": sr, "dismissal": "", "not_out": False})
    return batting


# ── SCORECARD PARSING ─────────────────────────────────────────────────────────

def parse_batting_nxt(nxt: str) -> list:
    out, seen = [], set()
    for m in re.finditer(r'"batName"\s*:\s*"([^"]+)"', nxt):
        name  = m.group(1).strip()
        after = nxt[m.end(): m.end()+500]
        runs  = re.search(r'"runs"\s*:\s*(\d+)',  after)
        balls = re.search(r'"balls"\s*:\s*(\d+)', after)
        fours = re.search(r'"fours"\s*:\s*(\d+)', after)
        sixes = re.search(r'"sixes"\s*:\s*(\d+)', after)
        out_d = re.search(r'"outDesc"\s*:\s*"([^"]*)"', after)
        no    = re.search(r'"notOut"\s*:\s*(true|false)', after)
        if not (runs and balls and fours and sixes): continue
        r_,b_,fo_,si_ = int(runs.group(1)),int(balls.group(1)),int(fours.group(1)),int(sixes.group(1))
        key = f"{name}_{r_}_{b_}"
        if key in seen: continue
        seen.add(key)
        sr  = round((r_/b_)*100,1) if b_ else 0.0
        dis = out_d.group(1).strip() if out_d else ""
        not_out = (no and no.group(1)=="true") or dis==""
        out.append({"name":name,"runs":r_,"balls":b_,"fours":fo_,"sixes":si_,
                    "sr":sr,"dismissal":dis,"not_out":not_out})
    return out


def parse_bowling_nxt(nxt: str) -> list:
    out, seen = [], set()
    for m in re.finditer(r'"bowlName"\s*:\s*"([^"]+)"', nxt):
        name  = m.group(1).strip()
        after = nxt[m.end(): m.end()+400]
        overs   = re.search(r'"overs"\s*:\s*"?([\d.]+)"?', after)
        maidens = re.search(r'"maidens"\s*:\s*(\d+)', after)
        runs    = re.search(r'"runs"\s*:\s*(\d+)',    after)
        wickets = re.search(r'"wickets"\s*:\s*(\d+)', after)
        if not (overs and runs and wickets): continue
        ov,r_,w_ = float(overs.group(1)),int(runs.group(1)),int(wickets.group(1))
        mai = int(maidens.group(1)) if maidens else 0
        eco = round(r_/ov,2) if ov else 0.0
        key = f"{name}_{ov}_{r_}_{w_}"
        if key in seen: continue
        seen.add(key)
        out.append({"name":name,"overs":ov,"maidens":mai,"runs":r_,"wickets":w_,"eco":eco})
    return out


def parse_extras_nxt(nxt: str) -> list:
    extras = []
    for em in re.finditer(r'"extrasData"\s*:\s*\{([^}]+)\}', nxt):
        c = em.group(1)
        def g(k): x = re.search(rf'"{k}"\s*:\s*(\d+)',c); return int(x.group(1)) if x else 0
        extras.append({"total":g("total"),"byes":g("byes"),"legByes":g("legByes"),
                       "noBalls":g("noBalls"),"wides":g("wides")})
    return extras


def parse_totals_nxt(nxt: str) -> list:
    totals, seen = [], set()
    for m in re.finditer(
        r'"runs"\s*:\s*(\d+)\s*,\s*"wickets"\s*:\s*(\d+)\s*,\s*"overs"\s*:\s*"?([\d.]+)"?',
        nxt[:12000]
    ):
        key = f"{m.group(1)}_{m.group(2)}_{m.group(3)}"
        if key in seen: continue
        seen.add(key)
        totals.append({"runs":int(m.group(1)),"wickets":int(m.group(2)),"overs":m.group(3)})
    return totals


def parse_xi_nxt(nxt: str) -> dict:
    teams: dict = {}
    for m in re.finditer(r'<b>([^<]+)</b>\s*\(Playing XI\):\s*([^"<]{20,})', nxt):
        team    = m.group(1).strip()
        players = [p.strip().split("(")[0].strip() for p in m.group(2).split(",") if p.strip()]
        teams[team] = [p for p in players if len(p)>1]
    if not teams:
        for m in re.finditer(r'"teamName"\s*:\s*"([^"]+)"[^}]{0,300}?"playersXI"\s*:\s*\[([^\]]+)\]', nxt):
            players = re.findall(r'"(?:name|playerName)"\s*:\s*"([^"]+)"', m.group(2))
            if players: teams[m.group(1)] = players
    return teams


def build_analysis(batting, bowling, totals, bbb) -> dict:
    a: dict = {}
    if totals:
        try:
            ov = float(totals[0].get("overs",0) or 0)
            a["crr"] = round(totals[0]["runs"]/ov,2) if ov else 0
        except Exception: pass

    if batting:
        a["top_bat"]  = sorted(batting, key=lambda x:-x.get("runs",0))[:5]
        a["fours"]    = sum(b.get("fours",0) for b in batting)
        a["sixes"]    = sum(b.get("sixes",0) for b in batting)
        a["total_runs"] = sum(b.get("runs",0) for b in batting)

    if bowling:
        a["top_bowl"] = sorted(bowling, key=lambda x:(-x.get("wickets",0),x.get("eco",99)))[:5]
        a["maidens"]  = sum(b.get("maidens",0) for b in bowling)

    if bbb:
        dots      = sum(1 for b in bbb if str(b.get("type","")) in ("0","·","") and not b.get("wicket"))
        a["dot_pct"] = round((dots/len(bbb))*100,1)

        # Runs per over + worm (cumulative)
        ov_runs: dict = {}
        for b in bbb:
            ov = int(b.get("over",0))
            ov_runs[ov] = ov_runs.get(ov,0) + b.get("runs",0) + b.get("extras",0)
        ovs = sorted(ov_runs.items(), key=lambda x: x[0])
        a["runs_per_over"] = [{"over": int(k)+1, "runs": v} for k,v in ovs]
        # Cumulative worm
        cum, worm = 0, []
        for k,v in ovs:
            cum += v
            worm.append({"over": int(k)+1, "total": cum})
        a["worm"] = worm

        # Wickets per over
        wkt_overs = {}
        for b in bbb:
            if b.get("wicket"):
                ov = int(b.get("over",0))+1
                wkt_overs[ov] = wkt_overs.get(ov,0)+1
        a["wickets_per_over"] = [{"over":k,"w":v} for k,v in sorted(wkt_overs.items())]

        # Boundary balls count
        a["fours_bbb"]  = sum(1 for b in bbb if b.get("four"))
        a["sixes_bbb"]  = sum(1 for b in bbb if b.get("six"))
        a["wickets_bbb"]= sum(1 for b in bbb if b.get("wicket"))

    return a


# ── HANDLER ───────────────────────────────────────────────────────────────────

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

        # Fetch scorecard page HTML + Next.js chunks
        _, nxt = fetch_html(f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}")

        batting  = parse_batting_nxt(nxt)
        bowling  = parse_bowling_nxt(nxt)
        extras   = parse_extras_nxt(nxt)
        totals   = parse_totals_nxt(nxt)
        xi       = parse_xi_nxt(nxt)

        # Match result
        result_m = re.findall(
            r'"status"\s*:\s*"([^"]*(?:won by|tied|no result|abandoned|draw)[^"]*)"',
            nxt, re.I)
        result = result_m[0] if result_m else None

        # Commentary: try API → NXT chunks
        bbb = try_commentary_api(match_id)
        if not bbb:
            bbb = parse_bbb_nxt(nxt)

        # If batting still empty, try HTML table
        if not batting:
            html, _ = fetch_html(f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}", timeout=12)
            batting = parse_html_batting(html)

        analysis = build_analysis(batting, bowling, totals, bbb)

        self._send(200, {
            "status":       "success" if (batting or bbb) else "empty",
            "match_id":     match_id,
            "updated_at":   datetime.now(IST).isoformat(),
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
