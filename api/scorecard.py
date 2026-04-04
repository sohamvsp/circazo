"""
/api/scorecard?id={match_id}
Returns: batting, bowling, ball-by-ball, playing XI, analysis
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
HEADERS = {"User-Agent": UA, "Accept": "text/html,*/*",
           "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.google.com/"}
 
 
def fetch(url: str) -> tuple[str, str]:
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
        logger.error("Fetch %s: %s", url, e)
        return "", ""
 
 
# ── BATTING ──────────────────────────────────────────────────────────────────
 
def scrape_batting(nxt: str) -> list[dict]:
    batting, seen = [], set()
    for m in re.finditer(r'"batName"\s*:\s*"([^"]+)"', nxt):
        name = m.group(1).strip()
        after = nxt[m.end(): m.end() + 500]
        runs  = re.search(r'"runs"\s*:\s*(\d+)',  after)
        balls = re.search(r'"balls"\s*:\s*(\d+)', after)
        fours = re.search(r'"fours"\s*:\s*(\d+)', after)
        sixes = re.search(r'"sixes"\s*:\s*(\d+)', after)
        out   = re.search(r'"outDesc"\s*:\s*"([^"]*)"', after)
        no    = re.search(r'"notOut"\s*:\s*(true|false)', after)
 
        if not (runs and balls and fours and sixes):
            continue
        r_, b_, fo_, si_ = int(runs.group(1)), int(balls.group(1)), int(fours.group(1)), int(sixes.group(1))
        key = f"{name}_{r_}_{b_}"
        if key in seen: continue
        seen.add(key)
 
        sr = round((r_ / b_) * 100, 1) if b_ else 0.0
        dis = out.group(1) if out else ""
        not_out = (no and no.group(1) == "true") or dis == ""
        batting.append({"name": name, "runs": r_, "balls": b_, "fours": fo_,
                        "sixes": si_, "sr": sr, "dismissal": dis, "not_out": not_out})
    return batting
 
 
# ── BOWLING ──────────────────────────────────────────────────────────────────
 
def scrape_bowling(nxt: str) -> list[dict]:
    bowling, seen = [], set()
    for m in re.finditer(r'"bowlName"\s*:\s*"([^"]+)"', nxt):
        name  = m.group(1).strip()
        after = nxt[m.end(): m.end() + 400]
        overs   = re.search(r'"overs"\s*:\s*"?([\d.]+)"?', after)
        maidens = re.search(r'"maidens"\s*:\s*(\d+)', after)
        runs    = re.search(r'"runs"\s*:\s*(\d+)',    after)
        wickets = re.search(r'"wickets"\s*:\s*(\d+)', after)
        if not (overs and runs and wickets): continue
        ov_, r_, w_ = float(overs.group(1)), int(runs.group(1)), int(wickets.group(1))
        mai_ = int(maidens.group(1)) if maidens else 0
        eco  = round(r_ / ov_, 2) if ov_ else 0.0
        key  = f"{name}_{ov_}_{r_}_{w_}"
        if key in seen: continue
        seen.add(key)
        bowling.append({"name": name, "overs": ov_, "maidens": mai_,
                        "runs": r_, "wickets": w_, "eco": eco})
    return bowling
 
 
# ── EXTRAS & TOTALS ──────────────────────────────────────────────────────────
 
def scrape_extras(nxt: str) -> list[dict]:
    extras = []
    for em in re.finditer(r'"extrasData"\s*:\s*\{([^}]+)\}', nxt):
        c = em.group(1)
        tot = re.search(r'"total"\s*:\s*(\d+)', c)
        b_  = re.search(r'"byes"\s*:\s*(\d+)',    c)
        lb  = re.search(r'"legByes"\s*:\s*(\d+)',  c)
        nb  = re.search(r'"noBalls"\s*:\s*(\d+)',  c)
        w_  = re.search(r'"wides"\s*:\s*(\d+)',    c)
        extras.append({
            "total":   int(tot.group(1)) if tot else 0,
            "byes":    int(b_.group(1))  if b_  else 0,
            "legByes": int(lb.group(1))  if lb  else 0,
            "noBalls": int(nb.group(1))  if nb  else 0,
            "wides":   int(w_.group(1))  if w_  else 0,
        })
    return extras
 
 
def scrape_totals(nxt: str) -> list[dict]:
    totals = []
    for m in re.finditer(
        r'"runs"\s*:\s*(\d+)\s*,\s*"wickets"\s*:\s*(\d+)\s*,\s*"overs"\s*:\s*"?([\d.]+)"?',
        nxt[:8000]
    ):
        totals.append({"runs": int(m.group(1)), "wickets": int(m.group(2)), "overs": m.group(3)})
    return totals
 
 
# ── BALL BY BALL ─────────────────────────────────────────────────────────────
 
def scrape_bbb(nxt: str) -> list[dict]:
    balls, seen = [], set()
    for m in re.finditer(r'"overNumber"\s*:\s*(\d+)\s*,\s*"ballNumber"\s*:\s*(\d+)', nxt):
        ov, bn = int(m.group(1)), int(m.group(2))
        key = f"{ov}.{bn}"
        if key in seen: continue
        seen.add(key)
 
        after  = nxt[m.start(): m.start() + 700]
        comm   = re.search(r'"commText"\s*:\s*"([^"]+)"', after)
        bat_r  = re.search(r'"batRuns"\s*:\s*(\d+)',       after)
        ext_r  = re.search(r'"extraRuns"\s*:\s*(\d+)',     after)
        is_w   = re.search(r'"isWicket"\s*:\s*(true|false)', after)
        is_4   = re.search(r'"isFour"\s*:\s*(true|false)', after)
        is_6   = re.search(r'"isSix"\s*:\s*(true|false)',  after)
        batter = re.search(r'"batName"\s*:\s*"([^"]+)"',  after)
        bowler = re.search(r'"bowlName"\s*:\s*"([^"]+)"', after)
 
        br = int(bat_r.group(1)) if bat_r else 0
        er = int(ext_r.group(1)) if ext_r else 0
        wicket = is_w and is_w.group(1) == "true"
        four   = is_4 and is_4.group(1) == "true"
        six    = is_6 and is_6.group(1) == "true"
        btype  = "W" if wicket else "6" if six else "4" if four else str(br)
 
        balls.append({
            "over": ov, "ball": bn, "label": f"{ov}.{bn}",
            "runs": br, "extras": er, "type": btype,
            "wicket": wicket, "four": four, "six": six,
            "batter":  batter.group(1) if batter else "",
            "bowler":  bowler.group(1) if bowler else "",
            "comment": comm.group(1)   if comm   else "",
        })
 
    balls.sort(key=lambda b: (b["over"], b["ball"]))
    return balls
 
 
# ── PLAYING XI ───────────────────────────────────────────────────────────────
 
def scrape_xi(nxt: str) -> dict:
    teams: dict[str, list[str]] = {}
    for m in re.finditer(r'<b>([^<]+)</b>\s*\(Playing XI\):\s*([^"<]{20,})', nxt):
        team_name = m.group(1).strip()
        players = [p.strip().split("(")[0].strip() for p in m.group(2).split(",") if p.strip()]
        teams[team_name] = [p for p in players if p]
    # JSON style
    if not teams:
        for m in re.finditer(r'"teamName"\s*:\s*"([^"]+)"[^}]{0,200}?"playersXI"\s*:\s*\[([^\]]+)\]', nxt):
            team = m.group(1)
            players = re.findall(r'"(?:name|playerName)"\s*:\s*"([^"]+)"', m.group(2))
            if players: teams[team] = players
    return teams
 
 
# ── ANALYSIS ─────────────────────────────────────────────────────────────────
 
def build_analysis(batting: list, bowling: list, totals: list, bbb: list) -> dict:
    analysis: dict = {}
 
    # Run rate
    if totals:
        tot = totals[0]
        r, ov = tot.get("runs", 0), tot.get("overs", "0")
        try:
            ov_f = float(ov)
            analysis["crr"] = round(r / ov_f, 2) if ov_f else 0
        except: pass
 
    # Top scorers
    if batting:
        analysis["top_bat"] = sorted(batting, key=lambda x: -x.get("runs", 0))[:3]
 
    # Top wicket takers
    if bowling:
        analysis["top_bowl"] = sorted(bowling, key=lambda x: (-x.get("wickets", 0), x.get("eco", 99)))[:3]
 
    # Boundary count
    if batting:
        analysis["fours"] = sum(b.get("fours", 0) for b in batting)
        analysis["sixes"] = sum(b.get("sixes", 0) for b in batting)
 
    # Dot ball % from bbb
    if bbb:
        dots = sum(1 for b in bbb if b.get("type") in ("0", "·", "") and not b.get("wicket"))
        analysis["dot_pct"] = round((dots / len(bbb)) * 100, 1)
 
    return analysis
 
 
# ── VERCEL HANDLER ───────────────────────────────────────────────────────────
 
class handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
 
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
 
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
 
    def do_GET(self):
        qs       = parse_qs(urlparse(self.path).query)
        match_id = qs.get("id", [None])[0]
        if not match_id:
            self._send(400, {"error": "Missing ?id="}); return
 
        include = set(qs.get("include", ["scorecard,bbb,xi,analysis"])[0].split(","))
 
        _, nxt_sc = fetch(f"https://www.cricbuzz.com/live-cricket-scorecard/{match_id}")
 
        batting  = scrape_batting(nxt_sc)  if "scorecard" in include else []
        bowling  = scrape_bowling(nxt_sc)  if "scorecard" in include else []
        extras   = scrape_extras(nxt_sc)   if "scorecard" in include else []
        totals   = scrape_totals(nxt_sc)   if "scorecard" in include else []
        bbb      = scrape_bbb(nxt_sc)      if "bbb"       in include else []
        xi       = scrape_xi(nxt_sc)       if "xi"        in include else {}
        analysis = build_analysis(batting, bowling, totals, bbb) if "analysis" in include else {}
 
        result_m = re.findall(
            r'"status"\s*:\s*"([^"]*(?:won by|tied|no result|abandoned|draw)[^"]*)"',
            nxt_sc, re.I)
        match_result = result_m[0] if result_m else None
 
        self._send(200, {
            "status":       "success" if (batting or bbb or xi) else "empty",
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
            "match_result": match_result,
        })
 
    def _send(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "s-maxage=20, stale-while-revalidate=25")
        self.end_headers()
        self.wfile.write(body)
